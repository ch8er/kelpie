import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    KelpieMark,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules._tasks import (
    approval_reasons_list,
    asks_a_person,
    condition_patterns,
    is_live,
    last_exit,
    last_seen,
    linked_conditions,
    live_session,
    undeclared_exit,
)

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
H = timedelta(hours=1)


def test_liveness() -> None:
    open_fresh = Session("a", "T-1", "x", NOW - 3 * H, last_heartbeat=NOW - 0.4 * H)
    open_stale = Session("b", "T-1", "x", NOW - 3 * H, last_heartbeat=NOW - 0.5 * H)  # 30m
    no_beat = Session("c", "T-1", "x", NOW - 0.25 * H)
    ended = Session("d", "T-1", "x", NOW - 3 * H, ended_at=NOW - H, last_heartbeat=NOW - 2 * H)
    assert is_live(open_fresh, NOW) and is_live(no_beat, NOW)
    assert not is_live(open_stale, NOW) and not is_live(ended, NOW)
    assert last_seen(ended) == NOW - H and last_seen(no_beat) == NOW - 0.25 * H
    assert is_live(open_stale, NOW, stale_after=2 * H)  # configurable
    snap = Snapshot(now=NOW, sessions=(open_stale, ended, open_fresh))
    assert live_session("T-1", snap) == open_fresh
    assert live_session("T-2", snap) is None


def cond(cid: str, task_id: str | None = "T-1", registered: datetime | None = None) -> Condition:
    return Condition(cid, ConditionKind.CI_STATUS, task_id=task_id, registered_at=registered)


def test_linked_conditions() -> None:
    task = Task("T-1", "t", Status.BLOCKED, NOW - H)
    old, new, other = (
        cond("old", registered=NOW - 3 * H),
        cond("new", registered=NOW - 0.5 * H),
        cond("x", "T-2"),
    )
    snap = Snapshot(now=NOW, tasks=(task,), conditions=(new, old, other, cond("unknown-age")))
    assert linked_conditions(task, snap) == ([new, cond("unknown-age")], None)
    by_block = Task("T-1", "t", Status.BLOCKED, NOW - H, block=Block("guard", NOW - H, "x"))
    assert linked_conditions(by_block, snap) == ([other], None)
    dangling = Task("T-1", "t", Status.BLOCKED, NOW - H, block=Block("guard", NOW - H, "gone"))
    assert linked_conditions(dangling, snap) == ([], "gone")


def test_undeclared_exit() -> None:
    def t(status: Status, since: datetime) -> Task:
        return Task("T-1", "t", status, since)

    ended = Session("S", "T-1", "a", NOW - 3 * H, ended_at=NOW - H)
    snap = lambda task, *ss: Snapshot(now=NOW, tasks=(task,), sessions=ss)  # noqa: E731
    assert (
        undeclared_exit(t(Status.IN_PROGRESS, NOW - 4 * H), snap(t(Status.IN_PROGRESS, NOW), ended))
        is not None
    )
    # Handed to review during the session: declared.
    task = t(Status.REVIEW, NOW - 2 * H)
    assert undeclared_exit(task, snap(task, ended)) is None
    # Review that predates the session: the reviewer left without a verdict.
    task = t(Status.REVIEW, NOW - 4 * H)
    assert undeclared_exit(task, snap(task, ended)) is not None
    # Declared final status, changed since, other statuses, no sessions.
    declared = Session("S", "T-1", "a", NOW - 3 * H, ended_at=NOW - H, final_status=Status.DONE)
    task = t(Status.IN_PROGRESS, NOW - 4 * H)
    assert undeclared_exit(task, snap(task, declared)) is None
    task = t(Status.IN_PROGRESS, NOW - 0.5 * H)
    assert undeclared_exit(task, snap(task, ended)) is None
    task = t(Status.BLOCKED, NOW - 4 * H)
    assert undeclared_exit(task, snap(task, ended)) is None
    task = t(Status.IN_PROGRESS, NOW - 4 * H)
    assert undeclared_exit(task, snap(task)) is None and last_exit(task, snap(task)) is None


def test_asks_a_person() -> None:
    """The one test rules 4 and 13 share for a block that asks a person (#169)."""
    owners = (Owner("ceo", "owner", OwnerKind.HUMAN), Owner("agent-1", "developer"))
    patterns = (re.compile(r"\bPR\s*#\d+", re.IGNORECASE),)

    def asks(
        reason: str,
        *conditions: Condition,
        owner: str | None = "agent-1",
        kelpie: KelpieMark | None = None,
    ) -> bool:
        block = Block(reason, NOW - H, kelpie=kelpie)
        task = Task("T-1", "t", Status.BLOCKED, NOW - H, owner_id=owner, block=block)
        snap = Snapshot(now=NOW, tasks=(task,), owners=owners, conditions=conditions)
        return asks_a_person(task, snap, condition_reasons=patterns)

    assert asks("waiting_human: needs_input: CEO: enable R2")
    assert asks("Needs_Input which certificate?") and asks("approval")
    assert not asks("needs_input_x: no") and not asks("approved by CI")
    assert not asks("waiting_human: transient: provider down")
    assert not asks("needs_input: automerge PR #12")  # waits on a condition it names
    assert not asks("waiting_human: vendor reply", owner="ceo")  # a person's own task
    assert asks("waiting_human: vendor reply", owner="not-in-the-directory")
    assert asks("waiting_human: vendor reply", owner=None)
    # Linked conditions: an unmet one other than a human task is a wait, not a question.
    pending = Condition("C-1", ConditionKind.PR_MERGED, "T-1", satisfied=False)
    assert not asks("needs_input: merge first", pending)
    assert asks("needs_input: merge first", replace(pending, satisfied=True))
    person = Condition("C-2", ConditionKind.HUMAN_TASK, "T-1", {"task": "H-1"}, satisfied=False)
    assert asks("waiting_human: H-1", person)
    # A dead end or a condition Kelpie cannot see is rule 4's alert, not a question.
    ended = replace(person, params={"task": "H-1", "terminal": True}, checked_at=NOW)
    assert not asks("waiting_human: H-1", ended)

    def named(cid: str, *conditions: Condition) -> bool:
        task = Task("T-1", "t", Status.BLOCKED, NOW - H, block=Block("needs_input", NOW - H, cid))
        snap = Snapshot(now=NOW, tasks=(task,), owners=owners, conditions=conditions)
        return asks_a_person(task, snap)

    assert not named("C-404") and named("C-2", person)
    # A run Kelpie stopped waits for a person whatever its words; no block asks nothing.
    assert asks("stopped", kelpie=KelpieMark.STOP)
    bare = Task("T-2", "t", Status.BLOCKED, NOW - H)
    assert not asks_a_person(bare, Snapshot(now=NOW, tasks=(bare,)))


def test_reason_and_pattern_config() -> None:
    assert approval_reasons_list([" Ask_Human "]) == ("ask_human",)
    assert [p.pattern for p in condition_patterns([r"PR\s*#\d+"])] == [r"PR\s*#\d+"]
    for bad in ([], ["", "x"], "needs_input", [1]):
        with pytest.raises(ValueError, match="approval_reasons"):
            approval_reasons_list(bad)
    for bad in ("x", [""], ["("], [".*"]):
        with pytest.raises(ValueError, match="condition_reasons"):
            condition_patterns(bad)
