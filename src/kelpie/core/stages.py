"""Task stages and task roles, shared by the live gauges (``/metrics``) and the daily
report's throughput (:mod:`kelpie.report.throughput`). Pure, over the core model."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime

from kelpie.core.model import Owner, Session, Status, Task

STAGES = ("working", "queue", "review", "waiting", "ownerless")
UNKNOWN_ROLE = "unknown"
# The stage of a task outside a running session, by status. ``in_progress`` outside a
# session waits for its agent to run again; finished and backlog tasks have no stage.
STATUS_STAGES: Mapping[Status, str] = {
    Status.READY: "queue",
    Status.IN_PROGRESS: "queue",
    Status.REVIEW: "review",
    Status.ACCEPTED: "review",
    Status.BLOCKED: "waiting",
    Status.WAITING: "waiting",
}


def status_stage(task: Task, status: Status) -> str | None:
    """The stage of ``task`` in ``status`` with no session running: ``in_progress`` without
    an owner (nor anyone it is routed to) is ownerless."""
    if status is Status.IN_PROGRESS and task.owner_id is None and task.assignee is None:
        return "ownerless"
    return STATUS_STAGES.get(status)


def live_stage(task: Task, live: bool) -> str | None:
    """The stage of a task now: ``live`` when a session is running on it."""
    if task.status.is_terminal or task.status is Status.BACKLOG:
        return None
    return "working" if live else status_stage(task, task.status)


def task_roles(tasks: Iterable[Task], owners: Iterable[Owner]) -> dict[str, str]:
    """Each task's role: its own, else its owner's."""
    by_owner = {o.id: o.role for o in owners}
    out = {}
    for t in tasks:
        role = t.role or (by_owner.get(t.owner_id) if t.owner_id else None)
        if role:
            out[t.id] = role
    return out


def rework_role(
    task: Task,
    returned_at: datetime,
    sessions: Sequence[Session],
    role_of: Callable[[Session], str | None],
    review_roles: Iterable[str],
    end: datetime,
) -> str:
    """The role that redoes a task sent back at ``returned_at``: not the reviewer that sent
    it back, but the role of the first non-review session running at or starting after the
    return (by start), else of the last one before it, else the task's own role unless that
    is a review role (a task in review is often assigned to its reviewer by then); else
    :data:`UNKNOWN_ROLE`. ``role_of`` gives a session's role from its owner, never from the
    task (that would be the reviewer again); None skips the session. A session without an
    end runs until ``end``."""
    reviewers = set(review_roles)
    doers = sorted(
        (s.started_at, s.ended_at or s.last_heartbeat or end, role)
        for s in sessions
        if (role := role_of(s)) is not None and role not in reviewers
    )
    after = [role for start, stop, role in doers if stop > returned_at]
    if after:
        return after[0]
    if doers:
        return doers[-1][2]
    if task.role and task.role not in reviewers:
        return task.role
    return UNKNOWN_ROLE
