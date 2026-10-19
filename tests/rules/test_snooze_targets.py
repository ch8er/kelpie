"""Which alert items a person may snooze from the chat (#176): rules 13, 4 and 2 name the
task (``snooze_task``) exactly where a snooze on it would hold the alert, and nowhere else."""

from __future__ import annotations

from pathlib import Path

from kelpie.core.model import Condition, ConditionKind, Status, Task
from kelpie.rules.needs_you import build as build_needs_you
from kelpie.rules.stale_blocks import StaleBlocks
from kelpie.rules.wait_condition import WaitCondition
from tests.incidents import load
from tests.rules.test_snoozes import NOW, NY, H, blocked, question, snap, waiting

FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"


def test_rule_13_offers_a_snooze_on_each_item_but_a_dead_end() -> None:
    (digest,) = NY.evaluate(snap([question("t_1", NOW - 5 * H), question("t_2", NOW - 6 * H)]))
    assert [(i["task_id"], i["snooze_task"]) for i in digest.params["items"]] == [
        ("t_2", "t_2"),
        ("t_1", "t_1"),
    ]
    incident = load(FIXTURES / "13-snoozed-item-dead-end.yaml")
    (dead,) = build_needs_you(dict(incident.config)).evaluate(incident.snapshot)
    (item,) = dead.params["items"]
    assert item["task_id"] == "H-1" and "snooze_task" not in item  # can never clear: news


def test_rule_4_offers_a_snooze_on_its_reminders_only() -> None:
    rule = StaleBlocks()
    pr = Condition("C-1", ConditionKind.PR_MERGED, "t_1", {"pr": "1"}, False, checked_at=NOW)
    (overdue,) = rule.evaluate(
        snap([blocked("t_1", NOW - 10 * H, "waiting for merge", "C-1")], conditions=[pr])
    )
    assert overdue.key is not None and overdue.key.startswith("overdue:")
    assert overdue.params["snooze_task"] == "t_1"
    (unlinked,) = rule.evaluate(snap([blocked("t_2", NOW - 10 * H, "stuck on the sandbox")]))
    assert unlinked.key is not None and unlinked.key.startswith("unlinked:")
    assert unlinked.params["snooze_task"] == "t_2"
    (unseen,) = rule.evaluate(snap([blocked("t_3", NOW - 10 * H, "waiting for C-9", "C-9")]))
    assert "snooze_task" not in unseen.params  # a reference Kelpie cannot see is broken
    closed = Condition(
        "C-2", ConditionKind.PR_MERGED, "t_4", {"pr": "2", "terminal": True}, False,
        checked_at=NOW,
    )  # fmt: skip
    found = rule.evaluate(
        snap([blocked("t_4", NOW - 10 * H, "waiting for merge", "C-2")], conditions=[closed])
    )
    assert found and all("snooze_task" not in a.params for a in found)  # a dead end


def test_rule_2_offers_a_snooze_on_the_one_human_task_it_waits_for() -> None:
    rule = WaitCondition()
    h = Task("H-1", "sign", Status.READY, NOW - 40 * H, role="owner", owner_id="ceo")
    person = Condition(
        "C-1", ConditionKind.HUMAN_TASK, "W-1", {"task": "H-1"}, False, checked_at=NOW
    )
    (overdue,) = rule.evaluate(snap([h, waiting("W-1", NOW - 30 * H)], conditions=[person]))
    assert overdue.task_id == "W-1" and overdue.params["snooze_task"] == "H-1"
    other = Condition(
        "C-2", ConditionKind.HUMAN_TASK, "W-1", {"task": "H-2"}, False, checked_at=NOW
    )
    ci = Condition("C-3", ConditionKind.CI_STATUS, "W-1", {"ref": "abc"}, False, checked_at=NOW)
    for conditions in ([person, other], [person, ci], [ci]):
        found = rule.evaluate(snap([h, waiting("W-1", NOW - 30 * H)], conditions=conditions))
        assert found and all("snooze_task" not in a.params for a in found)
