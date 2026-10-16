"""kelpie_tasks and kelpie_idle_agents: fleet gauges computed from each tick's snapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kelpie.app import ControlLoop
from kelpie.core.engine import Engine
from kelpie.core.model import Owner, OwnerKind, Session, Snapshot, Status, Task
from kelpie.metrics import Metrics

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)


def _snapshot() -> Snapshot:
    return Snapshot(
        now=NOW,
        owners=(
            Owner("a-live", "coder"),  # has a live session
            Owner("a-task", "coder"),  # owns an open task
            Owner("a-idle", "coder"),  # nothing: idle
            Owner("r-idle", "reviewer"),  # nothing: idle
            Owner("r-done", "reviewer"),  # only a finished task: idle
            Owner("a-stale", "tester"),  # only a stale session (hours old): idle
            Owner("h-1", "approver", OwnerKind.HUMAN),  # humans are not agents
        ),
        tasks=(
            Task("T-1", "t", Status.IN_PROGRESS, NOW, owner_id="a-live"),
            Task("T-2", "t", Status.WAITING, NOW, owner_id="a-task"),
            Task("T-3", "t", Status.DONE, NOW, owner_id="r-done"),
            Task("T-4", "t", Status.READY, NOW),
            Task("T-5", "t", Status.IN_PROGRESS, NOW),
        ),
        sessions=(
            Session("S-1", "T-1", "a-live", NOW - timedelta(minutes=5)),
            Session("S-2", "T-5", "a-stale", NOW - timedelta(hours=5)),
        ),
    )


def test_gauges_count_tasks_and_idle_agents() -> None:
    metrics = Metrics()
    assert "kelpie_tasks" not in metrics.render()  # nothing until a snapshot was seen
    loop = ControlLoop(Engine([]), _snapshot, metrics)
    loop.tick_once()
    text = metrics.render()
    assert 'kelpie_tasks{status="in_progress"} 2' in text
    assert 'kelpie_tasks{status="waiting"} 1' in text
    assert 'kelpie_tasks{status="done"} 1' in text
    assert 'kelpie_tasks{status="failed"} 0' in text
    assert 'kelpie_idle_agents{role="coder"} 1' in text
    assert 'kelpie_idle_agents{role="reviewer"} 2' in text
    assert 'kelpie_idle_agents{role="tester"} 1' in text
    assert "approver" not in text


def test_role_with_no_idle_agent_exposes_zero() -> None:
    metrics = Metrics()
    snapshot = Snapshot(
        now=NOW,
        owners=(Owner("a-1", "coder"),),
        tasks=(Task("T-1", "t", Status.IN_PROGRESS, NOW, owner_id="a-1"),),
    )
    ControlLoop(Engine([]), lambda: snapshot, metrics).tick_once()
    assert 'kelpie_idle_agents{role="coder"} 0' in metrics.render()


def test_failing_snapshot_source_keeps_the_gauges_and_counts_a_failure() -> None:
    metrics = Metrics()
    calls: list[int] = []

    def source() -> Snapshot:
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("adapter down")
        return _snapshot()

    loop = ControlLoop(Engine([]), source, metrics)
    loop.tick_once()
    before = [line for line in metrics.render().splitlines() if line.startswith("kelpie_tasks")]
    loop.tick_once()
    text = metrics.render()
    assert [line for line in text.splitlines() if line.startswith("kelpie_tasks")] == before
    assert 'kelpie_idle_agents{role="coder"} 1' in text
    assert "kelpie_engine_tick_failures_total 1" in text
    assert "kelpie_engine_ticks_total 1" in text


def test_reopened_after_done_rate_per_role() -> None:
    from kelpie.core.model import Return

    tasks = (
        Task("A", "A", Status.DONE, NOW - timedelta(hours=2), "coder"),
        Task("B", "B", Status.DONE, NOW - timedelta(hours=3), "coder"),
        Task(
            "C",
            "C",
            Status.READY,
            NOW - timedelta(hours=1),
            "coder",
            reopened_at=(NOW - timedelta(hours=1), NOW - timedelta(days=3)),  # one too old
            returns=(Return(NOW - timedelta(hours=1), "needs tests", "review"),),
        ),
        Task("D", "D", Status.READY, NOW, "tester"),
    )
    metrics = Metrics()
    loop = ControlLoop(Engine([]), lambda: Snapshot(now=NOW, tasks=tasks), metrics)
    loop.tick_once()
    text = metrics.render()
    assert 'kelpie_role_reopened_after_done_24h{role="coder"} 1' in text
    # 2 tasks still done + 1 reopened = 3 completions in the day
    assert 'kelpie_role_reopened_after_done_ratio{role="coder"} 0.3333' in text
    assert 'kelpie_role_returns_24h{role="coder"} 1' in text  # reopens are not returns
    assert 'kelpie_role_reopened_after_done_ratio{role="tester"}' not in text  # none done
