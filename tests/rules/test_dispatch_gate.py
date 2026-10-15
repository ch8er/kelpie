from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import (
    Condition,
    ConditionKind,
    Dependency,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules.dispatch_gate import DispatchGate, build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)


def task(tid: str, status: Status = Status.READY, since: timedelta = 60 * M) -> Task:
    return Task(tid, f"title {tid}", status, NOW - since, "developer")


def cond(
    cid: str,
    kind: ConditionKind = ConditionKind.PR_MERGED,
    task_id: str = "T",
    satisfied: bool | None = False,
    checked: timedelta | None = M,
    registered: timedelta = 60 * M,
    **params: Any,
) -> Condition:
    return Condition(
        cid,
        kind,
        task_id,
        {"repo": "example/repo", "pr": 7, **params},
        satisfied=satisfied,
        checked_at=None if checked is None else NOW - checked,
        registered_at=NOW - registered,
    )


def run(
    *tasks: Task,
    conditions: tuple[Condition, ...] = (),
    deps: tuple[Dependency, ...] = (),
    sessions: tuple[Session, ...] = (),
    rule: DispatchGate | None = None,
) -> list[Action]:
    snapshot = Snapshot(
        NOW, tasks=tasks, conditions=conditions, dependencies=deps, sessions=sessions
    )
    return list((rule or DispatchGate()).evaluate(snapshot))


def test_registered() -> None:
    assert isinstance(REGISTRY["dispatch_gate"]({}), DispatchGate)


def test_unmet_pr_condition_parks_the_ready_task() -> None:
    c = cond("C", pr_state="open", error="x", sha="abc")
    [action] = run(task("T"), conditions=(c,))
    assert action.kind is ActionKind.WAIT and action.task_id == "T" and action.once
    assert action.key == f"dispatch_gate:T:{(NOW - 60 * M).isoformat()}"
    assert action.params == {
        "conditions": [
            {
                "id": "C",
                "kind": "pr_merged",
                "params": {"repo": "example/repo", "pr": 7, "sha": "abc"},
            }
        ],
        "open_parents": [],
        "unmet_conditions": ["C"],
    }
    assert "PR example/repo#7 is not merged" in action.reason


@pytest.mark.parametrize(
    ("kind", "params", "text"),
    [
        (ConditionKind.CI_STATUS, {}, "CI on PR example/repo#7 is not green"),
        (ConditionKind.HUMAN_TASK, {"task": "H", "status": "ready", "owner": "x"}, "human task H"),
        (ConditionKind.TASK_STATUS, {"task": "P", "status": "review"}, "task P is not review"),
        (ConditionKind.CUSTOM, {}, "C (custom) is not met"),
    ],
)
def test_every_condition_kind_is_described(
    kind: ConditionKind, params: dict[str, Any], text: str
) -> None:
    [action] = run(task("T"), conditions=(cond("C", kind, **params),))
    assert text in action.reason
    kept = action.params["conditions"][0]["params"]
    if kind is ConditionKind.HUMAN_TASK:
        assert "status" not in kept and "owner" not in kept  # results, not inputs
    if kind is ConditionKind.TASK_STATUS:
        assert kept["status"] == "review"  # an input for task_status


def test_open_parent_gets_a_task_status_condition() -> None:
    deps = (
        Dependency("T", "P"),
        Dependency("T", "D"),
        Dependency("T", "GONE"),
        Dependency("T", "T"),
    )
    tasks = (task("T"), task("P", Status.IN_PROGRESS), task("D", Status.DONE))
    [action] = run(*tasks, deps=deps)
    assert action.params["open_parents"] == ["P"]
    assert action.params["conditions"] == [
        {
            "id": "dispatch_gate:T:P",
            "kind": "task_status",
            "params": {"task": "P", "status": "done", "source": "dispatch_gate"},
        }
    ]
    assert "parent P is in_progress" in action.reason


@pytest.mark.parametrize(
    "c",
    [
        cond("C", satisfied=True),
        cond("C", satisfied=None),  # unknown: cannot be resolved
        cond("C", checked=None),
        cond("C", checked=16 * M),  # stale evidence
        cond("C", terminal=True),  # can never be met: rules 2 and 4 alert
        cond("C", task_id="OTHER"),
        cond("C", registered=90 * M),  # left over from before the task became ready
    ],
    ids=["met", "unknown", "never-checked", "stale", "terminal", "other-task", "leftover"],
)
def test_prerequisites_that_hold_or_cannot_be_judged_do_not_gate(c: Condition) -> None:
    assert run(task("T"), conditions=(c,)) == []


@pytest.mark.parametrize(
    "status", [Status.IN_PROGRESS, Status.BACKLOG, Status.WAITING, Status.BLOCKED]
)
def test_only_ready_tasks_are_gated(status: Status) -> None:
    assert run(task("T", status), conditions=(cond("C"),)) == []


def test_ready_task_with_a_live_session_is_already_picked_up() -> None:
    live = (Session("S", "T", "a1", NOW - 5 * M, last_heartbeat=NOW - M),)
    assert run(task("T"), conditions=(cond("C"),), sessions=live) == []
    ended = (Session("S", "T", "a1", NOW - 50 * M, ended_at=NOW - 40 * M),)
    assert run(task("T"), conditions=(cond("C"),), sessions=ended)


def test_long_prerequisite_lists_are_shortened_in_the_message() -> None:
    conditions = tuple(cond(f"C{i}") for i in range(7))
    [action] = run(task("T"), conditions=conditions)
    assert len(action.params["conditions"]) == 7 and "+2 more" in action.reason


def test_max_condition_age_is_configurable() -> None:
    old = (cond("C", checked=30 * M),)
    assert run(task("T"), conditions=old) == []
    assert run(task("T"), conditions=old, rule=build({"max_condition_age": "1h"}))


@pytest.mark.parametrize(
    "config", [{"max_condition_age": "0s"}, {"max_condition_age": "soon"}, {"x": 1}]
)
def test_build_rejects_bad_config(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build(config)
