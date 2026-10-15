"""Control loop: snapshot -> rules -> actions -> executors. Deterministic, no LLM."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.executors import ConnectorExecutor, Executor, PlatformExecutor
from kelpie.core.model import Snapshot
from kelpie.core.ports import ActionConnector, Alerter, PlatformAdapter
from kelpie.core.rule import Rule

log = logging.getLogger(__name__)

_ALERT_ACTIONS = {ActionKind.ALERT, ActionKind.ESCALATE}


class Outcome(StrEnum):
    DRY_RUN = "dry_run"  # global override: logged only
    OBSERVED = "observed"  # rule in observe mode: logged only
    SUGGESTED = "suggested"  # sent to a human as a suggestion
    ALERTED = "alerted"  # alert/escalation delivered
    EXECUTED = "executed"  # carried out by an executor
    NOT_EXECUTED = "not_executed"  # no executor could do it; a human was alerted
    FAILED = "failed"  # the executor raised
    DROPPED = "dropped"  # should have reached a human, but no alerter is configured


@dataclass(frozen=True, slots=True)
class Dispatched:
    """What happened to an action. ``mode`` is the effective mode (observe under dry-run)."""

    action: Action
    mode: Mode
    outcome: Outcome


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
    ``config.dry_run`` overrides every mode.
    """

    rules: Sequence[Rule]
    adapters: Iterable[PlatformAdapter] = ()
    connectors: Iterable[ActionConnector] = ()
    alerter: Alerter | None = None
    config: EngineConfig = field(default_factory=EngineConfig)
    executors: dict[str, Executor] = field(init=False)
    _platforms: list[str] = field(init=False)

    def __post_init__(self) -> None:
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
        for rule in self.rules:
            try:
                actions.extend(rule.evaluate(snapshot))
            except Exception:
                log.exception("rule %s failed; skipped", rule.name)
        return actions

    def execute(self, actions: Sequence[Action], snapshot: Snapshot) -> list[Dispatched]:
        if snapshot.now.tzinfo is None:
            raise ValueError("snapshot.now must be timezone-aware")
        results: list[Dispatched] = []
        for action in actions:
            mode = Mode.OBSERVE if self.config.dry_run else self.config.mode_for(action.rule)
            try:
                outcome = self._handle(action, mode, snapshot)
            except Exception:
                log.exception("action failed; skipped: %s", action)
                outcome = Outcome.FAILED
            results.append(Dispatched(action, mode, outcome))
        return results

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
        if executor is None:
            log.warning("no executor for action; alerting instead: %s", action)
        elif executor.execute(action):
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
