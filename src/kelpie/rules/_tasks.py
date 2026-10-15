"""Shared task semantics for rules, so that every rule answers the same questions alike:
which conditions belong to a task right now, and is anyone still working on it."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from kelpie.core.conditions import linked_conditions
from kelpie.core.model import Condition, ConditionKind, Session, Snapshot, Status, Task

__all__ = ["linked_conditions"]  # defined in kelpie.core.conditions, re-exported for rules

# Condition data older than this is not evidence: the connector may have stopped polling.
DEFAULT_MAX_CONDITION_AGE = timedelta(minutes=15)

# An open session that has not sent a heartbeat for this long is treated as gone: crashed
# sessions are exactly the ones that never report their end.
DEFAULT_STALE_AFTER = timedelta(minutes=30)


def last_seen(session: Session) -> datetime:
    """The last sign of life of a session."""
    return session.ended_at or session.last_heartbeat or session.started_at


def is_live(session: Session, now: datetime, stale_after: timedelta = DEFAULT_STALE_AFTER) -> bool:
    return session.ended_at is None and now - last_seen(session) < stale_after


def live_session(
    task_id: str, snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> Session | None:
    """A session currently working on the task, if any."""
    return next(
        (s for s in snapshot.sessions_for(task_id) if is_live(s, snapshot.now, stale_after)),
        None,
    )


def fresh(
    condition: Condition,
    since: datetime,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_CONDITION_AGE,
) -> bool:
    """Whether the condition was checked after ``since`` and recently enough to act on."""
    checked = condition.checked_at
    return checked is not None and checked >= since and now - checked <= max_age


def dead_ends(conditions: Iterable[Condition], since: datetime) -> list[Condition]:
    """Unmet conditions flagged ``params.terminal`` (they can never be met), seen since ``since``.

    A connector sets the flag when what the condition waits for ended the wrong way: a PR
    closed without merging, a human's task failed or cancelled. Seen means checked after the
    wait began; age aside, so the alert holds while the connector is slow to check again.
    A condition flagged ``params.terminal_ok`` is not a dead end: the rule that registered it
    releases the task when its target ends (rule 12).
    """
    return [
        c
        for c in conditions
        if not c.satisfied
        and c.params.get("terminal") is True
        and c.params.get("terminal_ok") is not True
        and c.checked_at is not None
        and c.checked_at >= since
    ]


def dead_end_text(conditions: Iterable[Condition]) -> str:
    """What ended wrong, for an alert."""
    parts: list[str] = []
    for c in conditions:
        if c.kind is ConditionKind.PR_MERGED:
            parts.append(f"the PR was closed without merging ({c.id})")
        elif c.kind is ConditionKind.HUMAN_TASK:
            parts.append(
                f"the human task {c.params.get('task')} ended as {c.params.get('status')} ({c.id})"
            )
        else:
            parts.append(f"{c.id} ({c.kind}) can no longer be met")
    return "; ".join(parts)


# Statuses a session can declare as its end. in_progress/ready/backlog are not an end.
DECLARED = frozenset(
    {
        Status.DONE,
        Status.FAILED,
        Status.CANCELLED,
        Status.REVIEW,
        Status.ACCEPTED,
        Status.BLOCKED,
        Status.WAITING,
    }
)
HANDOFF = frozenset({Status.REVIEW, Status.ACCEPTED})  # queues for the next stage


@dataclass(frozen=True, slots=True)
class Exit:
    session: Session
    at: datetime  # ended_at, or the last sign of life of a silent session
    silent: bool


def last_exit(
    task: Task, snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> Exit | None:
    """The task's most recent session if it is over; None if a session is live or none ran."""
    exits: list[Exit] = []
    for s in snapshot.sessions_for(task.id):
        if is_live(s, snapshot.now, stale_after):
            return None
        exits.append(Exit(s, last_seen(s), silent=s.ended_at is None))
    return max(exits, key=lambda e: (e.at, e.session.started_at), default=None)


def undeclared_exit(
    task: Task, snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> Exit | None:
    """The exit that left a working task without a declared end (rule 1's business).

    Applies to in_progress, review and accepted tasks: the last session is over, nobody
    changed the task since, and the session neither declared a real end nor handed the
    task on to review/accepted during its run.
    """
    if task.status not in {Status.IN_PROGRESS, *HANDOFF}:
        return None
    end = last_exit(task, snapshot, stale_after)
    if end is None or task.status_since > end.at:
        return None
    if end.session.final_status in DECLARED:
        return None
    if task.status in HANDOFF and task.status_since >= end.session.started_at:
        return None  # handed on during the session: a declared end
    return end
