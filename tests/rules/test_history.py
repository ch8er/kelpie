from dataclasses import replace
from datetime import UTC, datetime, timedelta

from kelpie.core.config import EngineConfig
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot, Status, StatusStep, Task
from kelpie.core.serde import snapshot_from_dict
from kelpie.core.transitions import MemoryTransitionStore, StatusChange, TransitionRecorder
from kelpie.rules._history import bounces, time_to_accept
from tests.fakes import FakeAdapter, FakeAlerter

T0 = datetime(2026, 3, 2, 9, tzinfo=UTC)
H = timedelta(hours=1)
S = Status


def steps(*moves: tuple[float, Status]) -> tuple[StatusStep, ...]:
    out: list[StatusStep] = []
    previous: Status | None = None
    for hours, status in moves:
        out.append(StatusStep("T-1", T0 + hours * H, status, previous))
        previous = status
    return tuple(out)


def test_bounces_count_returns_from_review_or_done() -> None:
    assert bounces(()) == 0
    assert bounces(steps((0, S.READY), (1, S.IN_PROGRESS), (2, S.REVIEW), (3, S.DONE))) == 0
    back = steps(
        (0, S.IN_PROGRESS),
        (1, S.REVIEW),
        (2, S.IN_PROGRESS),
        (3, S.REVIEW),
        (4, S.DONE),
        (5, S.READY),  # reopened after done
    )
    assert bounces(back) == 2  # review -> in progress, done -> ready
    # Blocked and back is no bounce.
    assert bounces(steps((0, S.IN_PROGRESS), (1, S.BLOCKED), (2, S.IN_PROGRESS))) == 0


def test_time_to_accept() -> None:
    assert time_to_accept(steps((0, S.READY), (2.5, S.IN_PROGRESS))) == 2.5 * H
    assert time_to_accept(steps((0, S.BACKLOG), (1, S.READY), (4, S.IN_PROGRESS))) == 3 * H
    assert time_to_accept(steps((0, S.READY))) is None  # still waiting
    assert time_to_accept(steps((0, S.IN_PROGRESS), (1, S.REVIEW))) is None  # never ready
    assert time_to_accept(()) is None


def task(status: Status, since: datetime, tid: str = "T-1") -> Task:
    return Task(tid, "synthetic", status, since)


def test_the_engine_gives_rules_the_recorded_history() -> None:
    seen: list[tuple[StatusStep, ...]] = []

    class Spy:
        name = "spy"

        def evaluate(self, snapshot: Snapshot) -> list[object]:
            seen.append(snapshot.history_of("T-1"))
            return []

    store = MemoryTransitionStore()
    engine = Engine(
        [Spy()],  # type: ignore[list-item]
        adapters=[FakeAdapter()],
        alerter=FakeAlerter(),
        config=EngineConfig(dry_run=True),
        transitions=TransitionRecorder(store),
    )
    moves = [(0, S.READY), (1, S.IN_PROGRESS), (2, S.REVIEW), (3, S.IN_PROGRESS)]
    for hours, status in moves:
        at = T0 + hours * H
        engine.tick(lambda at=at, status=status: Snapshot(now=at, tasks=(task(status, at),)))
    history = seen[-1]
    assert [s.status for s in history] == [m[1] for m in moves]
    assert bounces(history) == 1 and time_to_accept(history) == H
    # A restarted engine reads the recorded history back from the store.
    seen.clear()
    restarted = Engine(
        [Spy()],  # type: ignore[list-item]
        adapters=[FakeAdapter()],
        alerter=FakeAlerter(),
        config=EngineConfig(dry_run=True),
        transitions=TransitionRecorder(store),
    )
    at = T0 + 4 * H
    restarted.tick(lambda: Snapshot(now=at, tasks=(task(S.IN_PROGRESS, T0 + 3 * H),)))
    assert [s.status for s in seen[-1]] == [m[1] for m in moves]
    # A source's own history (a fixture) is kept as it is.
    own = (StatusStep("T-1", T0, S.DONE),)
    restarted.tick(lambda: Snapshot(now=at + H, tasks=(task(S.DONE, at),), status_history=own))
    assert seen[-1] == own


