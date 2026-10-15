"""Control loop: snapshot -> rules -> actions -> adapter. Deterministic, no LLM."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Snapshot
from kelpie.core.ports import Alerter, PlatformAdapter, SupportsPause, SupportsReassign
from kelpie.core.rule import Rule

log = logging.getLogger(__name__)

_TASK_ACTIONS = {
    ActionKind.REQUEUE,
    ActionKind.UNBLOCK,
    ActionKind.COMMENT,
    ActionKind.WIP_COMMIT,
    ActionKind.WAKE,
    ActionKind.REASSIGN,
    ActionKind.PAUSE,
}


@dataclass(slots=True)
class Engine:
    """Runs rules over a snapshot and dispatches their actions.

    A failing rule or action is logged and skipped; it never stops the loop.
    """

    rules: Sequence[Rule]
    adapter: PlatformAdapter
    alerter: Alerter | None = None
    dry_run: bool = True

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        actions: list[Action] = []
        for rule in self.rules:
            try:
                actions.extend(rule.evaluate(snapshot))
            except Exception:
                log.exception("rule %s failed; skipped", rule.name)
        return actions

    def execute(self, actions: Sequence[Action]) -> None:
        for action in actions:
            if self.dry_run:
                log.info("dry-run: %s", action)
                continue
            try:
                self._dispatch(action)
            except Exception:
                log.exception("action failed; skipped: %s", action)

    def tick(self, snapshot_source: Callable[[], Snapshot]) -> list[Action]:
        actions = self.evaluate(snapshot_source())
        self.execute(actions)
        return actions

    def _dispatch(self, action: Action) -> None:
        if action.kind in _TASK_ACTIONS and not action.task_id:
            log.warning("%s without task_id; skipped: %s", action.kind, action)
            return
        tid = action.task_id or ""
        match action.kind:
            case ActionKind.REQUEUE | ActionKind.WAKE:
                # Waking a task means putting it back in the ready queue.
                self.adapter.requeue(tid, action.reason)
            case ActionKind.UNBLOCK:
                self.adapter.unblock(tid, action.reason)
            case ActionKind.COMMENT:
                self.adapter.comment(tid, action.reason)
            case ActionKind.WIP_COMMIT:
                self.adapter.wip_commit(tid, action.reason)
            case ActionKind.REASSIGN if action.params.get("owner_id") and isinstance(
                self.adapter, SupportsReassign
            ):
                self.adapter.reassign(tid, action.params["owner_id"], action.reason)
            case ActionKind.REASSIGN:
                # No target owner or adapter cannot reassign: ask a human instead.
                self._alert(action)
            case ActionKind.PAUSE if isinstance(self.adapter, SupportsPause):
                self.adapter.pause(tid, action.reason)
            case ActionKind.PAUSE:
                # Adapter cannot pause: a human has to stop the run.
                self._alert(action)
            case ActionKind.ALERT | ActionKind.ESCALATE:
                self._alert(action)

    def _alert(self, action: Action) -> None:
        if self.alerter is None:
            log.warning("no alerter configured; dropped: %s", action)
            return
        title = f"[{action.rule}] {action.kind}" + (f" {action.task_id}" if action.task_id else "")
        self.alerter.send(title, action.reason, action.severity)
