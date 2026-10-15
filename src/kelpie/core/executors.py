"""Executors carry out actions. The engine routes each action to one of them by name."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kelpie.core.actions import Action, ActionKind
from kelpie.core.ports import ActionConnector, PlatformAdapter, SupportsReassign, SupportsStop


class Executor(Protocol):
    @property
    def name(self) -> str: ...

    def execute(self, action: Action) -> bool:
        """Carry out ``action``. Return False if this executor cannot do it."""
        ...


@dataclass(slots=True)
class PlatformExecutor:
    """Maps task actions onto a :class:`PlatformAdapter` and its optional capabilities."""

    adapter: PlatformAdapter

    @property
    def name(self) -> str:
        return self.adapter.name

    def execute(self, action: Action) -> bool:
        adapter = self.adapter
        if action.kind is ActionKind.STOP:
            # Stops target a session; heartbeat loops may have no task at all.
            session_id = action.params.get("session_id")
            if not session_id or not isinstance(adapter, SupportsStop):
                return False
            adapter.stop(session_id, action.reason)
            return True
        tid = action.task_id
        if tid is None:
            return False
        match action.kind:
            case ActionKind.REQUEUE | ActionKind.WAKE:
                # Waking a task means putting it back in the ready queue.
                adapter.requeue(tid, action.reason)
            case ActionKind.UNBLOCK:
                adapter.unblock(tid, action.reason)
            case ActionKind.COMMENT:
                adapter.comment(tid, action.reason)
            case ActionKind.WIP_COMMIT:
                adapter.wip_commit(tid, action.reason)
            case ActionKind.REASSIGN if action.params.get("owner_id") and isinstance(
                adapter, SupportsReassign
            ):
                adapter.reassign(tid, action.params["owner_id"], action.reason)
            case _:
                return False
        return True


@dataclass(slots=True)
class ConnectorExecutor:
    """Runs the actions a connector declares in ``actions``."""

    connector: ActionConnector

    @property
    def name(self) -> str:
        return self.connector.name

    def execute(self, action: Action) -> bool:
        if action.kind not in self.connector.actions:
            return False
        self.connector.execute(action)
        return True
