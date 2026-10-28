"""The server side of multi-host: agents' boards merged into one snapshot, and actions on
them carried out through their agents (docs/multi-host.md).

:class:`RemoteFarm` is called once per tick, in the control-loop thread. It merges every
fresh report (namespaced ids, ``source`` = ``<agent>/<adapter>``), lists in
``Snapshot.unobserved`` what it could not merge, alerts once when an agent goes silent or
its clock drifts, and registers a :class:`RemoteExecutor` per reported board. A remote
executor hands the action to the hub and waits within a per-agent budget per tick, so a
slow or hostile agent costs the control loop at most ``[hub] action_timeout`` per tick.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, Did, Transition
from kelpie.core.executors import Executor
from kelpie.core.ledger import Outcome as LedgerOutcome
from kelpie.core.model import Snapshot
from kelpie.core.ports import Alerter, Deferred
from kelpie.remote.hub import AgentView, Hub
from kelpie.remote.protocol import (
    TASK_REF_KINDS,
    Outcome,
    ProtocolError,
    WireAction,
    local,
    local_condition_id,
    namespace_snapshot,
    wire_params,
)

log = logging.getLogger(__name__)

MAX_ACTIONS_PER_TICK = 16  # per agent; more fail fast and the ledger retries them
FRESH_SLACK = timedelta(seconds=10)  # on top of twice the agent's interval


class FarmObserver(Protocol):
    def agent_state(self, agent: str, up: bool, last_report: datetime | None) -> None: ...

    def adapter_failed(self, name: str) -> None: ...


class Busy(Deferred):
    """The agent cannot take the action this tick (its budget is used up, or it is not
    reporting); the engine asks again next tick, without counting an attempt."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.public_reason = reason


