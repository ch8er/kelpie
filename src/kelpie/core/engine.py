"""Control loop: snapshot -> rules -> actions -> executors. Deterministic, no LLM."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from kelpie.core.actions import Action, ActionKind
from kelpie.core.executors import ConnectorExecutor, Executor, PlatformExecutor
from kelpie.core.model import Snapshot
from kelpie.core.ports import ActionConnector, Alerter, PlatformAdapter
from kelpie.core.rule import Rule

log = logging.getLogger(__name__)

_ALERT_ACTIONS = {ActionKind.ALERT, ActionKind.ESCALATE}


@dataclass(slots=True)
class Engine:
    """Runs rules over a snapshot and routes their actions to executors.

    Executors are platform adapters and action-capable connectors, keyed by name. An
    action goes to ``action.executor`` if set, else to its task's ``source`` adapter,
    else to the only platform adapter if there is exactly one. Actions that no executor
    can carry out become alerts for a human. A failing rule or action is logged and
    skipped; it never stops the loop.
    """

    rules: Sequence[Rule]
    adapters: Iterable[PlatformAdapter] = ()
    connectors: Iterable[ActionConnector] = ()
    alerter: Alerter | None = None
    dry_run: bool = True
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

    def execute(self, actions: Sequence[Action], snapshot: Snapshot) -> None:
        for action in actions:
            if self.dry_run:
                log.info("dry-run: %s", action)
                continue
            try:
                self._dispatch(action, snapshot)
            except Exception:
                log.exception("action failed; skipped: %s", action)

    def tick(self, snapshot_source: Callable[[], Snapshot]) -> list[Action]:
        snapshot = snapshot_source()
        actions = self.evaluate(snapshot)
        self.execute(actions, snapshot)
        return actions

    def route(self, action: Action, snapshot: Snapshot) -> Executor | None:
        if action.executor is not None:
            return self.executors.get(action.executor)
        task = snapshot.task(action.task_id) if action.task_id else None
        if task is not None and task.source is not None:
            return self.executors.get(task.source)
        if len(self._platforms) == 1:
            return self.executors[self._platforms[0]]
        return None

    def _dispatch(self, action: Action, snapshot: Snapshot) -> None:
        if action.kind in _ALERT_ACTIONS:
            self._alert(action)
            return
        executor = self.route(action, snapshot)
        if executor is None:
            log.warning("no executor for action; alerting instead: %s", action)
            self._alert(action, executed=False)
        elif not executor.execute(action):
            # e.g. missing task_id or session_id, no target owner, or a missing capability.
            log.warning("%s cannot execute; alerting instead: %s", executor.name, action)
            self._alert(action, executed=False)

    def _alert(self, action: Action, executed: bool = True) -> None:
        if self.alerter is None:
            log.warning("no alerter configured; dropped: %s", action)
            return
        title = f"[{action.rule}] {action.kind}" + (f" {action.task_id}" if action.task_id else "")
        if not executed:
            title += " (not executed, needs a human)"
        self.alerter.send(title, action.reason, action.severity)
