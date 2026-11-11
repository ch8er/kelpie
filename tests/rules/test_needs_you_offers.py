"""Rule 13 (needs_you) puts an agent's own options on its digest item (#300): the offer, why
one was refused, and an answer once a person gave one from the chat."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.model import (
    Answer,
    Block,
    Comment,
    Condition,
    ConditionKind,
    Owner,
    OwnerKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.core.offers import item_offer, question_key
from kelpie.rules.needs_you import NeedsYou

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
SINCE = NOW - timedelta(hours=2)
OFFER = "Which region?\nOptions:\nA) eu-west\nB) us-east\nRecommend: A"
RULE = NeedsYou(default_approver="human-lead")
OWNERS = (Owner("human-lead", "lead", OwnerKind.HUMAN), Owner("agent-ops-1", "ops"))


def asking(tid: str = "T-1", text: str = OFFER, by: str = "agent-ops-1", **kw: Any) -> Task:
    fields: dict[str, Any] = {
        "role": "ops",
        "owner_id": "agent-ops-1",
        "block": Block("waiting_human: needs_input: which region?", SINCE),
        "comments": (Comment(by, text, SINCE),),
    }
    fields.update(kw)
    return Task(tid, f"agent {tid}", Status.BLOCKED, SINCE, **fields)


def items(*tasks: Task, answers: tuple[Answer, ...] = (), **kw: Any) -> list[dict[str, Any]]:
    snapshot = Snapshot(NOW, tasks=tasks, owners=OWNERS, answers=answers, **kw)
    (action,) = RULE.evaluate(snapshot)
    return list(action.params["items"])


def test_an_approval_item_carries_the_agents_options() -> None:
    (item,) = items(asking())
    offer = item["offer"]
    assert [o["text"] for o in offer["options"]] == ["eu-west", "us-east"]
    assert offer["recommended"] == "A" and offer["approver"] == "human-lead"
    assert offer["owner"] == "agent-ops-1" and offer["block_since"] == SINCE.isoformat()
    assert offer["question"] == question_key("T-1", "agent-ops-1", SINCE, offer["digest"])
    assert item_offer(offer) is not None
    assert "answered" not in offer


def test_a_plain_question_or_another_agents_options_add_nothing() -> None:
    (plain,) = items(asking(text="Which region?"))
    assert "offer" not in plain and "offer_refused" not in plain
    (planted,) = items(asking(by="agent-dev-9"))
    assert "offer" not in planted


def test_a_broken_offer_says_why() -> None:
    (item,) = items(asking(text="Options:\nA) eu-west\nC) us-east"))
    assert "offer" not in item and "out of order" in item["offer_refused"]


def test_an_answered_question_is_marked() -> None:
    (first,) = items(asking())
    key = first["offer"]["question"]
    answer = Answer("T-1", key, "B", "Telegram user 7", NOW, "executed")
    other = Answer("T-1", "0" * 32, "A", "Telegram user 8", NOW, "executed")
    (item,) = items(asking(), answers=(answer, other))
    assert item["offer"]["answered"] == {
        "label": "B",
        "by": "Telegram user 7",
        "at": NOW.isoformat(),
        "pending": False,
        "failed": False,
    }


def test_an_unassigned_question_has_no_approver() -> None:
    rule = NeedsYou()
    snapshot = Snapshot(NOW, tasks=(asking(),), owners=OWNERS)
    (action,) = rule.evaluate(snapshot)
    assert action.params["items"][0]["offer"]["approver"] is None


def test_a_human_task_item_gets_no_buttons_even_with_options_in_its_comments() -> None:
    person = Task(
        "H-1", "Approve budget", Status.READY, SINCE, role="lead", owner_id="human-lead",
        comments=(Comment("human-lead", OFFER, SINCE, True),),
    )  # fmt: skip
    parked = asking(
        "T-2",
        block=Block("waiting_human: needs_input: H-1", SINCE, "c1"),
    )
    cond = Condition("c1", ConditionKind.HUMAN_TASK, "T-2", {"task": "H-1"}, satisfied=False)
    found = items(person, parked, conditions=(cond,))
    assert [i["task_id"] for i in found] == ["H-1"]
    assert "offer" not in found[0]


def test_the_fixture_offers_only_the_owners_own_options() -> None:
    from pathlib import Path

    from kelpie.rules.needs_you import build
    from tests.incidents import load

    path = Path(__file__).parents[1] / "fixtures/incidents/13-approval-with-options.yaml"
    incident = load(path)
    (action,) = build(incident.config).evaluate(incident.snapshot)
    by_task = {i["task_id"]: i for i in action.params["items"]}
    assert [o["label"] for o in by_task["T-1361"]["offer"]["options"]] == ["A", "B"]
    assert by_task["T-1361"]["offer"]["recommended"] == "B"
    assert "offer" not in by_task["T-1362"] and "offer_refused" not in by_task["T-1362"]
