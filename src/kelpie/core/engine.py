"""Control loop: snapshot -> rules -> actions -> executors. Deterministic, no LLM."""

from __future__ import annotations

import inspect
import logging
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import (
    ANNOUNCES,
    AlertEvent,
    AlertItem,
    Did,
    SnoozeNote,
    SnoozeState,
    done_line,
    failed_line,
    failure_reason,
    lead_in,
    not_executed_line,
    suggested_line,
)
from kelpie.core.alerts import Transition as AlertTransition
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.executors import ConnectorExecutor, Executor, PlatformExecutor
from kelpie.core.incidents import FOLDED_UNDER, Outage, is_outage_state
from kelpie.core.ledger import (
    ActionLedger,
    Dispatched,
    LedgerStore,
    MemoryLedgerStore,
    Outcome,
    Suppression,
    subject,
)
from kelpie.core.model import Snapshot
from kelpie.core.ports import ActionConnector, Alerter, Deferred, PlatformAdapter
from kelpie.core.rule import Rule
from kelpie.core.states import MemoryStateStore, OpenState, StateStore, StateTracker, Transition

log = logging.getLogger(__name__)

_ALERT_ACTIONS = {ActionKind.ALERT, ActionKind.ESCALATE}
# An action that failed or that nothing could carry out needs a human: its alert goes out at
# least at this severity, so channels that skip info still hear about it.
_NEEDS_HUMAN = Severity.WARNING
_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}
# Effects whose own result is visible where it lands: no separate "done" message.
_QUIET_WHEN_DONE = {ActionKind.COMMENT}
# Outcomes after which an alert counts as announced (delivered, or logged by design).
_HANDLED = {
    Outcome.ALERTED,
    Outcome.SUGGESTED,
    Outcome.NOT_EXECUTED,
    Outcome.OBSERVED,
    Outcome.DRY_RUN,
    Outcome.DROPPED,
}


# Actions on a session rather than a task: routed to the adapter that reported the session.
SESSION_ACTIONS = frozenset({ActionKind.STOP})


