"""kelpie_role_idle_seconds_total: idle agent-seconds per role, counted tick to tick."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from kelpie.core.engine import Engine
from kelpie.metrics import MAX_IDLE_STEP, Metrics
from tests.test_metrics_fleet import NOW, _snapshot


def _tick(metrics: Metrics, minutes: float) -> None:
    metrics.observe_tick(
        0.1, [], Engine([]), replace(_snapshot(), now=NOW + timedelta(minutes=minutes))
    )


def _idle(metrics: Metrics, role: str) -> str | None:
    prefix = f'kelpie_role_idle_seconds_total{{role="{role}"}} '
    return next(
        (line[len(prefix) :] for line in metrics.render().splitlines() if line.startswith(prefix)),
        None,
    )


def test_idle_time_adds_up_per_role() -> None:
    metrics = Metrics()
    _tick(metrics, 0)
    assert _idle(metrics, "coder") == "0"  # listed from the first tick, nothing counted yet
    _tick(metrics, 1)
    _tick(metrics, 2)
    # coder: 1 idle agent for 2 minutes; reviewer: 2 for 2 minutes
    assert _idle(metrics, "coder") == "120"
    assert _idle(metrics, "reviewer") == "240"
    assert _idle(metrics, "tester") == "120"
    assert "# TYPE kelpie_role_idle_seconds_total counter" in metrics.render()


def test_a_busy_role_accrues_nothing() -> None:
    metrics = Metrics()
    busy = replace(_snapshot(), owners=_snapshot().owners[:2])  # a-live and a-task only
    for minutes in (0, 1, 2):
        metrics.observe_tick(
            0.1, [], Engine([]), replace(busy, now=NOW + timedelta(minutes=minutes))
        )
    assert _idle(metrics, "coder") == "0"


def test_a_long_gap_or_a_clock_going_back_is_not_counted() -> None:
    metrics = Metrics()
    _tick(metrics, 0)
    gap = MAX_IDLE_STEP.total_seconds() / 60 + 1
    _tick(metrics, gap)  # serve was stopped or the host slept
    assert _idle(metrics, "coder") == "0"
    _tick(metrics, gap - 1)  # the clock went back
    assert _idle(metrics, "coder") == "0"
    _tick(metrics, gap)  # one more minute from there
    assert _idle(metrics, "coder") == "60"


def test_nothing_before_a_snapshot() -> None:
    assert _idle(Metrics(), "coder") is None


def test_a_long_interval_widens_the_step() -> None:
    from kelpie.core.config import EngineConfig

    metrics = Metrics()
    engine = Engine([], config=EngineConfig(interval=timedelta(minutes=15)))
    for minutes in (0, 15.5):
        metrics.observe_tick(
            0.1, [], engine, replace(_snapshot(), now=NOW + timedelta(minutes=minutes))
        )
    assert _idle(metrics, "coder") == "930"
    assert "over 45 minutes is not counted" in metrics.render()


def test_a_failed_tick_counts_the_last_reading() -> None:
    metrics = Metrics()
    _tick(metrics, 0)
    metrics.observe_tick(0.1, [], Engine([]), None)  # no snapshot: nothing read
    _tick(metrics, 2)
    assert _idle(metrics, "coder") == "120"


def test_roles_past_the_cap_are_summed_under_other() -> None:
    from kelpie.core.model import Owner
    from kelpie.metrics import MAX_ROLES

    owners = tuple(Owner(f"a-{i}", f"role-{i:02d}") for i in range(MAX_ROLES + 3))
    metrics = Metrics()
    for minutes in (0, 1):
        snap = replace(
            _snapshot(), owners=owners, tasks=(), sessions=(), now=NOW + timedelta(minutes=minutes)
        )
        metrics.observe_tick(0.1, [], Engine([]), snap)
    lines = [
        x for x in metrics.render().splitlines() if x.startswith("kelpie_role_idle_seconds_total{")
    ]
    assert len(lines) == MAX_ROLES + 1
    assert _idle(metrics, "other") == "180"
