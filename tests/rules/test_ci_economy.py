import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind, ActionResult, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import (
    Block,
    Build,
    BuildStatus,
    Condition,
    ConditionKind,
    Resource,
    ResourceKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules.ci_economy import CiEconomy, build
from tests.fakes import FakeAlerter, FakeCI

NOW = datetime(2026, 3, 3, 12, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def b(bid: str, **fields: Any) -> Build:
    base: dict[str, Any] = {
        "source": "ci",
        "repo": "example/repo",
        "workflow": "ci",
        "branch": f"task/{bid}",
        "sha": f"{bid}-sha",
        "status": BuildStatus.SUCCESS,
        "started_at": NOW - 2 * H,
        "finished_at": NOW - 2 * H + 10 * M,
        "minutes": 10.0,
        "event": "pull_request",
    }
    base.update(fields)
    return Build(bid, **base)


def quota(**fields: Any) -> Resource:
    base: dict[str, Any] = {"limit": 3000.0, "unit": "minutes"}
    base.update(fields)
    return Resource("ci:x", ResourceKind.CI_MINUTES, **base)


def alerts(actions: list[Action] | Any, severity: Severity | None = None) -> list[Action]:
    return [
        a
        for a in actions
        if a.kind is ActionKind.ALERT and (severity is None or a.severity is severity)
    ]


def test_registered_and_defaults() -> None:
    assert isinstance(REGISTRY["ci_economy"]({}), CiEconomy)
    assert CiEconomy().evaluate(Snapshot(now=NOW)) == []


def test_waste_below_threshold_is_quiet() -> None:
    snap = Snapshot(now=NOW, builds=(b("B-1", draft=True, minutes=5),))
    assert CiEconomy(waste_min_minutes=30).evaluate(snap) == []


def test_waste_summary_is_a_state_that_escalates_when_it_doubles() -> None:
    rule = CiEconomy(waste_min_minutes=10)
    one = Snapshot(now=NOW, builds=(b("B-1", draft=True, minutes=12),))
    two = Snapshot(now=NOW, builds=(b("B-1", draft=True, minutes=25),))
    (a1,), (a2,) = rule.evaluate(one), rule.evaluate(two)
    assert a1.state == a2.state == "ci_economy:waste"
    assert a1.key != a2.key
    assert a1.since == NOW - 2 * H
    assert a1.params["categories"] == {"draft": {"builds": 1, "minutes": 12.0}}


def test_skipped_builds_and_real_work_are_not_waste() -> None:
    rule = CiEconomy(waste_min_minutes=0)
    snap = Snapshot(
        now=NOW,
        builds=(
            b("B-1", status=BuildStatus.SKIPPED, draft=True, minutes=0),
            b("B-2", changed_paths=("src/a.py",)),
            b("B-3", changed_paths=()),
        ),
    )
    assert rule.evaluate(snap) == []
    assert rule.classify(snap.builds[0], snap) is None


def test_superseded_finished_build_without_known_successor_is_not_waste() -> None:
    rule = CiEconomy()
    build_ = b("B-1", superseded_by="B-9")
    assert rule.classify(build_, Snapshot(now=NOW, builds=(build_,))) is None


def test_already_verified_needs_an_earlier_success_of_the_same_pipeline() -> None:
    rule = CiEconomy()
    first = b("B-1", sha="s", finished_at=NOW - 110 * M)
    again = b("B-2", sha="s", started_at=NOW - H, finished_at=NOW - 50 * M)
    other = b("B-3", sha="s", workflow="lint", started_at=NOW - H)
    pushed = b("B-4", sha="s", event="push", started_at=NOW - H)
    snap = Snapshot(now=NOW, builds=(first, again, other, pushed))
    assert rule.classify(again, snap) == "already_verified"
    assert rule.classify(first, snap) is None
    assert rule.classify(other, snap) is None
    assert rule.classify(pushed, snap) is None  # another trigger is not a repeat
    assert rule.classify(replace(again, sha=None), snap) is None


def test_wip_patterns() -> None:
    rule = build({"wip_patterns": [r"\bdo not merge\b"]})
    snap = Snapshot(now=NOW)
    assert rule.classify(b("B-1", commit_message="Do not merge: spike"), snap) == "wip"
    assert rule.classify(b("B-2", commit_message="WIP"), snap) is None
    default = CiEconomy()
    assert default.classify(b("B-3", commit_message="fixup! parser"), snap) == "wip"
    assert default.classify(b("B-4", commit_message="Wipe caches"), snap) is None


def test_cancel_only_active_superseded_builds_with_a_source() -> None:
    rule = CiEconomy(waste_min_minutes=1000)
    snap = Snapshot(
        now=NOW,
        builds=(
            b(
                "B-1",
                status=BuildStatus.QUEUED,
                superseded_by="B-2",
                finished_at=None,
                cancellable=True,
            ),
            b("B-2", status=BuildStatus.RUNNING, finished_at=None),
            b(
                "B-3",
                status=BuildStatus.RUNNING,
                superseded_by="B-2",
                source=None,
                cancellable=True,
            ),
            b("B-4", status=BuildStatus.RUNNING, superseded_by="B-2", finished_at=None),
            b("B-5", status=BuildStatus.UNKNOWN, superseded_by="B-2", cancellable=True),
        ),
    )
    (cancel,) = rule.evaluate(snap)
    assert cancel.kind is ActionKind.CANCEL_BUILD
    assert cancel.executor == "ci" and cancel.once
    assert cancel.params["build_id"] == "B-1"
    assert CiEconomy(cancel_superseded=False, waste_min_minutes=1000).evaluate(snap) == []


def test_exhausted_quota_lists_tasks_waiting_on_ci() -> None:
    tasks = (
        Task("T-1", "t", Status.BLOCKED, NOW - H, block=Block("ci", NOW - H, "C-1")),
        Task("T-2", "t", Status.WAITING, NOW - H),
        Task("T-3", "t", Status.BLOCKED, NOW - H, block=Block("ci", NOW - H, "C-3")),
        Task("T-4", "t", Status.IN_PROGRESS, NOW - H),
    )
    conditions = (
        Condition("C-1", ConditionKind.CI_STATUS, task_id="T-1", satisfied=False),
        Condition("C-2", ConditionKind.CI_STATUS, task_id="T-2"),
        Condition("C-3", ConditionKind.PR_MERGED, task_id="T-3", satisfied=False),
    )
    snap = Snapshot(
        now=NOW,
        tasks=tasks,
        conditions=conditions,
        resources=(quota(used=3000.0, resets_at=NOW + 24 * H),),
    )
    (alert,) = CiEconomy().evaluate(snap)
    assert alert.severity is Severity.CRITICAL
    assert alert.params["blocked_tasks"] == ["T-1", "T-2"]
    assert "billing" in alert.reason


def test_exhausted_without_waiting_tasks() -> None:
    snap = Snapshot(now=NOW, resources=(quota(used=2900.0),))
    (alert,) = CiEconomy(quota_critical=0.95).evaluate(snap)
    assert alert.severity is Severity.CRITICAL and alert.params["blocked_tasks"] == []


def test_forecast_warns_before_the_quota_runs_out() -> None:
    builds = (b("B-1", minutes=240.0),)  # 10 minutes an hour over 24h
    rule = CiEconomy(waste_min_minutes=1000)
    soon = Snapshot(now=NOW, builds=builds, resources=(quota(used=2900.0),))
    (alert,) = rule.evaluate(soon)
    assert alert.severity is Severity.WARNING
    assert alert.params["runs_out_at"] == (NOW + 10 * H).isoformat()
    assert alert.params["burn_per_hour"] == 10.0
    # Resets before it would run out, or runs out beyond the horizon: nothing.
    resets = Snapshot(
        now=NOW, builds=builds, resources=(quota(used=2900.0, resets_at=NOW + 5 * H),)
    )
    assert rule.evaluate(resets) == []
    far = Snapshot(now=NOW, builds=builds, resources=(quota(used=0.0),))
    assert CiEconomy(forecast_horizon=H).evaluate(far) == []
    idle = Snapshot(now=NOW, resources=(quota(used=2900.0),))
    assert rule.evaluate(idle) == []


def test_projection_without_usage() -> None:
    rule = CiEconomy(waste_min_minutes=1000)
    heavy = Snapshot(now=NOW, builds=(b("B-1", minutes=240.0),), resources=(quota(),))
    (alert,) = rule.evaluate(heavy)
    assert alert.severity is Severity.WARNING and alert.params["projected"] == 7200
    light = Snapshot(now=NOW, builds=(b("B-1", minutes=24.0),), resources=(quota(),))
    assert rule.evaluate(light) == []


def test_stale_and_limitless_readings_are_ignored() -> None:
    snap = Snapshot(
        now=NOW,
        resources=(quota(used=3000.0, at=NOW - 2 * H), quota(used=5.0, limit=None)),
    )
    assert CiEconomy().evaluate(snap) == []


def test_old_builds_fall_out_of_the_window() -> None:
    old = b("B-1", draft=True, started_at=NOW - 30 * H, finished_at=NOW - 29 * H)
    assert CiEconomy(waste_min_minutes=1).evaluate(Snapshot(now=NOW, builds=(old,))) == []


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"window": "soon"}, "window"),
        ({"window": "0s"}, "positive"),
        ({"docs_globs": "docs/*"}, "docs_globs"),
        ({"wip_patterns": ["("]}, "wip_patterns"),
        ({"waste_min_minutes": -1}, "out of range"),
        ({"waste_min_minutes": True}, "number"),
        ({"quota_critical": 1.5}, "out of range"),
        ({"cancel_superseded": "yes"}, "true or false"),
        ({"nope": 1}, "unknown"),
    ],
)
def test_config_errors(config: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build(config)


def test_config_values() -> None:
    rule = build(
        {
            "window": "12h",
            "forecast_horizon": "2d",
            "quota_period": "31d",
            "max_reading_age": "30m",
            "docs_globs": ["*.txt"],
            "waste_min_minutes": 0,
            "quota_critical": 0.9,
            "cancel_superseded": False,
        }
    )
    assert rule.window == 12 * H and rule.docs_globs == ("*.txt",)
    assert rule.quota_critical == 0.9 and not rule.cancel_superseded


def test_engine_routes_cancel_to_the_ci_connector_in_act_mode_only() -> None:
    snap = Snapshot(
        now=NOW,
        builds=(
            b(
                "B-1",
                status=BuildStatus.RUNNING,
                superseded_by="B-2",
                finished_at=None,
                cancellable=True,
            ),
            b("B-2", status=BuildStatus.RUNNING, finished_at=None),
        ),
    )
    rule = CiEconomy(waste_min_minutes=1000)
    for mode, dry_run, expected in (
        (Mode.ACT, False, ["B-1"]),
        (Mode.ACT, True, []),
        (Mode.OBSERVE, False, []),
        (Mode.SUGGEST, False, []),
    ):
        ci, alerter = FakeCI(), FakeAlerter()
        config = EngineConfig(dry_run=dry_run, modes={"ci_economy": mode})
        engine = Engine([rule], connectors=[ci], alerter=alerter, config=config)
        results = engine.tick(lambda: snap)
        assert ci.cancelled == expected
        if expected:
            assert results[0].outcome is Outcome.EXECUTED


def test_nothing_to_do_is_its_own_outcome() -> None:
    class Done(FakeCI):
        def execute(self, action: Action) -> ActionResult:  # type: ignore[override]
            return ActionResult.NOTHING_TO_DO

    snap = Snapshot(
        now=NOW,
        builds=(
            b("B-1", status=BuildStatus.RUNNING, superseded_by="B-2", cancellable=True),
            b("B-2", status=BuildStatus.RUNNING),
        ),
    )
    config = EngineConfig(dry_run=False, modes={"ci_economy": Mode.ACT})
    alerter = FakeAlerter()
    engine = Engine(
        [CiEconomy(waste_min_minutes=1000)], connectors=[Done()], alerter=alerter, config=config
    )
    (result,) = engine.tick(lambda: snap)
    assert result.outcome is Outcome.NOTHING_TO_DO and alerter.sent == []
    assert engine.tick(lambda: snap)[0].outcome is Outcome.SUPPRESSED  # once: done


def test_classifying_many_builds_is_linear() -> None:
    builds = tuple(
        b(
            f"B-{i}",
            sha=f"s{i % 3000}",
            started_at=NOW - H + i * timedelta(milliseconds=100),
            finished_at=NOW - H + i * timedelta(milliseconds=100) + M,
            superseded_by=f"B-{i + 1}" if i % 2 else None,
        )
        for i in range(6000)
    )
    snap = Snapshot(now=NOW, builds=builds)
    started = time.perf_counter()
    actions = CiEconomy().evaluate(snap)
    assert time.perf_counter() - started < 0.5
    assert alerts(actions, Severity.INFO)


def test_engine_takes_only_nothing_to_do_from_an_executor() -> None:
    class Odd(FakeCI):
        def execute(self, action: Action) -> Any:  # type: ignore[override]
            return ActionResult.DONE

    class Bad:
        name = "ci"

        def execute(self, action: Action) -> Outcome:
            return Outcome.SUGGESTED

    snap = Snapshot(
        now=NOW,
        builds=(
            b("B-1", status=BuildStatus.RUNNING, superseded_by="B-2", cancellable=True),
            b("B-2", status=BuildStatus.RUNNING),
        ),
    )
    config = EngineConfig(dry_run=False, modes={"ci_economy": Mode.ACT})
    rule = CiEconomy(waste_min_minutes=1000)
    engine = Engine([rule], connectors=[Odd()], alerter=FakeAlerter(), config=config)
    assert engine.tick(lambda: snap)[0].outcome is Outcome.EXECUTED
    engine = Engine([rule], alerter=FakeAlerter(), config=config)
    engine.executors["ci"] = Bad()  # type: ignore[assignment]
    assert engine.tick(lambda: snap)[0].outcome is Outcome.NOT_EXECUTED