@dataclass(slots=True)
class Engine:
    """Runs rules over a snapshot and routes their actions to executors.

    Executors are platform adapters and action-capable connectors, keyed by name. An
    action goes to ``action.executor`` if set; else a ``stop`` goes to the adapter that
    reported its session (``Session.source``); else an action goes to the ``source`` adapter
    of its task (or of the task behind ``params.session_id``); else to the only platform
    adapter if there is exactly one, unless ``fallback`` is off (multi-host: every board is
    remote, and a guess could act on the wrong host). Executors added with
    :meth:`add_executor` (remote boards) are routed to by name. Actions that no executor
    can carry out become alerts for a human. A failing rule or action is logged and
    skipped; it never stops the loop.

    Each rule runs in its configured mode (observe | suggest | act, default observe);
    ``config.dry_run`` overrides every mode. Every action passes the action ledger first
    (see :mod:`kelpie.core.ledger`). If the ledger itself fails, the engine fails closed:
    only observe and dry-run actions proceed, and a human is told once per tick.

    While a snapshot is partial (``Snapshot.unobserved``), states on the tasks it is missing,
    and farm-wide states, are not resolved.
    """

    rules: Sequence[Rule]
    adapters: Iterable[PlatformAdapter] = ()
    connectors: Iterable[ActionConnector] = ()
    alerter: Alerter | None = None
    config: EngineConfig = field(default_factory=EngineConfig)
    ledger_store: LedgerStore = field(default_factory=MemoryLedgerStore)
    state_store: StateStore = field(default_factory=MemoryStateStore)
    fallback: bool = True  # route to the only platform adapter when nothing else says where
    _by_name: dict[str, Rule] = field(init=False, default_factory=dict)
    _detail: str | None = field(init=False, default=None)  # of the action being handled
    ledger: ActionLedger = field(init=False)
    states: StateTracker = field(init=False)
    _evaluated: set[str] | None = field(init=False, default=None)
    rule_errors: Counter[str] = field(init=False, default_factory=Counter)
    _ledger_down_since: datetime | None = field(init=False, default=None)
    _ledger_write_failed: bool = field(init=False, default=False)
    # Open incidents and what they fold, kept across ticks while their state is open.
    _incidents: dict[str, _Incident] = field(init=False, default_factory=dict)
    executors: dict[str, Executor] = field(init=False)
    _platforms: list[str] = field(init=False)
    # Per tick: keys of effects an alert announces, what happened to them, the messages held
    # back for that alert, and the keys whose alert went out.
    _announced: set[str] = field(init=False, default_factory=set)
    _lines: dict[str, list[str]] = field(init=False, default_factory=dict)
    _held: dict[str, list[tuple[Action, bool, Did | None, str]]] = field(
        init=False, default_factory=dict
    )
    _told: set[str] = field(init=False, default_factory=set)
    _announcer_severity: dict[str, Severity] = field(init=False, default_factory=dict)
    # While an alert about a state is processed: what it says besides the rule's reason,
    # keyed by the alert's key.
    _transitions: dict[str, _StateNote] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.ledger = ActionLedger(
            self.ledger_store,
            cooldown=self.config.cooldown,
            retry_after=self.config.retry_after,
            max_per_subject_per_hour=self.config.max_actions_per_task_per_hour,
            once_window=self.config.once_window,
            once_attempts=self.config.once_attempts,
        )
        self.states = StateTracker(
            self.state_store, self.config.state_escalations, self.config.state_resolve_after
        )
        self.executors = {}
        self._platforms = []
        for adapter in self.adapters:
            self._add(PlatformExecutor(adapter))
            self._platforms.append(adapter.name)
        for connector in self.connectors:
            self._add(ConnectorExecutor(connector))
        self._by_name = {r.name: r for r in self.rules}

    def add_executor(self, executor: Executor) -> None:
        """Register an executor after construction (a remote board an agent reported).
        Call it from the control-loop thread; ValueError on a duplicate name."""
        self._add(executor)

    def _add(self, executor: Executor) -> None:
        if executor.name in self.executors:
            raise ValueError(f"duplicate executor name: {executor.name}")
        self.executors[executor.name] = executor

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        actions: list[Action] = []
        evaluated: set[str] = set()
        try:  # which states are open, for rules that keep one open on a lower bar
            open_ids = tuple(sorted(s.id for s in self.states.store.open_states()))
        except Exception:  # the state store's own errors are logged where it is written
            log.debug("state store unavailable; rules see no open states", exc_info=True)
            open_ids = ()
        if open_ids or snapshot.open_states:  # never what a snapshot source claimed
            snapshot = replace(snapshot, open_states=open_ids)
        for rule in self.rules:
            try:
                actions.extend(rule.evaluate(snapshot))
                evaluated.add(rule.name)
            except Exception:
                log.exception("rule %s failed; skipped", rule.name)
                self.rule_errors[rule.name] += 1
        # Only rules that ran can tell that one of their states is gone.
        self._evaluated = evaluated
        return actions

    @property
    def ledger_available(self) -> bool:
        """False while the last ledger read or write failed."""
        return self._ledger_down_since is None and not self._ledger_write_failed

    def execute(self, actions: Sequence[Action], snapshot: Snapshot) -> list[Dispatched]:
        if snapshot.now.tzinfo is None:
            raise ValueError("snapshot.now must be timezone-aware")
        results: list[Dispatched] = []
        seen: set[str] = set()
        # Effects first: an alert that announces one of them (params.announces) then says
        # what actually happened to it, and the effect's own message is not sent twice.
        # Incident alerts before the other alerts: what happened to them decides what folds.
        ordered = sorted(actions, key=lambda a: (a.kind in _ALERT_ACTIONS, _incident(a) is None))
        self._announced = {k for a in ordered for k in _announces(a)}
        self._lines, self._held, self._told = {}, {}, set()
        self._announcer_severity = {}
        for a in ordered:
            for k in _announces(a):
                known = self._announcer_severity.get(k, Severity.INFO)
                self._announcer_severity[k] = _higher(known, a.severity)
        self._keep_open_incidents(
            {a.state for a in ordered if _incident(a) is not None}, snapshot.now
        )
        try:
            for action in ordered:
                under = self._folded_under(action)
                if under is not None:
                    if action.state is not None:
                        seen.add(action.state)  # held back, not gone: it must not resolve
                    results.append(self._fold(action, under, snapshot))
                elif action.state is not None and action.kind in _ALERT_ACTIONS:
                    seen.add(action.state)
                    dispatched = self._state_alert(action, snapshot)
                    if (outage := _incident(action)) is not None:
                        self._note_incident(action, outage, dispatched)
                    results.append(dispatched)
                else:
                    results.append(self._process(action, snapshot))
            results.extend(self._resolve_gone(seen, snapshot))
        finally:
            self._release_held(snapshot)
            self._evaluated = None  # resolution is only valid right after evaluate()
        return results

    # --- incidents ----------------------------------------------------------------------

    def _keep_open_incidents(self, reported: set[str | None], now: datetime) -> None:
        """Forget incidents whose state closed. One not reported this tick (its rule failed,
        or its detection flickered) keeps folding what it folded last, but only within
        ``resolve_after`` of its last sighting: a rule that keeps failing must not hold
        alerts back for ever. Kept in memory only: after a restart an outage folds again
        once its rule reports it."""
        for state_id in list(self._incidents):
            if state_id in reported:
                continue
            try:
                state = self.states.store.get_state(state_id)
            except Exception:
                log.exception("state store unavailable; not folding under %s", state_id)
                state = None
            if state is None or now - state.last_seen >= self.states.resolve_after:
                del self._incidents[state_id]

    def _note_incident(self, action: Action, outage: Outage, dispatched: Dispatched) -> None:
        """Remember what an incident alert folds, by whether people got it: delivered now, or
        announced live before (its state is committed only once handled live, so a failed
        escalation of an outage people already know of keeps folding; a failed first
        announcement folds nothing). Under dry-run, what live mode would do (the rules'
        configured modes)."""
        if self.config.dry_run:
            reaches = self._configured(action) is not Mode.OBSERVE
            logged = not reaches
        else:
            known = self._safe_get_state(outage.state)
            reaches = dispatched.outcome is Outcome.ALERTED or (
                known is not None and known.mode is not Mode.OBSERVE
            )
            logged = dispatched.outcome is Outcome.OBSERVED
        if reaches or logged:
            self._incidents[outage.state] = _Incident(
                frozenset(outage.tasks), action.severity, reaches
            )
        else:  # never got to people: they have nothing in place of what it would fold
            self._incidents.pop(outage.state, None)

    def _safe_get_state(self, state_id: str) -> OpenState | None:
        try:
            return self.states.store.get_state(state_id)
        except Exception:
            log.exception("state store unavailable")
            return None

    def _folded_under(self, action: Action) -> str | None:
        """The incident this alert is folded under, if any: an alert about a task's wait
        (``about_wait``), not critical, about a task the incident explains, no more severe
        than the incident's alert, and only while that alert reached people, or while
        neither would (both only logged)."""
        if (
            not action.about_wait
            or action.kind is not ActionKind.ALERT
            or action.severity is Severity.CRITICAL
            or action.task_id is None
        ):
            return None
        quiet = self._configured(action) is Mode.OBSERVE
        for state_id, incident in sorted(self._incidents.items()):
            if (
                action.task_id in incident.tasks
                and _RANK[incident.severity] >= _RANK[action.severity]
                and (incident.reaches or quiet)
            ):
                return state_id
        return None

    def _fold(self, action: Action, under: str, snapshot: Snapshot) -> Dispatched:
        """Hold the alert back and record it under its incident. It gets a fingerprint of its
        own, so the ledger keeps ``folded_under`` instead of counting it on an earlier
        delivery of the same alert."""
        key = f"{action.key or action.state or action.reason}|{FOLDED_UNDER}:{under}"
        folded = replace(action, key=key, params={**action.params, FOLDED_UNDER: under})
        log.info("folded under %s: %s", under, action)
        if action.state is not None:  # still seen: last_seen moves on, no transition
            state_id, now = action.state, snapshot.now
            self._safe_state(lambda: self.states.touch(self.states.store.get_state(state_id), now))
        dispatched = Dispatched(
            folded, self._mode(action), Outcome.SUPPRESSED, Suppression.INCIDENT
        )
        self._record(dispatched, snapshot.now)
        return dispatched

    def _release_held(self, snapshot: Snapshot) -> None:
        """Send the effect messages whose announcing alert did not go out this tick."""
        for key, held in self._held.items():
            if key in self._told:
                continue
            # As urgent as the alert that would have carried it (a critical runaway's stop).
            floor = self._announcer_severity.get(key, Severity.INFO)
            for action, executed, kind, did in held:
                urgent = replace(action, severity=_higher(action.severity, floor))
                self._safe_alert(urgent, snapshot, executed=executed, kind=kind, did=did)
        self._announced, self._lines, self._held, self._told = set(), {}, {}, set()
        self._announcer_severity = {}

    def _process(self, action: Action, snapshot: Snapshot) -> Dispatched:
        mode = self._mode(action)
        try:
            hit = self.ledger.check(action, mode, snapshot.now)
            self._ledger_down_since = None
        except Exception:
            log.exception("action ledger unavailable")
            first = self._ledger_down_since is None
            if first:  # tell a human once per outage, not once per tick
                self._ledger_down_since = snapshot.now
                self._notice(Suppression.LEDGER_DOWN, action, snapshot)
            # Fail closed: nothing with an effect leaves Kelpie without a ledger.
            hit = None if mode is Mode.OBSERVE else (Suppression.LEDGER_DOWN, first)
        if hit is not None:
            why, first = hit
            log.info("suppressed (%s): %s", why, action)
            if first and why is Suppression.RATE_LIMIT:
                self._notice(why, action, snapshot)
            dispatched = Dispatched(action, mode, Outcome.SUPPRESSED, why, first)
        else:
            self._detail = None
            try:
                outcome = self._handle(action, mode, snapshot)
            except Deferred as exc:
                log.info("deferred: %s: %s", action, exc)
                if action.key is not None and action.key in self._announced:
                    # The alert announcing it must not read as if it was done.
                    line = (
                        f"Not done yet: {failure_reason(exc).rstrip('.')}; tried again next tick."
                    )
                    self._lines.setdefault(action.key, []).append(line)
                return Dispatched(action, mode, Outcome.DEFERRED)  # not an attempt
            except Exception as exc:
                self._detail = failure_reason(exc)
                log.warning("action failed: %s: %r", action, exc)
                log.debug("traceback of failed action: %s", action, exc_info=True)
                outcome = Outcome.FAILED
                if self._last_attempt(action, mode, snapshot.now):
                    did = failed_line(action, failure_reason(exc))
                    if action.kind in _ALERT_ACTIONS:
                        self._safe_alert(action, snapshot, executed=False, kind=Did.FAILED, did=did)
                    else:
                        self._effect_message(
                            action, snapshot, executed=False, kind=Did.FAILED, did=did
                        )
            dispatched = Dispatched(action, mode, outcome, detail=self._detail)
        self._record(dispatched, snapshot.now)
        return dispatched

    def _last_attempt(self, action: Action, mode: Mode, now: datetime) -> bool:
        """Whether this failure should reach a human.

        Every attempt is recorded as FAILED, but a human hears about it once: when the last
        allowed attempt of a once-action fails, else on the first failure per cooldown.
        """
        try:
            earlier = self.ledger.failed_attempts(action, mode, now)
        except Exception:
            log.exception("could not count earlier attempts; alerting")
            return True
        if action.once:
            return earlier + 1 >= self.config.once_attempts
        return earlier == 0

    # --- long-lived states --------------------------------------------------------------

    def _configured(self, action: Action) -> Mode:
        """The action's mode as configured: its rule's, or observe for an action its rule
        marks ``observe_only`` (a new check that stays in observe until switched on)."""
        return Mode.OBSERVE if action.observe_only else self.config.mode_for(action.rule)

    def _mode(self, action: Action) -> Mode:
        """The effective mode: observe under dry-run, else as configured."""
        return Mode.OBSERVE if self.config.dry_run else self._configured(action)

    def _state_alert(self, action: Action, snapshot: Snapshot) -> Dispatched:
        """Deliver an alert about a long-lived state only on a transition."""
        assert action.state is not None
        now, mode = snapshot.now, self._mode(action)
        try:
            transition, new, current = self.states.peek(action, now, mode)
        except Exception:
            log.exception("state store unavailable; alerting without transitions")
            return self._process(action, snapshot)  # the ledger cooldown still limits repeats
        if transition is Transition.ONGOING:
            self._safe_state(lambda: self.states.touch(current, now))
            dispatched = Dispatched(action, mode, Outcome.SUPPRESSED, Suppression.STATE_OPEN)
            self._record(dispatched, now)
            return dispatched
        shown = _TRANSITIONS.get(transition, AlertTransition.NEW)
        age = now - new.since if shown is AlertTransition.ESCALATED else None
        note = _StateNote(shown, age, action.reason)
        # The ledger records the reason with the transition worded in front of it; the alert
        # event carries the transition and the rule's own reason as separate fields.
        lead = lead_in(shown, age)
        # Each transition has its own fingerprint, so the cooldown only stops true repeats.
        key = f"{action.key or action.state}|{new.opened_at.isoformat()}|L{new.level}"
        key += f"|{new.severity}|{new.mode}"
        dispatched = self._process_noted(
            replace(action, reason=lead + action.reason, key=key), snapshot, note
        )
        if dispatched.outcome in _HANDLED:
            self._safe_state(lambda: self.states.commit(new))
        else:  # suppressed or failed: keep the old state so the next tick tries again
            self._safe_state(lambda: self.states.touch(current, now))
        return dispatched

    def _resolve_gone(self, seen: set[str], snapshot: Snapshot) -> list[Dispatched]:
        """Close states that rules which ran fine this tick stopped reporting."""
        if self._evaluated is None:
            return []
        try:
            gone = self.states.gone(self._evaluated, seen, snapshot.now)
        except Exception:
            log.exception("state store unavailable; cannot resolve states")
            return []
        gone = [
            s
            for s in gone
            if not _blind(s.task_id, snapshot.unobserved, self._reads_boards(s.rule, s.id))
        ]
        results: list[Dispatched] = []
        for state in gone:
            now_true = self._resolved_text(state, snapshot)
            age = snapshot.now - state.since
            note = _StateNote(AlertTransition.RESOLVED, age, now_true, closes=state.id)
            notice = Action(
                ActionKind.ALERT,
                state.rule,
                lead_in(AlertTransition.RESOLVED, age) + now_true,
                task_id=state.task_id,
                key=f"resolved:{state.id}|{state.opened_at.isoformat()}",
                params={"state": state.id},
                # told in the mode it was announced in: no live "resolved" for a state
                # people were never told about (an observe-only check's)
                observe_only=state.mode is Mode.OBSERVE,
            )
            dispatched = self._process_noted(notice, snapshot, note)
            results.append(dispatched)
            if dispatched.outcome in _HANDLED:  # otherwise retry the notice next tick
                self._safe_state(lambda s=state: self.states.resolve(s, snapshot.now))
        return results

    def _resolved_text(self, state: OpenState, snapshot: Snapshot) -> str:
        """What changed, for a resolve notice: the rule's own account of what is true now
        (``Rule`` docs: ``resolved``), else that it no longer reports it, naming the task;
        never the opening alert again."""
        hook = getattr(self._by_name.get(state.rule), "resolved", None)
        if callable(hook):
            try:
                # a hook that takes ``last_key`` also learns which alert last announced it
                wants_key = "last_key" in inspect.signature(hook).parameters
                text = (
                    hook(state.id, snapshot, last_key=state.key)
                    if wants_key
                    else hook(state.id, snapshot)
                )
            except Exception:
                log.exception("rule %s failed to describe resolved state %s", state.rule, state.id)
                text = None
            if isinstance(text, str) and text.strip():
                return text.strip()
            if text is not None:
                log.warning("rule %s described state %s with %r", state.rule, state.id, text)
        # What it no longer reports, quoted as the past: a task can hold several states.
        was = state.summary.split(". ")[0].rstrip(".")
        was = was if len(was) <= 160 else f"{was[:157]}..."
        task = snapshot.task(state.task_id) if state.task_id else None
        if task is not None:
            subject = f"task {task.id} ('{task.title}'): "
        elif state.task_id:
            subject = f"task {state.task_id}: "
        else:
            subject = ""
        return f'{subject}no longer reported: "{was}".'

    def _process_noted(self, action: Action, snapshot: Snapshot, note: _StateNote) -> Dispatched:
        """:meth:`_process` an alert about a state; its event carries ``note``."""
        assert action.key is not None
        self._transitions[action.key] = note
        try:
            return self._process(action, snapshot)
        finally:
            self._transitions.pop(action.key, None)

    def _reads_boards(self, rule: str, state: str) -> bool:
        """Whether a rule's state depends on the boards (``Rule`` docs: ``boards``)."""
        boards = getattr(self._by_name.get(rule), "boards", True)
        if callable(boards):
            try:
                return bool(boards(state))
            except Exception:
                log.exception("rule %s: boards(%r) failed", rule, state)
                return True
        return boards is not False

    @staticmethod
    def _safe_state(write: Callable[[], None]) -> None:
        try:
            write()
        except Exception:
            log.exception("could not update alert state")

    def _record(self, dispatched: Dispatched, now: datetime) -> None:
        try:
            self.ledger.record(dispatched, now)
            self._ledger_write_failed = False
        except Exception:
            # The entry stays in the ledger's memory, so it still counts for this process.
            log.exception("could not persist ledger entry; kept in memory")
            self._ledger_write_failed = True

    def _notice(self, why: Suppression, action: Action, snapshot: Snapshot) -> None:
        if why is Suppression.RATE_LIMIT:
            reason = (
                f"Rate limit of {self.config.max_actions_per_task_per_hour} actions/hour "
                f"reached for {subject(action)}; further actions are suppressed this hour."
            )
            notice = Action(ActionKind.ALERT, "kelpie", reason, task_id=action.task_id)
        else:
            reason = "Action ledger unavailable; actions with effects are held back."
            notice = Action(ActionKind.ALERT, "kelpie", reason)
        if not self.config.dry_run:
            self._safe_alert(replace(notice, severity=Severity.WARNING), snapshot)

    def _safe_alert(
        self,
        action: Action,
        snapshot: Snapshot,
        executed: bool = True,
        kind: Did | None = None,
        did: str | None = None,
    ) -> None:
        """Alert without ever raising out of the loop."""
        try:
            self._alert(action, snapshot, executed=executed, kind=kind, did=did)
        except Exception:
            log.exception("could not send alert: %s", action)

    def _effect_message(
        self,
        action: Action,
        snapshot: Snapshot,
        *,
        executed: bool = True,
        kind: Did | None = None,
        did: str,
    ) -> bool:
        """Tell a human what happened to an effect, or hold it for the alert of this tick
        that announces it. False if there is no alerter."""
        if self.alerter is None:
            log.warning("no alerter configured; dropped: %s", action)
            return False
        key = action.key
        if key is not None and key in self._announced:
            self._lines.setdefault(key, []).append(did)
            self._held.setdefault(key, []).append((action, executed, kind, did))
            return True
        return self._alert(action, snapshot, executed=executed, kind=kind, did=did)

    def tick(self, snapshot_source: Callable[[], Snapshot]) -> list[Dispatched]:
        snapshot = snapshot_source()
        return self.execute(self.evaluate(snapshot), snapshot)

    def _handle(self, action: Action, mode: Mode, snapshot: Snapshot) -> Outcome:
        if self.config.dry_run:
            log.info("dry-run: %s", action)
            return Outcome.DRY_RUN
        if mode is Mode.OBSERVE:
            log.info("observe: %s", action)
            return Outcome.OBSERVED
        if action.kind in _ALERT_ACTIONS:
            keys = _announces(action)
            lines = [line for k in keys for line in self._lines.get(k, ())]
            if any(not executed for k in keys for _, executed, _, _ in self._held.get(k, ())):
                # It carries a failure: that needs a human, whatever the alert's severity.
                action = replace(action, severity=_higher(action.severity, _NEEDS_HUMAN))
            # Done only when every announced effect was carried out: one with nothing held
            # (deferred, failed with retries left, suppressed) is still to do.
            done = bool(keys) and all(
                (held := self._held.get(k)) and all(kind is Did.DONE for _, _, kind, _ in held)
                for k in keys
            )
            if not self._alert(action, snapshot, did=" ".join(lines) or None, effects_done=done):
                return Outcome.DROPPED
            self._told.update(k for k in keys if k in self._lines)
            return Outcome.ALERTED
        if mode is Mode.SUGGEST:
            line = suggested_line(action)
            sent = self._effect_message(action, snapshot, kind=Did.SUGGESTED, did=line)
            return Outcome.SUGGESTED if sent else Outcome.DROPPED
        return self._dispatch(action, snapshot)

    def route(self, action: Action, snapshot: Snapshot) -> Executor | None:
        if action.executor:
            return self.executors.get(action.executor)
        task_id = action.task_id
        if session_id := action.params.get("session_id"):
            session = next((s for s in snapshot.sessions if s.id == session_id), None)
            # A stop acts on the session, so it goes to the adapter that reported it: its task
            # may be on another platform's board, or on none. Task actions that only mention
            # a session (comment, requeue, wip_commit) stay with the task's board.
            if (
                action.kind in SESSION_ACTIONS
                and session is not None
                and session.source in self.executors
            ):
                return self.executors[session.source]
            if not task_id:
                task_id = session.task_id if session else None
        task = snapshot.task(task_id) if task_id else None
        if task is not None and task.source:
            return self.executors.get(task.source)
        if self.fallback and len(self._platforms) == 1:
            return self.executors[self._platforms[0]]
        return None

    def _dispatch(self, action: Action, snapshot: Snapshot) -> Outcome:
        executor = self.route(action, snapshot)
        result = None if executor is None else executor.execute(action)
        if executor is None:
            log.warning("no executor for action; alerting instead: %s", action)
        elif result is Outcome.NOTHING_TO_DO:  # the only outcome an executor may give
            log.info("%s: nothing to do for %s", executor.name, action)
            return result
        elif result is True or (result and not isinstance(result, Outcome)):
            detail = result if isinstance(result, str) else None
            self._detail = detail
            if action.kind not in _QUIET_WHEN_DONE:
                try:  # it is done: a message that cannot be sent must not undo that
                    line = done_line(action, detail)
                    self._effect_message(action, snapshot, kind=Did.DONE, did=line)
                except Exception:
                    log.exception("could not announce: %s", action)
            return Outcome.EXECUTED
        else:
            # e.g. missing task_id or session_id, no target owner, or a missing capability.
            log.warning("%s cannot execute; alerting instead: %s", executor.name, action)
        line = not_executed_line(action)
        sent = self._effect_message(action, snapshot, executed=False, did=line)
        return Outcome.NOT_EXECUTED if sent else Outcome.DROPPED

    def _alert(
        self,
        action: Action,
        snapshot: Snapshot,
        executed: bool = True,
        kind: Did | None = None,
        did: str | None = None,
        effects_done: bool = False,
    ) -> bool:
        """Send ``action`` to a human. Return False if there is no alerter to send it to.
        ``kind`` is what Kelpie did when that is more than telling a human (suggested, done,
        failed); an action that was not executed counts as ``Did.NOT_EXECUTED`` unless it
        failed. ``effects_done``: the effects the alert announces were all carried out, so
        it asks nobody to do them (see :func:`alert_items`)."""
        if self.alerter is None:
            log.warning("no alerter configured; dropped: %s", action)
            return False
        subject = f"{action.kind} {action.task_id}" if action.task_id else str(action.kind)
        severity = action.severity
        if not executed and _RANK[severity] < _RANK[_NEEDS_HUMAN]:
            severity = _NEEDS_HUMAN
        if kind is None:
            kind = Did.NOTIFIED if executed else Did.NOT_EXECUTED
        note = self._transitions.get(action.key) if action.key is not None else None
        self.alerter.send(
            AlertEvent(
                rule=action.rule,
                subject=subject,
                reason=action.reason if note is None else note.reason,
                severity=severity,
                transition=AlertTransition.NEW if note is None else note.transition,
                did=kind,
                did_text=did,
                age=None if note is None else note.age,
                headline=_headline(action),
                items=alert_items(action, snapshot, effects_done=effects_done),
                snoozed=snoozed_items(action, snapshot),
                snoozed_total=_count(action.params.get("snoozed_total")),
                at=snapshot.now,
                # a resolve notice names the state it closes (its Action.state stays None)
                state=action.state if note is None or note.closes is None else note.closes,
            )
        )
        return True


