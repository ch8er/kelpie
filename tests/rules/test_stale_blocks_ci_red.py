"""Rule 4: a block that waits for CI while its PR's head CI finished red (ci_red_after)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules.stale_blocks import StaleBlocks, build

NOW = datetime(2026, 3, 2, 14, tzinfo=UTC)
SINCE = NOW - timedelta(hours=2)
CID = "block:T-1:pr_merged:example/repo#63"
SHA = "a" * 40


def red(
    since: datetime = NOW - timedelta(hours=1),
    kind: ConditionKind = ConditionKind.PR_MERGED,
    **p: Any,
) -> Condition:
    params: dict[str, Any] = {
        "repo": "example/repo",
        "pr": 63,
        "pr_state": "open",
        "head_sha": SHA,
        "head_ci": "failure",
        "head_ci_failed": ["test"],
        "head_ci_red_since": since.isoformat(),
        **p,
    }
    return Condition(
        CID, kind, "T-1", {k: v for k, v in params.items() if v is not None},
        satisfied=False, checked_at=NOW - timedelta(minutes=1), registered_at=SINCE,
    )  # fmt: skip


def snap(c: Condition, *, since: datetime = SINCE, **extra: Any) -> Snapshot:
    task = Task(
        "T-1",
        "Review",
        Status.BLOCKED,
        since,
        owner_id="reviewer-1",
        block=Block("automerge PR #63 — waiting for CI", since, condition_id=CID),
    )
    owners = (Owner("reviewer-1", "reviewer"), Owner("ceo", "ceo", OwnerKind.HUMAN))
    return Snapshot(now=NOW, tasks=(task,), conditions=(c,), owners=owners, **extra)


def alerts(rule: StaleBlocks, s: Snapshot) -> list[Action]:
    return [a for a in rule.evaluate(s) if a.kind is ActionKind.ALERT]


def test_observe_by_default_and_its_own_state() -> None:
    (a,) = alerts(StaleBlocks(), snap(red()))
    assert a.observe_only and a.state == f"stale_blocks:ci-red:T-1:{SINCE.isoformat()}"
    assert (
        a.params["head_sha"] == SHA
        and a.params["red_since"] == (NOW - timedelta(hours=1)).isoformat()
    )
    assert a.since == NOW - timedelta(hours=1)
    (b,) = alerts(StaleBlocks(ci_red_mode="rule"), snap(red()))
    assert not b.observe_only


def test_off_and_negative_cases() -> None:
    assert alerts(StaleBlocks(ci_red_after=None), snap(red())) == []
    # Still running (no red_since), unknown, expecting failure, checked before the block.
    assert alerts(StaleBlocks(), snap(red(head_ci_red_since=None))) == []
    assert alerts(StaleBlocks(), snap(replace(red(), satisfied=None))) == []
    failing = red(kind=ConditionKind.CI_STATUS, expect="failure")
    assert alerts(StaleBlocks(), snap(failing)) == []
    young = NOW - timedelta(minutes=10)
    old = replace(red(), checked_at=young - timedelta(minutes=1))
    assert alerts(StaleBlocks(ci_red_after=timedelta(minutes=1)), snap(old, since=young)) == []
    assert alerts(StaleBlocks(), snap(red(head_ci_red_since="yesterday"))) == []
    assert alerts(StaleBlocks(), snap(red(head_ci_red_since="2026-03-02T13:00:00"))) == []
    assert alerts(StaleBlocks(), snap(red(NOW + timedelta(minutes=5)))) == []
    other = replace(red(), kind=ConditionKind.TASK_STATUS)
    assert all("CI failed" not in a.reason for a in alerts(StaleBlocks(), snap(other)))


def test_grace_counts_from_the_block_when_it_is_younger() -> None:
    young = NOW - timedelta(minutes=20)
    c = replace(red(NOW - timedelta(hours=3)), checked_at=NOW - timedelta(minutes=1))
    assert alerts(StaleBlocks(), snap(c, since=young)) == []
    assert alerts(StaleBlocks(), snap(c, since=NOW - timedelta(minutes=31)))


def test_odd_params_still_alert_without_them() -> None:
    (a,) = alerts(StaleBlocks(), snap(red(pr=True, head_ci_failed="test", head_sha=None)))
    assert "The PR's CI failed on its head (checks not named)" in a.reason
    assert a.params["pr"] is None


def test_reminder_to_the_idle_assignee_while_asks_reach_agents() -> None:
    from kelpie.rules.unanswered_asks import UnansweredAsks

    rule = StaleBlocks(asks=UnansweredAsks())
    _, remind = alerts(rule, snap(red()))
    assert remind.params["recipient"] == "reviewer-1" and remind.once_per_key
    assert remind.key == f"ci-red-remind:T-1:{SINCE.isoformat()}:{SHA}"
    live = Session("s-1", "T-1", "reviewer-1", NOW - timedelta(minutes=5), last_heartbeat=NOW)
    assert len(alerts(rule, snap(red(), sessions=(live,)))) == 1  # busy: not interrupted
    human = snap(red())
    human = replace(human, tasks=(replace(human.tasks[0], owner_id="ceo"),))
    assert len(alerts(rule, human)) == 1  # a person gets the alert, not an agent reminder
    assert len(alerts(StaleBlocks(), snap(red()))) == 1  # asks do not reach agents


def test_merged_still_unblocks() -> None:
    merged = replace(red(), satisfied=True, satisfied_at=NOW - timedelta(minutes=30))
    (a,) = StaleBlocks().evaluate(snap(merged))
    assert a.kind is ActionKind.UNBLOCK


@pytest.mark.parametrize(
    ("config", "message"),
    [({"ci_red_mode": "act"}, "ci_red_mode"), ({"ci_red_after": "0s"}, "positive")],
)
def test_config(config: dict[str, Any], message: str) -> None:
    assert build({"ci_red_after": False}).ci_red_after is None
    assert build({"ci_red_mode": "rule", "ci_red_after": "1h"}).ci_red_after == timedelta(hours=1)
    with pytest.raises(ValueError, match=message):
        build(config)


def test_snoozed_stale_and_overdue() -> None:
    from kelpie.core.model import Snooze

    snooze = Snooze("T-1", NOW + timedelta(days=1), "release", "ceo", NOW - timedelta(hours=1))
    held = replace(snap(red()), snoozes={"T-1": snooze})
    assert alerts(StaleBlocks(), held) == []  # snoozed: the person knows
    (a,) = alerts(StaleBlocks(), snap(red()))
    assert a.params["snooze_task"] == "T-1"
    stale = replace(red(), checked_at=NOW - timedelta(hours=2))
    assert all("CI failed" not in x.reason for x in alerts(StaleBlocks(), snap(stale)))
    # Past max_block both are said: the overdue alert is unchanged.
    late = snap(red(), since=NOW - timedelta(hours=9))
    kinds = sorted(x.key.split(":")[0] for x in alerts(StaleBlocks(), late))
    assert kinds == ["ci-red", "overdue"]


def test_resolved_notice_of_a_ci_red_state_held_by_a_snooze() -> None:
    from kelpie.core.model import Snooze

    rule = StaleBlocks(ci_red_mode="rule")
    (a,) = alerts(rule, snap(red()))
    assert a.state is not None
    snooze = Snooze("T-1", NOW + timedelta(days=1), "release", "ceo", NOW - timedelta(hours=1))
    held = replace(snap(red()), snoozes={"T-1": snooze})
    notice = rule.resolved(a.state, held)
    assert notice is not None and "no reminders" in notice
    assert rule.resolved(a.state, snap(red())) is None  # still reported
