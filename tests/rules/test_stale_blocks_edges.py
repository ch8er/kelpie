"""Edge cases of rule 4 (stale_blocks) that pin boundaries and the rule's own settings."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Block, Condition, ConditionKind, Snapshot, Snooze, Status, Task
from kelpie.rules.needs_you import NeedsYou
from kelpie.rules.stale_blocks import StaleBlocks

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
M = timedelta(minutes=1)
H = timedelta(hours=1)
NAME = "stale_blocks"


def blocked(since: datetime, cid: str | None = "C-1", reason: str = "waiting_pr_merge: #1") -> Task:
    return Task(
        "T-101", "t", Status.BLOCKED, since, owner_id="agent-dev-1",
        block=Block(reason, since, cid),
    )  # fmt: skip


def pr(
    satisfied: bool | None,
    checked: datetime | None,
    at: datetime | None = None,
) -> Condition:
    return Condition(
        "C-1", ConditionKind.PR_MERGED, params={"repo": "example/repo", "pr": 12},
        satisfied=satisfied, satisfied_at=at, checked_at=checked,
    )  # fmt: skip


def run(task: Task, *conditions: Condition, rule: StaleBlocks | None = None) -> list[Action]:
    snap = Snapshot(now=NOW, tasks=(task,), conditions=conditions)
    return list((rule or StaleBlocks()).evaluate(snap))


def test_unlinked_alert_fires_at_exactly_unlinked_alert_after() -> None:
    task = blocked(NOW - 4 * H, cid=None)
    assert run(blocked(NOW - 4 * H + M, cid=None)) == []
    (alert,) = run(task)
    assert alert.key == f"unlinked:T-101:{(NOW - 4 * H).isoformat()}"
    assert alert.since == NOW - 4 * H
    assert alert.severity is Severity.WARNING


def test_evidence_exactly_max_condition_age_old_is_not_stale() -> None:
    """Stale means older than ``max_condition_age``, the same bound as fresh evidence."""
    rule = StaleBlocks(max_condition_age=60 * M, stale_evidence_after=60 * M)
    assert run(blocked(NOW - 2 * H), pr(False, NOW - 60 * M), rule=rule) == []
    (alert,) = run(blocked(NOW - 2 * H), pr(False, NOW - 61 * M), rule=rule)
    assert alert.key is not None and alert.key.startswith("stale-evidence:")


def test_stale_evidence_alert_at_exactly_stale_evidence_after_of_block_age() -> None:
    (alert,) = run(blocked(NOW - H), pr(None, None))
    assert alert.key == f"stale-evidence:T-101:{(NOW - H).isoformat()}"
    assert run(blocked(NOW - H + M), pr(None, None)) == []


def test_stale_evidence_alert_when_last_checked_exactly_stale_evidence_after_ago() -> None:
    (alert,) = run(blocked(NOW - 2 * H), pr(False, NOW - H))
    assert alert.key is not None and alert.key.startswith("stale-evidence:")
    assert run(blocked(NOW - 2 * H), pr(False, NOW - H + M)) == []


def test_unblock_reads_freshness_with_the_rules_max_condition_age() -> None:
    rule = StaleBlocks(max_condition_age=60 * M)
    (unblock,) = run(blocked(NOW - 2 * H), pr(True, NOW - 30 * M, NOW - 40 * M), rule=rule)
    assert unblock.kind is ActionKind.UNBLOCK and unblock.rule == NAME
    assert unblock.params["outlived_minutes"] == 40


def test_a_marker_on_a_block_that_names_a_condition_holds_nothing() -> None:
    """A marker snooze holds only a wait on a person; the inbox's ``condition_reasons`` say
    this block waits on a condition (an automerge), so its alert stays."""
    task = blocked(NOW - 5 * H, cid=None, reason="needs_input: automerge PR #12 until:2026-03-09")
    marker = Snooze("T-101", NOW + 7 * 24 * H, None, None, NOW - H, "marker")
    snap = Snapshot(now=NOW, tasks=(task,), snoozes=MappingProxyType({"T-101": marker}))
    assert StaleBlocks().evaluate(snap) == []  # asks a person: the marker holds it
    inbox = NeedsYou(condition_reasons=(re.compile(r"automerge PR"),))
    (alert,) = StaleBlocks(inbox=inbox).evaluate(snap)
    assert alert.key is not None and alert.key.startswith("unlinked:")