@dataclass(frozen=True, slots=True)
class _StateNote:
    """What the alert about a long-lived state says besides the rule's reason."""

    transition: AlertTransition
    age: timedelta | None  # how long the condition has been open (escalated, resolved)
    reason: str  # the rule's own reason, without the wording of the transition
    # a resolve notice: the state it closes, which its event names (never Action.state,
    # which would keep the state open)
    closes: str | None = None


_TRANSITIONS = {
    Transition.ESCALATED: AlertTransition.ESCALATED,
    Transition.CHANGED: AlertTransition.CHANGED,
}


def _blind(task_id: str | None, unobserved: Sequence[str], boards: bool = True) -> bool:
    """Whether a state may have stopped being reported only because part of the farm is not
    in the snapshot: one on a task of an unobserved agent, or a farm-wide one (no task) of a
    rule that reads the boards (``boards``) while anything is unobserved."""
    if not unobserved:
        return False
    if task_id is None:
        return boards
    if "hub" in unobserved:
        return True
    return any(task_id.startswith(f"{part.split('/')[0]}:") for part in unobserved)


def _higher(a: Severity, b: Severity) -> Severity:
    return a if _RANK[a] >= _RANK[b] else b


def _announces(action: Action) -> list[str]:
    """Keys of the effects an alert reports on (``params.announces``: a key or a list)."""
    if action.kind not in _ALERT_ACTIONS:
        return []
    raw = action.params.get(ANNOUNCES)
    keys = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [k for k in keys if isinstance(k, str) and k]


