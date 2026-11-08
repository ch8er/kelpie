from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.conditions import check_task_status, linked_conditions
from kelpie.core.model import (
    Condition,
    ConditionKind,
    ConditionTarget,
    Snapshot,
    Status,
    TargetKind,
    Task,
)

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
H = timedelta(hours=1)


def board(*conditions: Condition, status: Status = Status.IN_PROGRESS) -> Snapshot:
    tasks = (Task("T-1", "t", status, NOW - H), Task("T-2", "t", Status.WAITING, NOW - H))
    return Snapshot(NOW, tasks=tasks, conditions=conditions)


def cond(cid: str = "C", **params: object) -> Condition:
    return Condition(cid, ConditionKind.TASK_STATUS, "T-2", params=dict(params))


def test_snapshot_without_task_status_conditions_is_returned_as_is() -> None:
    snapshot = board(Condition("X", ConditionKind.PR_MERGED, "T-2"))
    assert check_task_status(snapshot) is snapshot


def test_condition_holds_once_the_task_has_the_status() -> None:
    [c] = check_task_status(board(cond(task="T-1"), status=Status.DONE)).conditions
    assert c.satisfied is True and c.satisfied_at == NOW - H and c.checked_at == NOW
    assert c.params == {"task": "T-1", "observed_status": "done"}
    [kept] = check_task_status(board(c, status=Status.DONE)).conditions
    assert kept.satisfied_at == NOW - H  # stays the time it first held


def test_other_status_is_unmet_and_a_different_end_is_terminal() -> None:
    [open_] = check_task_status(board(cond(task="T-1"))).conditions
    assert open_.satisfied is False and "terminal" not in open_.params
    [ended] = check_task_status(board(cond(task="T-1"), status=Status.CANCELLED)).conditions
    assert ended.satisfied is False and ended.params["terminal"] is True
    [review] = check_task_status(
        board(cond(task="T-1", status="review"), status=Status.REVIEW)
    ).conditions
    assert review.satisfied is True


@pytest.mark.parametrize(
    ("params", "error"),
    [
        ({"task": "T-9"}, "not_found"),
        ({}, "invalid_params"),
        ({"task": ""}, "invalid_params"),
        ({"task": 3}, "invalid_params"),
        ({"task": "T-1", "status": "merged"}, "invalid_params"),
        ({"task": "T-2"}, "invalid_params"),  # the condition's own task
    ],
)
def test_unknown_when_the_task_cannot_be_judged(params: dict[str, object], error: str) -> None:
    stale = cond(**params, observed_status="done", terminal=True)
    [c] = check_task_status(board(stale)).conditions
    assert c.satisfied is None and c.checked_at == NOW and c.params["error"] == error
    assert "observed_status" not in c.params and "terminal" not in c.params


# --- shared conditions and their targets ---------------------------------------------------


def test_a_shared_condition_lists_every_waiter_once() -> None:
    c = Condition("C", ConditionKind.PR_MERGED, "T-1", task_ids=("T-2", "T-1", "", "T-3"))
    assert c.waiters == ("T-1", "T-2", "T-3")
    assert c.waits("T-3") and c.waits("T-1") and not c.waits("T-9")
    assert Condition("C", ConditionKind.TIME).waiters == ()
    assert Condition("C", ConditionKind.TIME, task_ids=("T-4",)).waiters == ("T-4",)


@pytest.mark.parametrize(
    ("kind", "params", "target"),
    [
        (
            ConditionKind.PR_MERGED,
            {"repo": "example/repo", "pr": 7},
            (TargetKind.PR, "example/repo#7"),
        ),
        (
            ConditionKind.CI_STATUS,
            {"repo": "example/repo", "pr": "#8"},
            (TargetKind.PR, "example/repo#8"),
        ),
        (ConditionKind.HUMAN_TASK, {"task": "H-1"}, (TargetKind.TASK, "H-1")),
        (ConditionKind.TASK_STATUS, {"task": "P-1", "status": "done"}, (TargetKind.TASK, "P-1")),
        (ConditionKind.CUSTOM, {"person": " alice "}, (TargetKind.PERSON, "alice")),
        # The params do not say: no target.
        (ConditionKind.PR_MERGED, {"repo": "example/repo"}, None),
        (ConditionKind.PR_MERGED, {"repo": "example/repo", "pr": True}, None),
        (ConditionKind.PR_MERGED, {"repo": "example/repo", "pr": "abc"}, None),
        (ConditionKind.HUMAN_TASK, {}, None),
        (ConditionKind.QUOTA_RESET, {"provider": "example-llm"}, None),
        (ConditionKind.TIME, {"task": "T-1"}, None),
    ],
)
def test_the_target_is_read_from_params(
    kind: ConditionKind, params: dict[str, object], target: tuple[TargetKind, str] | None
) -> None:
    got = Condition("C", kind, "T-1", params=params).target
    assert got == (None if target is None else ConditionTarget(*target))


def test_linked_conditions_include_shared_ones() -> None:
    task = Task("T-2", "t", Status.WAITING, NOW - H)
    shared = Condition("S", ConditionKind.PR_MERGED, "T-1", task_ids=("T-2",), registered_at=NOW)
    old = Condition(
        "O", ConditionKind.PR_MERGED, "T-1", task_ids=("T-2",), registered_at=NOW - 2 * H
    )
    other = Condition("X", ConditionKind.PR_MERGED, "T-1")
    snap = Snapshot(NOW, tasks=(task,), conditions=(shared, old, other))
    assert [c.id for c in linked_conditions(task, snap)[0]] == ["S"]


def test_a_task_status_condition_cannot_wait_for_any_of_its_waiters() -> None:
    c = Condition("C", ConditionKind.TASK_STATUS, "T-2", {"task": "T-1"}, task_ids=("T-1",))
    (checked,) = check_task_status(board(c)).conditions
    assert checked.params["error"] == "invalid_params" and checked.satisfied is None


def test_a_waiter_left_listed_reads_a_later_join_as_its_own_wait() -> None:
    """The producer contract: a waiter leaves ``task_ids`` when it stops waiting. One left
    listed would link the condition again once another task joins (a later
    ``registered_at``); this documents why the contract matters."""
    again = Task("T-1", "t", Status.WAITING, NOW - H)  # waits again, on something else
    stale = Condition("S", ConditionKind.PR_MERGED, "T-5", task_ids=("T-1",), registered_at=NOW)
    snap = Snapshot(NOW, tasks=(again,), conditions=(stale,))
    assert [c.id for c in linked_conditions(again, snap)[0]] == ["S"]
    assert not Condition("S", ConditionKind.PR_MERGED, "T-5").waits("")
