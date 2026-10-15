"""Hermes platform adapter (stub).

Reads tasks from the Hermes Kanban SQLite database and receives session events from
a Hermes plugin registered on the ``post_tool_call`` and ``on_session_end`` hooks.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from kelpie.core.model import Task
from kelpie.core.ports import PlatformEvent

DEFAULT_DB = Path("~/.hermes/kanban.db")


@dataclass(slots=True)
class HermesAdapter:
    db_path: Path = DEFAULT_DB
    name: str = "hermes"

    def list_tasks(self) -> Sequence[Task]:
        raise NotImplementedError

    def get_task(self, task_id: str) -> Task | None:
        raise NotImplementedError

    def requeue(self, task_id: str, reason: str) -> None:
        raise NotImplementedError

    def unblock(self, task_id: str, reason: str) -> None:
        raise NotImplementedError

    def comment(self, task_id: str, body: str) -> None:
        raise NotImplementedError

    def wip_commit(self, task_id: str, message: str) -> None:
        raise NotImplementedError

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        raise NotImplementedError