@dataclass(frozen=True, slots=True)
class _Incident:
    """An open incident's alert as the engine last handled it: what it folds."""

    tasks: frozenset[str]
    severity: Severity
    reaches: bool  # people got its alert; else it was only logged


def _incident(action: Action) -> Outage | None:
    """The outage an alert reports, whichever rule found it (its params name its own
    state)."""
    if action.kind is not ActionKind.ALERT or action.state is None:
        return None
    if not is_outage_state(action.state):
        return None
    outage = Outage.from_params(action.params)
    return outage if outage is not None and outage.state == action.state else None


# --- what an alert is about, for its layout ------------------------------------------------


def _headline(action: Action) -> str | None:
    """The rule's own one-line summary (``params.headline``), if it gave one."""
    value = action.params.get("headline")
    return value.strip() if isinstance(value, str) and value.strip() else None


def alert_items(
    action: Action, snapshot: Snapshot, *, effects_done: bool = False
) -> tuple[AlertItem, ...]:
    """The tasks an alert is about: ``params.items`` (mappings with ``task_id`` and optional
    ``title``, ``needed``, ``since``, ``blocking``, ``snooze`` and ``snooze_task``; other
    keys are the rule's own and are ignored; malformed entries are skipped), else the
    action's own task (with ``params.snooze`` and ``params.snooze_task``). A ``snooze`` is a
    mapping (:func:`_snooze_note`); a malformed one is dropped, the item kept. Titles come
    from the entry, else the snapshot; ages are counted to ``snapshot.now``. Only the actions of
    the current tick are rendered, never ledger rows. With ``effects_done`` (the alert
    announces effects Kelpie carried out: a stop, a close) no item says what is needed: the
    rule's ask is what Kelpie already did; a suggested, failed or not executed effect keeps
    it. Pure, and never raises on a rule's params."""
    items = _items(action, snapshot)
    return tuple(replace(i, needed=None) for i in items) if effects_done else items


