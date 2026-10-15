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
    checked: datetime | None = NOW - M,
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
    snap = Snapshot(now=NOW, tasks=(task,), conditions=conditions)
    return list((rule or StaleBlocks()).evaluate(snap))


def test_registered() -> None:
    assert isinstance(REGISTRY["stale_blocks"]({}), StaleBlocks)


def test_unblocks_with_how_long_the_block_outlived_its_cause() -> None:
    [action] = run(blocked(), cond())
    assert action.kind is ActionKind.UNBLOCK and action.task_id == "T-1"
    assert "outlived its cause by 1h." in action.reason
    assert action.params == {"conditions": ["C-1"], "outlived_minutes": 60}


@pytest.mark.parametrize("satisfied", [False, None])
def test_unsatisfied_or_unchecked_condition_keeps_the_block(satisfied: bool | None) -> None:
    assert run(blocked(), cond(satisfied=satisfied, at=None)) == []


def test_missing_or_absent_condition_keeps_the_block() -> None:
    assert run(blocked("C-404")) == []
    assert run(blocked(None)) == []
    assert run(blocked("C-404"), cond("C-2", task_id="T-1")) == []  # dangling id wins


def test_task_linked_conditions_count_and_all_must_hold() -> None:
    assert len(run(blocked(None), cond("C-2", task_id="T-1"))) == 1
    both = run(
        blocked(None), cond("C-2", task_id="T-1"), cond("C-3", satisfied=False, task_id="T-1")
    )
    assert both == []


def unblocks(actions: list) -> list:
    return [a for a in actions if a.kind is ActionKind.UNBLOCK]


def test_evidence_must_be_fresh_and_newer_than_the_block() -> None:
    assert unblocks(run(blocked(), cond(checked=None))) == []
    assert run(blocked(since=NOW - 10 * M), cond(at=NOW - 120 * M, checked=NOW - 30 * M)) == []
    rule = StaleBlocks(max_condition_age=15 * M)
    assert unblocks(run(blocked(), cond(checked=NOW - 16 * M), rule=rule)) == []
    assert len(unblocks(run(blocked(), cond(checked=NOW - 15 * M), rule=rule))) == 1


def test_grace_counts_from_the_last_met_but_not_before_the_block() -> None:
    rule = StaleBlocks(unblock_grace=5 * M)
    assert run(blocked(), cond(at=NOW - 4 * M), rule=rule) == []
    assert len(run(blocked(), cond(at=NOW - 5 * M), rule=rule)) == 1
    later = cond("C-2", at=NOW - 2 * M, task_id="T-1")
    assert run(blocked(None), cond("C-1", task_id="T-1"), later, rule=rule) == []
    # Met long ago, re-checked after a fresh block: grace runs from the block start.
    fresh = blocked(since=NOW - 3 * M)
    assert run(fresh, cond(at=NOW - 120 * M, checked=NOW - M), rule=rule) == []


def test_unknown_met_time_is_reported_as_a_lower_bound() -> None:
    # A connector that polls every minute but never sets satisfied_at still unblocks.
    [action] = run(blocked(), cond(at=None, checked=NOW - M))
    assert "by at least 3h" in action.reason
    assert action.params["outlived_minutes"] == 180


@pytest.mark.parametrize("status", [Status.READY, Status.IN_PROGRESS, Status.WAITING])
def test_only_blocked_tasks(status: Status) -> None:
    assert run(blocked(status=status), cond()) == []


def test_key_is_per_block() -> None:
    [a] = run(blocked(since=NOW - 100 * M), cond())
    [b] = run(blocked(since=NOW - 90 * M), cond())
    assert a.key != b.key


def alerts(actions: list) -> list[str]:
    return [a.key.split(":")[0] for a in actions if a.kind is ActionKind.ALERT]


def test_unlinked_and_missing_condition_blocks_alert_after_threshold() -> None:
    rule = StaleBlocks(unlinked_alert_after=120 * M)
    assert alerts(run(blocked(None), rule=rule)) == ["unlinked"]
    assert run(blocked(None, since=NOW - 119 * M), rule=rule) == []
    [alert] = run(blocked("C-404"), rule=rule)
    assert "C-404" in alert.reason and "cannot see" in alert.reason
    assert run(blocked(None), rule=StaleBlocks(unlinked_alert_after=None)) == []


def test_unmet_condition_past_max_block_alerts() -> None:
    rule = StaleBlocks(max_block=180 * M)
    assert alerts(run(blocked(), cond(satisfied=False, at=None), rule=rule)) == ["overdue"]
    assert run(blocked(since=NOW - 179 * M), cond(satisfied=False, at=None), rule=rule) == []


def test_connector_that_stopped_checking_alerts_instead_of_unblocking() -> None:
    rule = StaleBlocks(stale_evidence_after=60 * M)
    assert alerts(run(blocked(), cond(checked=NOW - 61 * M), rule=rule)) == ["stale-evidence"]
    assert alerts(run(blocked(), cond(satisfied=False, checked=None), rule=rule)) == [
        "stale-evidence"
    ]
    # Checked 30 minutes ago: too old to unblock (15m), too recent to alert (1h).
    assert run(blocked(), cond(checked=NOW - 30 * M), rule=rule) == []
    young = blocked(since=NOW - 30 * M)
    assert run(young, cond(checked=None), rule=rule) == []  # block younger than the threshold


@pytest.mark.parametrize(
    ("minutes", "text"), [(0, "0m"), (45, "45m"), (180, "3h"), (185, "3h 05m"), (-5, "0m")]
)
def test_human(minutes: int, text: str) -> None:
    assert _human(minutes * M) == text


def test_build() -> None:
    rule = build(
        {
            "unblock_grace": "0s",
            "max_condition_age": "30m",
            "unlinked_alert_after": "4h",
            "max_block": False,
            "stale_evidence_after": "2h",
        }
    )
    assert rule.unblock_grace == timedelta(0) and rule.max_condition_age == 30 * M
    assert rule.unlinked_alert_after == 240 * M and rule.stale_evidence_after == 120 * M
    assert rule.max_block is None
    for bad in (
        {"unblock_grace": [1]},
        {"unblock_grace": -60},
        {"max_condition_age": "0s"},
        {"max_block": True},
        {"max_condition_age": False},
        {"grace": "1m"},
    ):
        with pytest.raises(ValueError):
            build(bad)
