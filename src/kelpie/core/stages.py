"""Task stages and task roles, shared by the live gauges (``/metrics``) and the daily
report's throughput (:mod:`kelpie.report.throughput`). Pure, over the core model."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from kelpie.core.model import Owner, Status, Task

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
    an owner is ownerless."""
    if status is Status.IN_PROGRESS and task.owner_id is None:
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
