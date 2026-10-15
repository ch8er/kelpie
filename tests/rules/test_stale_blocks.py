from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import Block, Condition, ConditionKind, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.stale_blocks import StaleBlocks, _human, build

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
M = timedelta(minutes=1)


def blocked(
    cid: str | None = "C-1", since: datetime = NOW - 180 * M, status: Status = Status.BLOCKED
) -> Task:
    return Task("T-1", "t", status, since, block=Block("waiting_pr_merge: #1", since, cid))


def cond(
    cid: str = "C-1",
    satisfied: bool | None = True,
    at: datetime | None = NOW - 60 * M,
    task_id: str | None = None,
    checked: datetime | None = None,
) -> Condition:
    return Condition(
        cid,
        ConditionKind.PR_MERGED,
        task_id,
        satisfied=satisfied,
        satisfied_at=at,
        checked_at=checked,
    )


def run(task: Task, *conditions: Condition, rule: StaleBlocks | None = None) -> list:
    return list(
        (rule or StaleBlocks()).evaluate(Snapshot(now=NOW, tasks=(task,), conditions=conditions))
    )


def test_registered() -> None:
    assert isinstance(REGISTRY["stale_blocks"]({}), StaleBlocks)


def test_unblocks_with_how_long_the_block_outlived_its_cause() -> None:
    [action] = run(blocked(), cond())
    assert action.kind is ActionKind.UNBLOCK and action.task_id == "T-1"
    assert "outlived its cause by 1h" in action.reason
    assert action.params == {"conditions": ["C-1"], "outlived_minutes": 60}


@pytest.mark.parametrize("satisfied", [False, None])
def test_unsatisfied_or_unchecked_condition_keeps_the_block(satisfied: bool | None) -> None:
    assert run(blocked(), cond(satisfied=satisfied, at=None)) == []


def test_missing_or_absent_condition_keeps_the_block() -> None:
    assert run(blocked("C-404")) == []  # points at a condition Kelpie cannot see
    assert run(blocked(None)) == []  # nothing linked at all
    assert run(blocked("C-404"), cond("C-2", task_id="T-1")) == []


def test_task_linked_conditions_count_and_all_must_hold() -> None:
    assert len(run(blocked(None), cond("C-2", task_id="T-1"))) == 1
    assert run(blocked(), cond(), cond("C-2", satisfied=False, task_id="T-1")) == []
    assert run(blocked(), cond(), cond("C-3", task_id="T-other")) != []  # other task's


def test_grace_counts_from_the_last_condition_met() -> None:
    rule = StaleBlocks(unblock_grace=5 * M)
    assert run(blocked(), cond(at=NOW - 4 * M), rule=rule) == []
    assert len(run(blocked(), cond(at=NOW - 5 * M), rule=rule)) == 1
    later = cond("C-2", at=NOW - 2 * M, task_id="T-1")
    assert run(blocked(), cond(), later, rule=rule) == []


def test_met_time_falls_back_to_checked_at_then_block_start() -> None:
    [action] = run(blocked(), cond(at=None, checked=NOW - 30 * M))
    assert action.params["outlived_minutes"] == 30
    [action] = run(blocked(since=NOW - 20 * M), cond(at=None))
    assert action.params["outlived_minutes"] == 20


def test_block_set_after_condition_was_met_counts_from_the_block() -> None:
    [action] = run(blocked(since=NOW - 10 * M), cond(at=NOW - 120 * M))
    assert action.params["outlived_minutes"] == 10


@pytest.mark.parametrize("status", [Status.READY, Status.IN_PROGRESS, Status.REVIEW])
def test_only_blocked_tasks(status: Status) -> None:
    assert run(blocked(status=status), cond()) == []


def test_key_is_per_block() -> None:
    [a] = run(blocked(since=NOW - 100 * M), cond())
    [b] = run(blocked(since=NOW - 90 * M), cond())
    assert a.key != b.key


@pytest.mark.parametrize(
    ("minutes", "text"), [(0, "0m"), (45, "45m"), (180, "3h"), (185, "3h 05m"), (-5, "0m")]
)
def test_human(minutes: int, text: str) -> None:
    assert _human(minutes * M) == text


def test_build() -> None:
    assert build({"unblock_grace": "10m"}).unblock_grace == 10 * M
    for bad in ({"unblock_grace": [1]}, {"grace": "1m"}):
        with pytest.raises(ValueError):
            build(bad)
