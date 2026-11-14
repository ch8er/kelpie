"""Edges of asks in block reasons (rule ``unanswered_asks`` and rule 4) and of agents'
comment questions to people (rule 13), #461."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.actions import ActionKind
from kelpie.core.model import (
    Block,
    Comment,
    Condition,
    ConditionKind,
    Dependency,
    KelpieMark,
    Owner,
    OwnerKind,
    Snapshot,
    Snooze,
    Status,
    Task,
)
from kelpie.rules import _asks
from kelpie.rules.needs_you import NeedsYou
from kelpie.rules.stale_blocks import StaleBlocks
from kelpie.rules.unanswered_asks import UnansweredAsks, hand_off

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
H = timedelta(hours=1)
OWNERS = (
    Owner("dev-1", "developer"),
    Owner("arch-1", "architect"),
    Owner("person", "owner", OwnerKind.HUMAN),
)


def blocked(tid: str, reason: str, *comments: Comment, **kw: Any) -> Task:
    since = kw.pop("since", NOW - 6 * H)
    return Task(
        tid,
        "t",
        kw.pop("status", Status.BLOCKED),
        since,
        owner_id=kw.pop("owner_id", "dev-1"),
        block=Block(reason, since, **kw.pop("block", {})),
        comments=comments,
        **kw,
    )


def snap(*tasks: Task, **extra: Any) -> Snapshot:
    return Snapshot(now=NOW, tasks=tasks, owners=extra.pop("owners", OWNERS), **extra)


def block_ask(task: Task, **extra: Any) -> _asks.Ask | None:
    return _asks.block_ask(task, snap(task, **extra))


def test_a_block_ask_needs_a_question_an_agent_and_an_agent_owner() -> None:
    assert block_ask(blocked("T", "@arch-1 v3 or v4?")) is not None
    assert block_ask(blocked("T", "@arch-1 v3 then.")) is None  # no question mark
    assert block_ask(blocked("T", "@person v3 or v4?")) is None  # a person: rule 13's
    assert block_ask(blocked("T", "@dev-1 v3 or v4?")) is None  # its own owner
    assert block_ask(blocked("T", "@nobody v3 or v4?")) is None  # no such agent
    assert block_ask(blocked("T", "@arch-1 v3?", owner_id="person")) is None  # a person's task
    assert block_ask(blocked("T", "@arch-1 v3?", owner_id=None)) is None  # nobody asks
    assert block_ask(blocked("T", "@arch-1 v3?", status=Status.WAITING)) is None
    stop = blocked("T", "@arch-1 v3?", block={"kelpie": KelpieMark.STOP})
    assert block_ask(stop) is None  # Kelpie's own stop waits for a person


def test_a_block_with_a_condition_is_the_conditions() -> None:
    linked = blocked("T", "@arch-1 merge #1?", block={"condition_id": "pr:1"})
    pr = Condition("pr:1", ConditionKind.PR_MERGED, checked_at=NOW, params={"pr": 1})
    assert block_ask(linked, conditions=(pr,)) is None
    assert block_ask(linked) is None  # names one Kelpie cannot see: rule 4's broken link


def test_answers_to_a_block_ask() -> None:
    since = NOW - 6 * H
    after = since + H
    assert block_ask(blocked("T", "@arch-1 v3?", Comment("arch-1", "v4", after))) is None
    by_person = Comment("person", "use v4", after, human=True)
    assert block_ask(blocked("T", "@arch-1 v3?", by_person)) is None
    before = Comment("arch-1", "hello", since - H)
    assert block_ask(blocked("T", "@arch-1 v3?", before)) is not None  # before the block
    asker = Comment("dev-1", "still waiting", after)
    assert block_ask(blocked("T", "@arch-1 v3?", asker)) is not None  # the asker is no answer
    # answered on another task of the same machine, naming the asker
    other = Task(
        "O", "o", Status.IN_PROGRESS, since, comments=(Comment("arch-1", "@dev-1 v4", after),)
    )
    task = blocked("T", "@arch-1 v3?")
    assert _asks.block_ask(task, snap(task, other)) is None


def test_the_first_agent_by_name_is_asked() -> None:
    owners = (*OWNERS, Owner("arch-0", "architect"))
    found = block_ask(blocked("T", "@arch-1 or @arch-0, v3?"), owners=owners)
    assert found is not None and found.recipient.id == "arch-0"
    assert found.in_block and found.asker == "dev-1"


def test_reminder_wording_and_key_for_a_block_ask() -> None:
    task = blocked("T", "@arch-1 v3 or v4?")
    [alert] = UnansweredAsks().evaluate(snap(task))
    assert alert.params["source"] == "block"
    assert alert.key is not None and alert.key.endswith(":block")
    assert "dev-1 blocked task T on a question to arch-1" in alert.reason
    assert '"@arch-1 v3 or v4?"' in alert.reason


def test_a_snooze_holds_the_block_ask_and_rule_4_stays_quiet() -> None:
    task = blocked("T", "@arch-1 v3 or v4?")
    snooze = Snooze("T", NOW + 24 * H, "release", "ceo", NOW - H)
    s = snap(task, snoozes={"T": snooze})
    assert list(UnansweredAsks().evaluate(s)) == []
    assert [a for a in StaleBlocks(asks=UnansweredAsks()).evaluate(s) if a.task_id == "T"] == []


def test_hand_off_values() -> None:
    assert hand_off(False) is None
    assert hand_off(True) == UnansweredAsks()
    found = hand_off({"stop_after": "2d"})
    assert found is not None and found.stop_after == 2 * 24 * H
    try:
        hand_off("yes")
    except ValueError as exc:
        assert "must be false, true" in str(exc)
    else:
        raise AssertionError("a string is no hand-off")


def test_rule_4_resolve_notice_names_the_agent_asked() -> None:
    task = blocked("T", "@arch-1 v3 or v4?")
    rule = StaleBlocks(asks=UnansweredAsks())
    state = f"stale_blocks:T:{task.block.since.isoformat()}"  # type: ignore[union-attr]
    notice = rule.resolved(state, snap(task))
    assert notice is not None and "asks arch-1 a question" in notice
    assert StaleBlocks().resolved(state, snap(task)) is None  # without the hand-off: still its own


def test_block_asks_do_not_storm_while_handed_off() -> None:
    tasks = [blocked(f"T-{i}", "@arch-1 v3 or v4?", since=NOW - H) for i in range(6)]
    on = StaleBlocks(asks=UnansweredAsks(), storm_mode="rule").evaluate(snap(*tasks))
    assert [a for a in on if a.kind is ActionKind.ALERT] == []
    off = StaleBlocks(storm_mode="rule").evaluate(snap(*tasks))
    assert any("tasks blocked within" in a.reason for a in off)


def _question(at: datetime, *more: Comment, status: Status = Status.IN_PROGRESS) -> Task:
    return Task(
        "Q",
        "q",
        status,
        NOW - 9 * H,
        owner_id="dev-1",
        comments=(Comment("dev-1", "@person drop v1?", at), *more),
    )


def test_a_question_to_a_person_is_answered_by_any_person_on_the_task() -> None:
    asked = NOW - 2 * H
    [digest] = NeedsYou().evaluate(snap(_question(asked)))
    item = digest.params["items"][0]
    assert item["kind"] == "question" and item["asker"] == "dev-1"
    assert item["needed"] == "@person drop v1?"
    lead = Comment("lead", "ok", asked + H, human=True)
    assert NeedsYou().evaluate(snap(_question(asked, lead))) == []
    agent = Comment("arch-1", "I think so", asked + H)
    assert len(NeedsYou().evaluate(snap(_question(asked, agent)))) == 1  # not an answer


def test_a_question_is_capped_and_one_item_per_task_and_person() -> None:
    long = "@person " + "x" * 400 + "?"
    task = Task(
        "Q",
        "q",
        Status.IN_PROGRESS,
        NOW - 9 * H,
        owner_id="dev-1",
        comments=(
            Comment("dev-1", long, NOW - 3 * H),
            Comment("arch-1", "@person and v2?", NOW - 2 * H),
        ),
    )
    [digest] = NeedsYou().evaluate(snap(task))
    assert digest.params["items_total"] == 1
    needed = digest.params["items"][0]["needed"]
    assert len(needed) == 300 and needed.endswith("…")


def test_a_persons_own_listed_task_is_not_listed_twice() -> None:
    """An agent's question on a human task already in the person's digest adds no line."""
    human = Task("H", "store listing", Status.READY, NOW - 9 * H, owner_id="person")
    waiting = Task("A", "a", Status.READY, NOW - 5 * H, owner_id="dev-1")
    asked = Task(
        "H",
        "store listing",
        Status.READY,
        NOW - 9 * H,
        owner_id="person",
        comments=(Comment("dev-1", "@person screenshots ready?", NOW - 3 * H),),
    )
    deps = (Dependency("A", "H"),)
    [plain] = NeedsYou().evaluate(snap(human, waiting, dependencies=deps))
    [with_q] = NeedsYou().evaluate(snap(asked, waiting, dependencies=deps))
    assert plain.params["items_total"] == with_q.params["items_total"] == 1
    assert with_q.params["items"][0]["kind"] == "human_task"