def _items(action: Action, snapshot: Snapshot) -> tuple[AlertItem, ...]:
    raw = action.params.get("items")
    if isinstance(raw, list):
        items = tuple(i for e in raw if (i := _item(e, snapshot)) is not None)
        if items:
            return items
    if not action.task_id:
        return ()
    task = snapshot.task(action.task_id)
    since = action.since or (task.status_since if task is not None else None)
    needed = action.params.get("needed")
    return (
        AlertItem(
            action.task_id,
            _title(action.task_id, snapshot),
            _age(since, snapshot.now),
            needed.strip() if isinstance(needed, str) and needed.strip() else None,
            _count(action.params.get("blocking")),
            _start(since),
            _snooze_note(action.params.get("snooze")),
            _task_ref(action.params.get("snooze_task")),
        ),
    )


def snoozed_items(action: Action, snapshot: Snapshot) -> tuple[AlertItem, ...]:
    """The tasks a person snoozed that an alert leaves out and lists in its footer:
    ``params.snoozed``, entries like ``params.items``' that carry a ``snooze`` (others are
    skipped). Pure, and never raises on a rule's params."""
    raw = action.params.get("snoozed")
    if not isinstance(raw, list):
        return ()
    found = (_item(e, snapshot) for e in raw)
    return tuple(i for i in found if i is not None and i.snooze is not None)


