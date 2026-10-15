"""Shared task semantics for rules, so that every rule answers the same questions alike:
which conditions belong to a task right now, and is anyone still working on it."""

from __future__ import annotations

from datetime import datetime, timedelta

from kelpie.core.model import Condition, Session, Snapshot, Task

# An open session that has not sent a heartbeat for this long is treated as gone: crashed
# sessions are exactly the ones that never report their end.
DEFAULT_STALE_AFTER = timedelta(hours=2)


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


def linked_conditions(task: Task, snapshot: Snapshot) -> tuple[list[Condition], str | None]:
    """Conditions of the task's current block or wait, and a referenced id Kelpie cannot see.

    The condition named by the block wins. Otherwise conditions linked by ``task_id`` count,
    except those registered before the task entered its current status (left over from an
    earlier wait). Returns ``([], missing_id)`` when the block names an unknown condition.
    """
    if task.block is not None and task.block.condition_id:
        c = snapshot.condition(task.block.condition_id)
        return ([c], None) if c is not None else ([], task.block.condition_id)
    current = [
        c
        for c in snapshot.conditions
        if c.task_id == task.id
        and (c.registered_at is None or c.registered_at >= task.status_since)
    ]
    return sorted(current, key=lambda c: c.id), None
