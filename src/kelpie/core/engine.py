"""Control loop: snapshot -> rules -> actions -> adapter. Deterministic, no LLM."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Snapshot
from kelpie.core.ports import Alerter, PlatformAdapter
from kelpie.core.rule import Rule

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Engine:
    rules: Sequence[Rule]
    adapter: PlatformAdapter
    alerter: Alerter | None = None
    dry_run: bool = True

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        actions: list[Action] = []
        for rule in self.rules:
            actions.extend(rule.evaluate(snapshot))
        return actions

    def execute(self, actions: Sequence[Action]) -> None:
        for action in actions:
            if self.dry_run:
                log.info("dry-run: %s", action)
                continue
            self._dispatch(action)

    def tick(self, snapshot_source: Callable[[], Snapshot]) -> list[Action]:
        actions = self.evaluate(snapshot_source())
        self.execute(actions)
        return actions

    def _dispatch(self, action: Action) -> None:
        tid = action.task_id
        match action.kind:
            case ActionKind.REQUEUE if tid:
                self.adapter.requeue(tid, action.reason)
            case ActionKind.UNBLOCK if tid:
                self.adapter.unblock(tid, action.reason)
            case ActionKind.COMMENT if tid:
                self.adapter.comment(tid, action.reason)
            case ActionKind.WIP_COMMIT if tid:
                self.adapter.wip_commit(tid, action.reason)
            case ActionKind.ALERT | ActionKind.ESCALATE if self.alerter:
                self.alerter.send(f"[{action.rule}] {action.kind}", action.reason, action.severity)
            case _:
                raise NotImplementedError(f"action not supported yet: {action.kind}")