def _snooze_note(value: object) -> SnoozeNote | None:
    """A ``snooze`` mapping (``state``: snoozed, expired or grown; ``until``: ISO 8601 with
    an offset; optional ``reason``, ``by``, ``source`` and ``grown``), or None when it is
    absent or malformed."""
    if not isinstance(value, Mapping):
        return None
    try:
        state = SnoozeState(value.get("state"))
    except ValueError:
        return None
    ok, until = _since(value.get("until"))
    reason, by, grown = value.get("reason"), value.get("by"), value.get("grown", 0)
    if not ok or until is None or _count(grown) is None:
        return None
    try:
        until.astimezone(UTC)  # out of range (year 9999 with an offset): nothing to show
    except OverflowError:
        return None
    if not all(v is None or isinstance(v, str) for v in (reason, by)):
        return None
    return SnoozeNote(
        state,
        until,
        (reason.strip() or None) if reason else None,
        (by.strip() or None) if by else None,
        value.get("source") == "marker",
        _count(grown) or 0,
    )


def _item(entry: object, snapshot: Snapshot) -> AlertItem | None:
    """One entry of ``params.items``, or None when it is malformed."""
    if not isinstance(entry, Mapping):
        return None
    task_id = entry.get("task_id")
    needed, blocking, title = entry.get("needed"), entry.get("blocking"), entry.get("title")
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if title is not None and not isinstance(title, str):
        return None
    if needed is not None and not isinstance(needed, str):
        return None
    if blocking is not None and _count(blocking) is None:
        return None
    ok, since = _since(entry.get("since"))
    if not ok:
        return None
    return AlertItem(
        task_id,
        title.strip() if title and title.strip() else _title(task_id, snapshot),
        _age(since, snapshot.now),
        needed.strip() if needed and needed.strip() else None,
        _count(blocking),
        _start(since),
        _snooze_note(entry.get("snooze")),
        _task_ref(entry.get("snooze_task")),
    )


MAX_TASK_REF = 200  # characters of a ``snooze_task``; a longer one is dropped
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})  # never in a task id


def _task_ref(value: object) -> str | None:
    """A task id a rule names (``snooze_task``), or None when it is absent or malformed: too
    long, or with a control or format character (it may come from a condition an agent
    registered, and ends up in logs and chat buttons)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_TASK_REF:
        return None
    if any(unicodedata.category(ch) in _UNPRINTABLE for ch in value):
        return None
    return value


def _since(value: object) -> tuple[bool, datetime | None]:
    """(usable, a timezone-aware datetime or None when absent) from a datetime or ISO text."""
    if value is None:
        return True, None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return False, None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False, None
    return True, value


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _title(task_id: str, snapshot: Snapshot) -> str | None:
    task = snapshot.task(task_id)
    if task is None or not task.title or not task.title.strip():
        return None
    return task.title.strip()


def _start(since: datetime | None) -> datetime | None:
    return since if since is not None and since.tzinfo is not None else None


def _age(since: datetime | None, now: datetime) -> timedelta | None:
    if since is None or since.tzinfo is None:
        return None
    return max(now - since, timedelta(0))
