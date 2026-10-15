"""Protocols that integrations implement. Core depends on these, never on implementations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Condition, ConditionKind, Session, Status, Task


@runtime_checkable
class PlatformAdapter(Protocol):
    """Bridge to an agent platform (Hermes, OpenClaw, Paperclip, Claude Code, ...)."""

    name: str

    def list_tasks(self) -> Sequence[Task]: ...

    def get_task(self, task_id: str) -> Task | None: ...

    def requeue(self, task_id: str, reason: str) -> None:
        """Unassign the task and put it back in the ready queue of its current stage
        (a task in review goes back to the review queue, not to development)."""
        ...

    def unblock(self, task_id: str, reason: str) -> None: ...

    def comment(self, task_id: str, body: str) -> None: ...

    def wip_commit(self, task_id: str, message: str) -> None: ...

    def events(self, since: datetime) -> Iterable[PlatformEvent]: ...


@runtime_checkable
class SupportsReassign(Protocol):
    """Optional adapter capability: hand a task to another owner."""

    def reassign(self, task_id: str, owner_id: str, reason: str) -> None: ...


@runtime_checkable
class SupportsStop(Protocol):
    """Optional adapter capability: stop (pause) a running session.

    Works for sessions without a task (e.g. heartbeat loops). The task, if any, keeps its
    status; a human or a later action resumes it.
    """

    def stop(self, session_id: str, reason: str) -> None: ...


@runtime_checkable
class ConditionConnector(Protocol):
    """Evaluates external conditions: GitHub PR/CI, LLM provider quotas, human tasks."""

    name: str
    kinds: frozenset[ConditionKind]

    def check(self, condition: Condition) -> Condition: ...


@runtime_checkable
class ActionConnector(Protocol):
    """A connector that can also carry out actions, e.g. ``cancel_build`` for a CI system."""

    name: str
    actions: frozenset[ActionKind]

    def execute(self, action: Action) -> None: ...


@runtime_checkable
class Alerter(Protocol):
    def send(self, title: str, body: str, severity: str) -> None: ...


class PlatformEvent(Protocol):
    """A platform-side event (session start/end, status change, tool call)."""

    at: datetime
    task_id: str | None
    session: Session | None
    status: Status | None
