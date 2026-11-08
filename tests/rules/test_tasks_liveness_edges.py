"""Liveness helpers in ``kelpie.rules._tasks`` that mutation testing found untested: on a
multi-host farm a sleep counts only for the host that slept, and a session or task of a
``kelpie agent`` is aged on that agent's clock; boundaries are inclusive as documented."""

from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.model import Condition, ConditionKind, HostSleep, Session, Snapshot, Status, Task
from kelpie.rules._tasks import (
    awake,
    fresh,
    is_live,
    last_exit,
    live_session,
    slept_since,
    task_awake,
    undeclared_exit,
)

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
H = timedelta(hours=1)
LAPTOP = HostSleep(NOW - 50 * M, NOW - 10 * M, agent="laptop")


def remote(sid: str = "S-1", heard: datetime = NOW - 55 * M, **kw: Any) -> Session:
    return Session(
        sid, "T-1", "laptop:dev-1", NOW - 2 * H, last_heartbeat=heard, source="laptop/hermes", **kw
    )


def test_a_sleep_counts_only_for_its_host() -> None:
    assert awake(NOW - H, NOW, [LAPTOP], "laptop") == 20 * M
    assert awake(NOW - H, NOW, [LAPTOP], None) == H
    assert awake(NOW - H, NOW, [LAPTOP], "desk") == H


def test_a_remote_session_is_aged_on_its_hosts_clock() -> None:
    """Heard 55 minutes ago, 40 of them asleep: 15 awake minutes, live."""
    assert is_live(remote(), NOW, 30 * M, [LAPTOP])
    assert not is_live(remote(), NOW, 30 * M, [HostSleep(LAPTOP.start, LAPTOP.end, agent=None)])
    assert not is_live(remote(), NOW, 30 * M, ())


def test_live_session_and_last_exit_read_the_sleeps() -> None:
    snap = Snapshot(
        now=NOW,
        tasks=(Task("T-1", "t", Status.IN_PROGRESS, NOW - 2 * H, source="laptop/hermes"),),
        sessions=(remote(),),
        host_sleeps=(LAPTOP,),
    )
    assert live_session("T-1", snap) is not None
    assert last_exit(snap.tasks[0], snap) is None
    assert undeclared_exit(snap.tasks[0], snap) is None


def test_a_remote_task_is_aged_on_its_hosts_clock() -> None:
    task = Task("T-1", "t", Status.IN_PROGRESS, NOW - 2 * H, source="laptop/hermes")
    snap = Snapshot(now=NOW, tasks=(task,), host_sleeps=(LAPTOP,))
    assert task_awake(task, NOW - H, snap) == 20 * M


def test_awake_with_sleeps_from_now_on_is_zero() -> None:
    assert awake(NOW, NOW, [LAPTOP], "laptop") == timedelta(0)


def test_slept_since_takes_the_first_sleep_after_the_last_sign() -> None:
    """Two sleeps given out of order: the one right after the session went quiet is it."""
    first = HostSleep(NOW - 50 * M, NOW - 40 * M, agent="laptop")
    second = HostSleep(NOW - 30 * M, NOW - 20 * M, agent="laptop")
    snap = Snapshot(now=NOW, host_sleeps=(second, first))
    assert slept_since(remote(heard=NOW - 55 * M), snap, 10 * M) == first


def test_slept_since_bar_is_exclusive_and_an_earlier_sleep_is_not_it() -> None:
    sleep = HostSleep(NOW - 45 * M, NOW - 20 * M, agent="laptop")
    snap = Snapshot(now=NOW, host_sleeps=(sleep,))
    assert slept_since(remote(heard=NOW - 55 * M), snap, 11 * M) == sleep
    assert slept_since(remote(heard=NOW - 55 * M), snap, 10 * M) is None  # 10 min awake first
    assert slept_since(remote(heard=NOW - 20 * M), snap, H) is None  # ended as it was heard


def test_fresh_from_the_moment_it_began() -> None:
    since = NOW - H
    checked = Condition("c", ConditionKind.CUSTOM, checked_at=since)
    assert fresh(checked, since, NOW, 2 * H)
    assert fresh(Condition("c", ConditionKind.CUSTOM, checked_at=NOW - H), since, NOW, H)
    assert not fresh(
        Condition("c", ConditionKind.CUSTOM, checked_at=NOW - H - M), since, NOW, 2 * H
    )


def _exit(status: Status, since: datetime, end: datetime, started: datetime) -> Snapshot:
    session = Session("S-1", "T-1", "dev-1", started, ended_at=end)
    return Snapshot(now=NOW, tasks=(Task("T-1", "t", status, since),), sessions=(session,))


def test_a_task_changed_at_the_exit_is_still_the_exits() -> None:
    snap = _exit(Status.IN_PROGRESS, NOW - H, NOW - H, NOW - 2 * H)
    assert undeclared_exit(snap.tasks[0], snap) is not None


def test_a_handoff_at_the_run_start_is_the_runs_own() -> None:
    """Moved to review the moment the run began: the run handed it on, no undeclared exit."""
    snap = _exit(Status.REVIEW, NOW - 2 * H, NOW - H, NOW - 2 * H)
    assert undeclared_exit(snap.tasks[0], snap) is None
