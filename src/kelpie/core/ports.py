"""Protocols that integrations implement. Core depends on these, never on implementations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from kelpie.core.actions import Action, ActionKind, ActionResult
from kelpie.core.model import (
    Condition,
    ConditionKind,
    LimitHit,
    ProviderStatus,
    Resource,
    Session,
    Snapshot,
    Status,
    Task,
)


@runtime_checkable
class PlatformAdapter(Protocol):
    """Bridge to an agent platform (Hermes, OpenClaw, Paperclip, Claude Code, ...).

    Contract for ``waiting`` tasks (rule 2): the agent registered a condition and exited on
    purpose. The adapter reports such tasks to the platform's dispatcher as waiting and
    alive, never as dead or abandoned, until Kelpie wakes them.
    """

    name: str

    def list_tasks(self) -> Sequence[Task]: ...

    def get_task(self, task_id: str) -> Task | None: ...

    def requeue(self, task_id: str, reason: str) -> None:
        """Unassign the task and put it back in the ready queue of its current stage (a task
        in review goes back to the review queue, not to development). The adapter clears
        the owner and resets ``status_since`` to the time of the requeue, so rules see a
        fresh state rather than the abandoned one."""
        ...

    def unblock(self, task_id: str, reason: str) -> None: ...

    def comment(self, task_id: str, body: str) -> None: ...

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        """Commit and push everything uncommitted in the task's worktree, on ``branch`` when
        given. With ``draft_pr`` the push goes to a draft PR (which does not run CI) instead
        of relying on ``[skip ci]`` in the message. Afterwards the adapter reports the new
        commit as the session's ``last_commit``, so the next session can resume from it."""
        ...

    def events(self, since: datetime) -> Iterable[PlatformEvent]: ...


@runtime_checkable
class SnapshotSource(Protocol):
    """Optional adapter capability: describe the board as of ``now``.

    A platform adapter that can describe its board: tasks, owners, dependencies, sessions
    and spans as of ``now``. The caller owns the clock; the adapter never reads it.
    """

    def snapshot(self, now: datetime) -> Snapshot: ...


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

    def execute(self, action: Action) -> ActionResult | bool | None:
        """Carry out ``action``.

        ``REFUSED`` (or False): not allowed by the connector's config, or not a target it
        knows; a human is told instead. ``NOTHING_TO_DO``: the target no longer needs it.
        ``DONE``, True or None: carried out. A transient failure (network, rate limit, a
        state that could not be re-checked) raises, so the engine records a failure and
        the ledger retries it later."""
        ...


@runtime_checkable
class ResourceReader(Protocol):
    """Reads host resource readings (disk, memory, load, processes) for a snapshot.

    Must not raise: when the source is unavailable it returns no readings, so rules see no
    data rather than stale data. The caller owns the clock.
    """

    def read(self, now: datetime) -> Sequence[Resource]: ...


@runtime_checkable
class ProviderReader(Protocol):
    """Reads LLM provider quota and auth state for a snapshot: one status per provider and
    the limit hits seen so far (kept for a while, so budgets can forecast the next one).

    Must not raise: a provider that cannot be read is reported with ``ok`` False. The
    caller owns the clock.
    """

    def read(self, now: datetime) -> tuple[Sequence[ProviderStatus], Sequence[LimitHit]]: ...


@runtime_checkable
class Alerter(Protocol):
    def send(self, title: str, body: str, severity: str) -> None: ...


class PlatformEvent(Protocol):
    """A platform-side event (session start/end, status change, tool call)."""

    at: datetime
    task_id: str | None
    session: Session | None
    status: Status | None
