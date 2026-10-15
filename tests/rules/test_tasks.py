from datetime import UTC, datetime, timedelta

from kelpie.core.model import Block, Condition, ConditionKind, Session, Snapshot, Status, Task
from kelpie.rules._tasks import (
    is_live,
    last_exit,
    last_seen,
    linked_conditions,
    live_session,
    undeclared_exit,
)

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
H = timedelta(hours=1)


def test_liveness() -> None:
    open_fresh = Session("a", "T-1", "x", NOW - 3 * H, last_heartbeat=NOW - 0.5 * H)
    open_stale = Session("b", "T-1", "x", NOW - 3 * H, last_heartbeat=NOW - 2 * H)
    no_beat = Session("c", "T-1", "x", NOW - 1 * H)
    ended = Session("d", "T-1", "x", NOW - 3 * H, ended_at=NOW - H, last_heartbeat=NOW - 2 * H)
    assert is_live(open_fresh, NOW) and is_live(no_beat, NOW)
    assert not is_live(open_stale, NOW) and not is_live(ended, NOW)
    assert last_seen(ended) == NOW - H and last_seen(no_beat) == NOW - H
    snap = Snapshot(now=NOW, sessions=(open_stale, ended, open_fresh))
    assert live_session("T-1", snap) == open_fresh
    assert live_session("T-2", snap) is None


def cond(cid: str, task_id: str | None = "T-1", registered: datetime | None = None) -> Condition:
    return Condition(cid, ConditionKind.CI_STATUS, task_id=task_id, registered_at=registered)


def test_linked_conditions() -> None:
    task = Task("T-1", "t", Status.BLOCKED, NOW - H)
    old, new, other = (
        cond("old", registered=NOW - 3 * H),
        cond("new", registered=NOW - 0.5 * H),
        cond("x", "T-2"),
    )
    snap = Snapshot(now=NOW, tasks=(task,), conditions=(new, old, other, cond("unknown-age")))
    assert linked_conditions(task, snap) == ([new, cond("unknown-age")], None)
    by_block = Task("T-1", "t", Status.BLOCKED, NOW - H, block=Block("guard", NOW - H, "x"))
    assert linked_conditions(by_block, snap) == ([other], None)
    dangling = Task("T-1", "t", Status.BLOCKED, NOW - H, block=Block("guard", NOW - H, "gone"))
    assert linked_conditions(dangling, snap) == ([], "gone")


def test_undeclared_exit() -> None:
    def t(status: Status, since: datetime) -> Task:
        return Task("T-1", "t", status, since)

    ended = Session("S", "T-1", "a", NOW - 3 * H, ended_at=NOW - H)
    snap = lambda task, *ss: Snapshot(now=NOW, tasks=(task,), sessions=ss)  # noqa: E731
    assert (
        undeclared_exit(t(Status.IN_PROGRESS, NOW - 4 * H), snap(t(Status.IN_PROGRESS, NOW), ended))
        is not None
    )
    # Handed to review during the session: declared.
    task = t(Status.REVIEW, NOW - 2 * H)
    assert undeclared_exit(task, snap(task, ended)) is None
    # Review that predates the session: the reviewer left without a verdict.
    task = t(Status.REVIEW, NOW - 4 * H)
    assert undeclared_exit(task, snap(task, ended)) is not None
    # Declared final status, changed since, other statuses, no sessions.
    declared = Session("S", "T-1", "a", NOW - 3 * H, ended_at=NOW - H, final_status=Status.DONE)
    task = t(Status.IN_PROGRESS, NOW - 4 * H)
    assert undeclared_exit(task, snap(task, declared)) is None
    task = t(Status.IN_PROGRESS, NOW - 0.5 * H)
    assert undeclared_exit(task, snap(task, ended)) is None
    task = t(Status.BLOCKED, NOW - 4 * H)
    assert undeclared_exit(task, snap(task, ended)) is None
    task = t(Status.IN_PROGRESS, NOW - 4 * H)
    assert undeclared_exit(task, snap(task)) is None and last_exit(task, snap(task)) is None