def test_the_window_and_the_cap() -> None:
    recorder = TransitionRecorder(MemoryTransitionStore(), window=timedelta(days=1), max_steps=3)
    status = [S.IN_PROGRESS, S.REVIEW]
    for k in range(10):
        at = T0 + k * H
        recorder.record(Snapshot(now=at, tasks=(task(status[k % 2], at),)))
    assert len(recorder.steps(T0 + 9 * H)) == 3  # at most max_steps per task
    # Two days later nothing changed within the window: the task has no history.
    assert recorder.steps(T0 + timedelta(days=2)) == ()
    # The last change before the window is kept when there is one inside it.
    recorder = TransitionRecorder(MemoryTransitionStore(), window=timedelta(hours=2))
    for hours, status_ in ((0, S.READY), (5, S.IN_PROGRESS)):
        at = T0 + hours * H
        recorder.record(Snapshot(now=at, tasks=(task(status_, at),)))
    assert [s.status for s in recorder.steps(T0 + 5 * H)] == [S.READY, S.IN_PROGRESS]


def test_fixtures_can_carry_a_history() -> None:
    snap = snapshot_from_dict(
        {
            "now": "2026-03-02T12:00:00+00:00",
            "status_history": [
                {"task_id": "T-1", "at": "2026-03-02T10:00:00+00:00", "status": "ready"},
                {
                    "task_id": "T-1",
                    "at": "2026-03-02T11:00:00+00:00",
                    "status": "in_progress",
                    "previous": "ready",
                },
            ],
        }
    )
    assert time_to_accept(snap.history_of("T-1")) == H
    assert snap.history_of("T-2") == ()
    assert replace(snap, status_history=()).history_of("T-1") == ()


def test_live_history_matches_a_restart_and_keeps_the_status_before_the_window() -> None:
    """Ticks every 30 minutes: a task ready for longer than the window keeps its ready
    step, live as after a restart (the slow pickups are the ones worth seeing)."""
    window = timedelta(hours=2)
    store = MemoryTransitionStore()
    live = TransitionRecorder(store, window=window)
    for half_hours in range(11):  # ready from 0h, picked up at 5h
        at = T0 + half_hours * H / 2
        status = S.READY if half_hours < 10 else S.IN_PROGRESS
        live.record(Snapshot(now=at, tasks=(task(status, T0 if status is S.READY else at),)))
    now = T0 + 5 * H
    seen = live.steps(now)
    assert [s.status for s in seen] == [S.READY, S.IN_PROGRESS]
    assert time_to_accept(seen) == 5 * H
    restarted = TransitionRecorder(store, window=window)
    restarted.record(Snapshot(now=now + H / 2, tasks=(task(S.IN_PROGRESS, now),)))
    assert restarted.steps(now + H / 2) == seen
    # A first sighting older than the window: no history until something changes.
    old = TransitionRecorder(MemoryTransitionStore(), window=window)
    old.record(Snapshot(now=now, tasks=(task(S.READY, now - 10 * H),)))
    assert old.steps(now) == ()
    old.record(Snapshot(now=now + H, tasks=(task(S.IN_PROGRESS, now + H),)))
    assert time_to_accept(old.steps(now + H)) == 11 * H


def test_a_failed_first_read_is_tried_again() -> None:
    class Flaky(MemoryTransitionStore):
        fail = True

        def transitions(self, start: datetime, end: datetime) -> list:  # type: ignore[override,type-arg]
            if self.fail:
                raise OSError("locked")
            return super().transitions(start, end)

    store = Flaky()
    store.rows.append(StatusChange(T0, "T-1", S.READY, T0))
    recorder = TransitionRecorder(store)
    recorder.record(Snapshot(now=T0 + H, tasks=(task(S.READY, T0),)))
    assert recorder.steps(T0 + H) == ()
    store.fail = False
    recorder.record(Snapshot(now=T0 + 2 * H, tasks=(task(S.IN_PROGRESS, T0 + 2 * H),)))
    assert [s.status for s in recorder.steps(T0 + 2 * H)] == [S.READY, S.IN_PROGRESS]


def test_live_and_restarted_history_agree_after_a_busy_spell() -> None:
    """A task that changed several times and then stayed put, read later inside the
    window: live gives what a restart reads from the store."""
    window = timedelta(hours=10)
    store = MemoryTransitionStore()
    live = TransitionRecorder(store, window=window)
    for hour in range(14):
        at = T0 + hour * H
        status = (S.IN_PROGRESS, S.REVIEW)[min(hour, 4) % 2]
        live.record(Snapshot(now=at, tasks=(task(status, at),)))
    now = T0 + 13 * H
    restarted = TransitionRecorder(store, window=window)
    restarted.record(Snapshot(now=now, tasks=(task(S.IN_PROGRESS, T0 + 4 * H),)))
    assert live.steps(now) == restarted.steps(now)
    assert [s.at for s in live.steps(now)] == [T0 + 2 * H, T0 + 3 * H, T0 + 4 * H]
    assert bounces(live.steps(now)) == 2
