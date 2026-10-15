from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.conditions import check_task_status
from kelpie.core.model import Condition, ConditionKind, Snapshot, Status, Task

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
H = timedelta(hours=1)


def board(*conditions: Condition, status: Status = Status.IN_PROGRESS) -> Snapshot:
    return Snapshot(NOW, tasks=(Task("T-1", "t", status, NOW - H),), conditions=conditions)


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
    ],
)
def test_unknown_when_the_task_cannot_be_judged(params: dict[str, object], error: str) -> None:
    stale = cond(**params, observed_status="done", terminal=True)
    [c] = check_task_status(board(stale)).conditions
    assert c.satisfied is None and c.checked_at == NOW and c.params["error"] == error
    assert "observed_status" not in c.params and "terminal" not in c.params
