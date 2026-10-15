"""Control loop: snapshot -> rules -> actions -> executors. Deterministic, no LLM."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.executors import ConnectorExecutor, Executor, PlatformExecutor
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
from kelpie.core.ports import ActionConnector, Alerter, PlatformAdapter
from kelpie.core.rule import Rule
from kelpie.core.states import MemoryStateStore, StateStore, StateTracker, Transition

log = logging.getLogger(__name__)

_ALERT_ACTIONS = {ActionKind.ALERT, ActionKind.ESCALATE}
# Outcomes after which an alert counts as announced (delivered, or logged by design).
_HANDLED = {
    Outcome.ALERTED,
    Outcome.SUGGESTED,
    Outcome.NOT_EXECUTED,
    Outcome.OBSERVED,
    Outcome.DRY_RUN,
    Outcome.DROPPED,
}


@dataclass(slots=True)
class Engine:
    """Runs rules over a snapshot and routes their actions to executors.

    Executors are platform adapters and action-capable connectors, keyed by name. An
    action goes to ``action.executor`` if set, else to the ``source`` adapter of its task
    (or of the task behind ``params.session_id``), else to the only platform adapter if
    there is exactly one. Actions that no executor
    can carry out become alerts for a human. A failing rule or action is logged and
    skipped; it never stops the loop.

    Each rule runs in its configured mode (observe | suggest | act, default observe);
    ``config.dry_run`` overrides every mode. Every action passes the action ledger first
    (see :mod:`kelpie.core.ledger`). If the ledger itself fails, the engine fails closed:
    only observe and dry-run actions proceed, and a human is told once per tick.
    """

    rules: Sequence[Rule]
    adapters: Iterable[PlatformAdapter] = ()
    connectors: Iterable[ActionConnector] = ()
    alerter: Alerter | None = None
    config: EngineConfig = field(default_factory=EngineConfig)
    ledger_store: LedgerStore = field(default_factory=MemoryLedgerStore)
    state_store: StateStore = field(default_factory=MemoryStateStore)
    ledger: ActionLedger = field(init=False)
    states: StateTracker = field(init=False)
    _evaluated: set[str] | None = field(init=False, default=None)
    rule_errors: Counter[str] = field(init=False, default_factory=Counter)
    _ledger_down_since: datetime | None = field(init=False, default=None)
    _ledger_write_failed: bool = field(init=False, default=False)
    executors: dict[str, Executor] = field(init=False)
    _platforms: list[str] = field(init=False)

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

    def _add(self, executor: Executor) -> None:
        if executor.name in self.executors:
            raise ValueError(f"duplicate executor name: {executor.name}")
        self.executors[executor.name] = executor

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        actions: list[Action] = []
        evaluated: set[str] = set()
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
        for action in actions:
            if action.state is not None and action.kind in _ALERT_ACTIONS:
                seen.add(action.state)
                results.append(self._state_alert(action, snapshot))
            else:
                results.append(self._process(action, snapshot))
        results.extend(self._resolve_gone(seen, snapshot))
        self._evaluated = None  # resolution is only valid right after evaluate()
        return results

    def _process(self, action: Action, snapshot: Snapshot) -> Dispatched:
        mode = Mode.OBSERVE if self.config.dry_run else self.config.mode_for(action.rule)
        try:
            hit = self.ledger.check(action, mode, snapshot.now)
            self._ledger_down_since = None
        except Exception:
            log.exception("action ledger unavailable")
            first = self._ledger_down_since is None
            if first:  # tell a human once per outage, not once per tick
                self._ledger_down_since = snapshot.now
                self._notice(Suppression.LEDGER_DOWN, action)
            # Fail closed: nothing with an effect leaves Kelpie without a ledger.
            hit = None if mode is Mode.OBSERVE else (Suppression.LEDGER_DOWN, first)
        if hit is not None:
            why, first = hit
            log.info("suppressed (%s): %s", why, action)
            if first and why is Suppression.RATE_LIMIT:
                self._notice(why, action)
            dispatched = Dispatched(action, mode, Outcome.SUPPRESSED, why, first)
        else:
            try:
                outcome = self._handle(action, mode, snapshot)
            except Exception:
                log.exception("action failed: %s", action)
                outcome = Outcome.FAILED
                self._safe_alert(action, executed=False, prefix="failed")
            dispatched = Dispatched(action, mode, outcome)
        self._record(dispatched, snapshot.now)
        return dispatched

    # --- long-lived states --------------------------------------------------------------

    def _mode(self, rule: str) -> Mode:
        return Mode.OBSERVE if self.config.dry_run else self.config.mode_for(rule)

    def _state_alert(self, action: Action, snapshot: Snapshot) -> Dispatched:
        """Deliver an alert about a long-lived state only on a transition."""
        assert action.state is not None
        now, mode = snapshot.now, self._mode(action.rule)
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
        prefix = {
            Transition.ESCALATED: f"Still open after {_age(now - new.since)}: ",
            Transition.CHANGED: "Changed: ",
        }.get(transition, "")
        # Each transition has its own fingerprint, so the cooldown only stops true repeats.
        key = f"{action.key or action.state}|{new.opened_at.isoformat()}|L{new.level}"
        key += f"|{new.severity}|{new.mode}"
        dispatched = self._process(
            replace(action, reason=prefix + action.reason, key=key), snapshot
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
        results: list[Dispatched] = []
        for state in gone:
            first_line = state.summary.split(". ")[0].rstrip(".")
            notice = Action(
                ActionKind.ALERT,
                state.rule,
                f"Resolved after {_age(snapshot.now - state.since)}: {first_line}.",
                task_id=state.task_id,
                key=f"resolved:{state.id}|{state.opened_at.isoformat()}",
                params={"state": state.id},
            )
            dispatched = self._process(notice, snapshot)
            results.append(dispatched)
            if dispatched.outcome in _HANDLED:  # otherwise retry the notice next tick
                self._safe_state(lambda s=state: self.states.resolve(s, snapshot.now))
        return results

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

    def _notice(self, why: Suppression, action: Action) -> None:
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
            self._safe_alert(replace(notice, severity=Severity.WARNING))

    def _safe_alert(self, action: Action, executed: bool = True, prefix: str = "") -> None:
        """Alert without ever raising out of the loop."""
        try:
            self._alert(action, executed=executed, prefix=prefix)
        except Exception:
            log.exception("could not send alert: %s", action)

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
            return Outcome.ALERTED if self._alert(action) else Outcome.DROPPED
        if mode is Mode.SUGGEST:
            return Outcome.SUGGESTED if self._alert(action, prefix="suggest") else Outcome.DROPPED
        return self._dispatch(action, snapshot)

    def route(self, action: Action, snapshot: Snapshot) -> Executor | None:
        if action.executor:
            return self.executors.get(action.executor)
        task_id = action.task_id
        if not task_id and (session_id := action.params.get("session_id")):
            session = next((s for s in snapshot.sessions if s.id == session_id), None)
            task_id = session.task_id if session else None
        task = snapshot.task(task_id) if task_id else None
        if task is not None and task.source:
            return self.executors.get(task.source)
        if len(self._platforms) == 1:
            return self.executors[self._platforms[0]]
        return None

    def _dispatch(self, action: Action, snapshot: Snapshot) -> Outcome:
        executor = self.route(action, snapshot)
        result = None if executor is None else executor.execute(action)
        if executor is None:
            log.warning("no executor for action; alerting instead: %s", action)
        elif isinstance(result, Outcome):
            log.info("%s: %s for %s", executor.name, result, action)
            return result
        elif result is True:
            return Outcome.EXECUTED
        else:
            # e.g. missing task_id or session_id, no target owner, or a missing capability.
            log.warning("%s cannot execute; alerting instead: %s", executor.name, action)
        return Outcome.NOT_EXECUTED if self._alert(action, executed=False) else Outcome.DROPPED

    def _alert(self, action: Action, executed: bool = True, prefix: str = "") -> bool:
        """Send ``action`` to a human. Return False if there is no alerter to send it to."""
        if self.alerter is None:
            log.warning("no alerter configured; dropped: %s", action)
            return False
        title = f"[{action.rule}] {prefix + ' ' if prefix else ''}{action.kind}"
        title += f" {action.task_id}" if action.task_id else ""
        if not executed:
            title += " (not executed, needs a human)"
        self.alerter.send(title, action.reason, action.severity)
        return True


def _age(d: timedelta) -> str:
    hours = d.total_seconds() / 3600
    return f"{hours:.1f}h" if hours >= 1 else f"{max(0, int(d.total_seconds() // 60))}m"
