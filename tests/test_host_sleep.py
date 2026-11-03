"""Host sleep and wake (issue #296): the clock watch, awake ages and the rules that use them."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Action
from kelpie.core.engine import Engine
from kelpie.core.host_sleep import SleepWatch, merge_sleeps
from kelpie.core.model import (
    HostSleep,
    Owner,
    Session,
    Snapshot,
    Status,
    Task,
    slept,
    source_agent,
)
from kelpie.rules._tasks import awake, is_live, slept_since, task_awake
from kelpie.rules.checkpoints import Checkpoints
from kelpie.rules.orphan_status import OrphanStatus

T0 = datetime(2026, 3, 2, 11, tzinfo=UTC)
M = timedelta(minutes=1)
SLEEP = HostSleep(T0 + 24 * M, T0 + 71 * M)  # 11:24-12:11, 47 minutes


@dataclass
class Clocks:
    """A wall clock and a monotonic clock that stops while the host sleeps."""

    wall: datetime = T0
    mono: float = 1000.0

    def run(self, d: timedelta) -> None:
        self.wall += d
        self.mono += d.total_seconds()

    def sleep(self, d: timedelta) -> None:
        self.wall += d

    def monotonic(self) -> float:
        return self.mono


def test_watch_finds_a_suspend_from_the_clocks() -> None:
    clocks = Clocks()
    found: list[HostSleep] = []
    watch = SleepWatch(monotonic=clocks.monotonic, on_sleep=found.append)
    assert watch.observe(clocks.wall) is None  # the first tick has nothing to compare
    clocks.run(30 * timedelta(seconds=1))
    assert watch.observe(clocks.wall) is None
    clocks.run(10 * timedelta(seconds=1))
    clocks.sleep(47 * M)
    clocks.run(20 * timedelta(seconds=1))
    watch.mark()
    clocks.run(3 * timedelta(seconds=1))  # the source reads the wall clock a bit later
    sleep = watch.observe(clocks.wall - 3 * timedelta(seconds=1))
    assert sleep is not None and sleep.length == 47 * M and sleep.agent is None
    assert sleep.end == clocks.wall - 3 * timedelta(seconds=1)
    assert found == [sleep] and watch.sleeps(clocks.wall) == (sleep,)
    clocks.run(25 * timedelta(hours=1))
    watch.observe(clocks.wall)
    assert watch.sleeps(clocks.wall) == ()  # kept for a day


def test_watch_ignores_slow_ticks_small_steps_and_backward_jumps() -> None:
    clocks = Clocks()
    watch = SleepWatch(monotonic=clocks.monotonic, agent="host-b")
    watch.observe(clocks.wall)
    clocks.run(20 * M)  # a tick stuck in a slow adapter: both clocks ran
    assert watch.observe(clocks.wall) is None
    clocks.sleep(90 * timedelta(seconds=1))  # under min_gap (an NTP step, a short nap)
    assert watch.observe(clocks.wall) is None
    clocks.wall -= 10 * M  # the wall clock set back
    assert watch.observe(clocks.wall) is None
    clocks.sleep(5 * M)
    sleep = watch.observe(clocks.wall)
    assert sleep is not None and sleep.agent == "host-b"
    with pytest.raises(ValueError):
        SleepWatch(min_gap=timedelta(0))


def test_watch_keeps_a_sleep_once_and_survives_a_failing_recorder() -> None:
    def boom(_: HostSleep) -> None:
        raise RuntimeError("disk full")

    watch = SleepWatch(on_sleep=boom)
    watch.add(SLEEP)
    watch.add(SLEEP)
    assert watch.sleeps(SLEEP.end) == (SLEEP,)


def test_slept_counts_overlaps_once_per_host() -> None:
    other = HostSleep(T0 + 30 * M, T0 + 80 * M, agent="host-b")
    copy = HostSleep(T0 + 60 * M, T0 + 75 * M, source="wake-log")
    sleeps = (SLEEP, other, copy)
    assert slept(sleeps, T0, T0 + 2 * timedelta(hours=1), None) == 51 * M
    assert slept(sleeps, T0 + 60 * M, T0 + 65 * M, None) == 5 * M
    assert slept(sleeps, T0, T0 + 10 * M, None) == timedelta(0)
    assert slept(sleeps, T0, T0 + 2 * timedelta(hours=1), "host-b") == 50 * M
    assert merge_sleeps([SLEEP, other], [SLEEP]) == (SLEEP, other)
    assert source_agent("host-b/hermes") == "host-b"
    assert source_agent("hermes") is None and source_agent(None) is None


def test_awake_age_and_liveness() -> None:
    now = T0 + 73 * M
    beat = T0 + 23 * M
    assert awake(beat, now, (SLEEP,)) == 3 * M
    assert awake(beat, now, (SLEEP,), "host-b") == 50 * M  # another host's sleep
    assert awake(now, beat, (SLEEP,)) == beat - now  # no sleep counted backwards
    s = Session("S-1", "T-1", "dev-1", T0, last_heartbeat=beat)
    assert not is_live(s, now)
    assert is_live(s, now, sleeps=(SLEEP,))
    remote = Session("h:S-1", "h:T-1", "h:dev-1", T0, last_heartbeat=beat, source="h/hermes")
    assert not is_live(remote, now, sleeps=(SLEEP,))  # the server slept, not host h


def test_slept_since_only_for_a_session_that_was_not_over_before_the_sleep() -> None:
    snap = Snapshot(now=T0 + 110 * M, host_sleeps=(SLEEP,))
    quiet = Session("S-1", "T-1", "dev-1", T0, last_heartbeat=T0 + 23 * M)
    long_gone = Session("S-2", "T-2", "dev-1", T0, last_heartbeat=T0 - 60 * M)
    assert slept_since(quiet, snap, 30 * M) == SLEEP
    assert slept_since(long_gone, snap, 30 * M) is None  # stale an hour before the sleep
    assert slept_since(Session("S-3", "T-3", "d", T0 + 80 * M), snap, 30 * M) is None


def _ownerless(status_since: datetime) -> Task:
    return Task("T-9", "Ownerless", Status.IN_PROGRESS, status_since, role="developer")


def test_orphan_age_counts_awake_time_only() -> None:
    rule = OrphanStatus(orphan_after=timedelta(minutes=40))
    task = _ownerless(T0 + 20 * M)
    now = T0 + 75 * M  # 55 minutes on the wall clock, 8 awake
    asleep = Snapshot(now=now, tasks=(task,), host_sleeps=(SLEEP,))
    assert rule.evaluate(asleep) == []
    stalled = Snapshot(now=now, tasks=(task,))
    assert any(a.task_id == "T-9" for a in rule.evaluate(stalled))
    assert task_awake(task, task.status_since, asleep) == 8 * M


def test_commit_reminder_counts_awake_time_only() -> None:
    rule = Checkpoints(commit_every=timedelta(minutes=45))
    task = Task("T-5", "Feature", Status.IN_PROGRESS, T0, owner_id="dev-1")
    session = Session(
        "S-5",
        "T-5",
        "dev-1",
        T0,
        last_heartbeat=T0 + 74 * M,
        last_commit_at=T0 + 10 * M,
        dirty_worktree=True,
    )
    owners = (Owner("dev-1", role="developer"),)
    now = T0 + 75 * M  # 65 minutes since the commit, 18 of them awake
    base = Snapshot(now=now, tasks=(task,), sessions=(session,), owners=owners)
    assert any(a.task_id == "T-5" for a in rule.evaluate(base))
    slept_snap = Snapshot(
        now=now, tasks=(task,), sessions=(session,), owners=owners, host_sleeps=(SLEEP,)
    )
    assert not any(a.task_id == "T-5" for a in rule.evaluate(slept_snap))


@dataclass
class Seen:
    name: str = "seen"
    snapshots: list[Snapshot] = field(default_factory=list)

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        self.snapshots.append(snapshot)
        return []


def test_engine_adds_its_sleeps_to_the_snapshot() -> None:
    clocks = Clocks()
    rule = Seen()
    remote = HostSleep(T0, T0 + 5 * M, agent="host-b")
    engine = Engine([rule], sleep_watch=SleepWatch(monotonic=clocks.monotonic))
    engine.tick(lambda: Snapshot(now=clocks.wall, host_sleeps=(remote,)))
    clocks.run(30 * timedelta(seconds=1))
    clocks.sleep(47 * M)
    engine.tick(lambda: Snapshot(now=clocks.wall, host_sleeps=(remote,)))
    first, second = rule.snapshots
    assert first.host_sleeps == (remote,)
    assert len(second.host_sleeps) == 2 and second.host_sleeps[0] == remote
    mine = second.host_sleeps[1]
    assert mine.agent is None and mine.length == 47 * M and mine.end == clocks.wall