class RemoteActionFailed(Exception):
    """The agent's adapter raised; ``public_reason`` is the agent's safe summary."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.public_reason = reason


@dataclass(slots=True)
class _Budget:
    seconds: float = 0.0
    actions: int = 0


@dataclass(slots=True)
class _Health:
    """What the farm told a human about one agent (alerts on transitions only)."""

    last_fresh: datetime | None = None
    silent_told: bool = False
    skew_told: bool = False


@dataclass(slots=True)
class RemoteFarm:
    hub: Hub
    add_executor: Callable[[Executor], None]
    clock: Callable[[], datetime]
    alerter: Callable[[], Alerter | None] = lambda: None
    observer: FarmObserver | None = None
    dry_run: bool = False  # alerts are only logged, as for every other alert
    late_after: timedelta = timedelta(minutes=5)  # the engine's retry_after
    started: datetime = field(init=False)
    _kinds: dict[str, frozenset[ActionKind]] = field(init=False, default_factory=dict)
    _fresh: set[str] = field(init=False, default_factory=set)
    _budgets: dict[str, _Budget] = field(init=False, default_factory=dict)
    _health: dict[str, _Health] = field(init=False, default_factory=dict)
    _failing: set[str] = field(init=False, default_factory=set)  # boards told unreadable

    def __post_init__(self) -> None:
        self.started = self.clock()

    # --- merge ----------------------------------------------------------------------------

    def snapshot(self, now: datetime) -> Snapshot:
        """The merged boards of every agent with a fresh report, as of ``now``."""
        self._budgets.clear()  # a new tick: every agent gets its budget again
        parts: list[Snapshot] = []
        unobserved: list[str] = []
        fresh: set[str] = set()
        failing: set[str] = set()
        for view in self.hub.agents():
            report = view.report
            if not self._usable(view, now):
                unobserved.append(view.name)
                continue
            assert report is not None
            fresh.add(view.name)
            for part in report.adapters:
                source = f"{view.name}/{part.name}"
                self._register(view.name, part.name, part.actions)
                if part.snapshot is None:
                    log.error("agent %s: adapter %s failed: %s", view.name, part.name, part.error)
                    if self.observer is not None:
                        self.observer.adapter_failed(source)
                    unobserved.append(source)
                    failing.add(source)
                    if source not in self._failing:
                        self._failing.add(source)
                        self._alert(
                            Severity.WARNING,
                            f"board {source} unreadable",
                            f"Agent {view.name} cannot read its {part.name} board "
                            f"({part.error}); its tasks are not evaluated until it can.",
                        )
                    continue
                parts.append(namespace_snapshot(view.name, part.name, part.snapshot))
                for board in part.unobserved:  # one board of several the adapter reads
                    where = f"{source}:{board}"
                    unobserved.append(f"{view.name}:{board}")  # the prefix of its task ids
                    failing.add(where)
                    if self.observer is not None:
                        self.observer.adapter_failed(where)
                    if where not in self._failing:
                        self._failing.add(where)
                        self._alert(
                            Severity.WARNING,
                            f"board {where} unreadable",
                            f"Agent {view.name} cannot read its {part.name} board {board}; "
                            "its tasks are not evaluated until it can.",
                        )
        for source in sorted(self._failing - failing):
            if source.split(":")[0] in failing:  # a board of an adapter that fails whole
                continue
            if source.split("/")[0] in fresh:  # read again, or no longer reported
                self._failing.discard(source)
                self._alert(
                    Severity.INFO,
                    f"board {source} unreadable",
                    f"Agent {source.split('/')[0]} reads its {source.split('/')[1]} board again.",
                    transition=Transition.RESOLVED,
                )
        self._fresh = fresh
        self._tell_late()
        return Snapshot(
            now=now,
            tasks=tuple(t for p in parts for t in p.tasks),
            owners=tuple(o for p in parts for o in p.owners),
            dependencies=tuple(d for p in parts for d in p.dependencies),
            conditions=tuple(c for p in parts for c in p.conditions),
            sessions=tuple(s for p in parts for s in p.sessions),
            spans=tuple(s for p in parts for s in p.spans),
            unobserved=tuple(unobserved),
        )

    def _usable(self, view: AgentView, now: datetime) -> bool:
        """Whether the agent's last report may be merged: young enough and from a clock in
        range. Tells a human on transitions to and from silence and skew."""
        health = self._health.setdefault(view.name, _Health())
        settings = self.hub.settings
        report, at = view.report, view.report_at
        skew = None if report is None or at is None else report.sent_at - at
        skewed = skew is not None and abs(skew) > settings.max_clock_skew
        fresh = (
            report is not None
            and at is not None
            and not skewed
            and now - at <= 2 * report.interval + FRESH_SLACK
        )
        if fresh:
            health.last_fresh = at
        if skewed and not health.skew_told:
            assert skew is not None
            health.skew_told = True
            self._alert(
                Severity.WARNING,
                f"agent {view.name} clock off",
                f"The clock of agent {view.name} is {_age(abs(skew))} "
                f"{'ahead of' if skew > timedelta(0) else 'behind'} the server's; its board is "
                "not evaluated until it is in range (run NTP on both).",
            )
        elif not skewed and health.skew_told and report is not None:
            health.skew_told = False
            self._alert(
                Severity.INFO,
                f"agent {view.name} clock off",
                f"The clock of agent {view.name} is in range again.",
                transition=Transition.RESOLVED,
            )
        since = health.last_fresh or self.started
        silent = not fresh and now - since > settings.silent_after
        if silent and not health.silent_told:
            health.silent_told = True
            when = (
                f"since {health.last_fresh.isoformat(timespec='seconds')}"
                if health.last_fresh
                else "since Kelpie started"
            )
            self._alert(
                Severity.WARNING,
                f"agent {view.name} silent",
                f"No fresh report from agent {view.name} {when}; its tasks are not evaluated "
                "and their alerts are held until it reports again.",
            )
        elif fresh and health.silent_told:
            health.silent_told = False
            self._alert(
                Severity.INFO,
                f"agent {view.name} silent",
                f"Agent {view.name} reports again.",
                transition=Transition.RESOLVED,
            )
        if self.observer is not None:
            self.observer.agent_state(view.name, fresh, at)
        return fresh

    def _register(self, agent: str, adapter: str, kinds: frozenset[ActionKind]) -> None:
        source = f"{agent}/{adapter}"
        if source not in self._kinds:
            self.add_executor(RemoteExecutor(self, agent, adapter))
        self._kinds[source] = kinds

    # --- actions --------------------------------------------------------------------------

    def execute(self, agent: str, adapter: str, action: Action) -> bool | str | LedgerOutcome:
        """Carry out ``action`` on ``agent``'s board ``adapter`` (see RemoteExecutor)."""
        source = f"{agent}/{adapter}"
        if action.kind not in self._kinds.get(source, frozenset()):
            log.warning("%s does not carry out %s", source, action.kind)
            return False
        wire = self._wire(agent, adapter, action)
        if wire is None:
            return False
        if agent not in self._fresh:
            raise Busy(f"agent {agent} is not reporting")
        budget = self._budgets.setdefault(agent, _Budget())
        left = self.hub.settings.action_timeout.total_seconds() - budget.seconds
        if left < 0.5 or budget.actions >= MAX_ACTIONS_PER_TICK:
            raise Busy(f"agent {agent} is busy this tick")
        budget.actions += 1
        started = time.monotonic()
        try:
            result = self.hub.send(agent, wire, left)
        finally:
            budget.seconds += time.monotonic() - started
        match result.outcome:
            case Outcome.DONE:
                return result.detail or True
            case Outcome.NOTHING_TO_DO:
                return LedgerOutcome.NOTHING_TO_DO
            case Outcome.NOT_EXECUTED:
                log.warning("agent %s did not carry out %s: %s", agent, action.kind, result.detail)
                return False
        raise RemoteActionFailed(result.detail or f"it failed on agent {agent}")

    def _wire(self, agent: str, adapter: str, action: Action) -> WireAction | None:
        """``action`` in the agent's own ids; None when it names another host's ids."""
        task_id = None
        if action.task_id is not None:
            task_id = local(agent, action.task_id)
            if task_id is None:
                log.warning("%s is not a task of agent %s: %s", action.task_id, agent, action)
                return None
        params: dict[str, Any] = dict(action.params)
        for key in ("session_id", "owner_id"):
            if isinstance(params.get(key), str):
                mine = local(agent, params[key])
                if mine is None:
                    log.warning("%s %s is not on agent %s", key, params[key], agent)
                    return None
                params[key] = mine
        # Only a wait carries conditions to register; elsewhere (wake, unblock) they are
        # condition ids for the record, which wire_params drops.
        if action.kind is ActionKind.WAIT and isinstance(params.get("conditions"), list):
            others = [a.name for a in self.hub.settings.agents if a.name != agent]
            conditions = [_condition(agent, c, others) for c in params["conditions"]]
            if any(c is None for c in conditions):
                log.warning("a condition of %s names another host's task", action)
                return None
            params["conditions"] = conditions
        try:
            clean = wire_params(action.kind, params)
        except ProtocolError as exc:
            log.warning("cannot send %s to agent %s: %s", action.kind, agent, exc)
            return None
        return WireAction("", adapter, action.kind, action.rule, action.reason, task_id, clean)

    def _tell_late(self) -> None:
        for late in self.hub.late_results(self.late_after):
            action = late.action
            target = action.task_id or action.params.get("session_id") or "?"
            if late.result.outcome is not Outcome.DONE:
                log.info("agent %s: late result for %s: %s", late.agent, action.id, late.result)
                continue
            self._alert(
                Severity.INFO,
                f"{action.kind} {late.agent}:{target}",
                f"Carried out after all on agent {late.agent}, after Kelpie had reported it "
                f"as failed: {action.kind} {target}.",
                rule=action.rule,
                did=Did.DONE,
                did_text=late.result.detail,
            )

    def _alert(
        self,
        severity: Severity,
        subject: str,
        reason: str,
        *,
        rule: str = "kelpie",
        transition: Transition = Transition.NEW,
        did: Did = Did.NOTIFIED,
        did_text: str | None = None,
    ) -> None:
        level = logging.INFO if severity is Severity.INFO else logging.WARNING
        log.log(level, "[%s] %s (%s): %s", rule, subject, transition, reason)
        alerter = self.alerter()
        if self.dry_run or alerter is None:
            return
        event = AlertEvent(rule, subject, reason, severity, transition, did, did_text)
        try:
            alerter.send(event)
        except Exception:
            log.exception("could not send alert: %s", subject)


@dataclass(frozen=True, slots=True)
class RemoteExecutor:
    """Carries out actions on one board of one agent: ``<agent>/<adapter>``."""

    farm: RemoteFarm
    agent: str
    adapter: str

    @property
    def name(self) -> str:
        return f"{self.agent}/{self.adapter}"

    def execute(self, action: Action) -> bool | str | LedgerOutcome:
        return self.farm.execute(self.agent, self.adapter, action)


def _condition(agent: str, raw: object, others: Iterable[str] = ()) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    params = dict(raw.get("params") or {})
    if raw.get("kind") in TASK_REF_KINDS and isinstance(params.get("task"), str):
        task = local(agent, params["task"])
        if task is None:
            return None
        params["task"] = task
    cid = raw.get("id")
    if isinstance(cid, str):
        if not cid.endswith(f"@{agent}") and any(cid.endswith(f"@{o}") for o in others):
            return None  # a condition another machine reported
        cid = local_condition_id(agent, cid)
    return {
        "id": cid,
        "kind": raw.get("kind"),
        "params": params,
    }


def _age(d: timedelta) -> str:
    seconds = d.total_seconds()
    return f"{seconds / 60:.0f} min" if seconds >= 120 else f"{seconds:.0f}s"
