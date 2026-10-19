from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules.needs_you import NeedsYou, inbox_task_ids
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


def test_pr_closed_without_merge_alerts_at_once_on_fresh_evidence() -> None:
    closed = replace(
        cond(satisfied=False, at=None), params={"pr_state": "closed", "terminal": True}
    )
    (action,) = run(blocked(since=NOW - 10 * M), closed)
    assert action.kind is ActionKind.ALERT and "closed without merging" in action.reason
    assert not action.about_wait  # a dead end is broken, not long: never folded (incident 20)
    # Evidence from before the block, or a PR still open: no early alert.
    old = replace(closed, checked_at=NOW - 20 * M)  # before the block: not evidence
    assert run(blocked(since=NOW - 10 * M), old) == []
    open_pr = replace(closed, params={"pr_state": "open"})
    assert run(blocked(since=NOW - 10 * M), open_pr) == []
    # Seen closed during the block but not re-checked lately: the alert stays.
    (kept,) = run(blocked(since=NOW - 60 * M), old)
    assert kept.key == action.key.replace((NOW - 10 * M).isoformat(), (NOW - 60 * M).isoformat())


def test_any_terminal_unmet_condition_alerts_at_once() -> None:
    """The flag, not the PR state, is what makes a condition a dead end (human tasks too)."""
    human = Condition(
        "C-1",
        ConditionKind.HUMAN_TASK,
        "T-1",
        params={"task": "T-42", "status": "failed", "terminal": True},
        satisfied=False,
        checked_at=NOW - M,
    )
    (action,) = run(blocked(since=NOW - 10 * M), human)
    assert action.kind is ActionKind.ALERT
    assert "human task T-42 ended as failed" in action.reason
    # Without the flag a closed PR is just an unmet condition; a met one is never a dead end.
    plain = replace(human, params={"task": "T-42", "status": "in_progress"})
    assert run(blocked(since=NOW - 10 * M), plain) == []
    legacy = replace(cond(satisfied=False, at=None), params={"pr_state": "closed"})
    assert run(blocked(since=NOW - 10 * M), legacy) == []
    met = replace(human, satisfied=True, satisfied_at=NOW - 5 * M)
    assert [a.kind for a in run(blocked(since=NOW - 60 * M), met)] == [ActionKind.UNBLOCK]


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
    assert all(a.about_wait for a in run(blocked(None), rule=rule))  # an outage may explain it
    assert run(blocked(None, since=NOW - 119 * M), rule=rule) == []
    [alert] = run(blocked("C-404"), rule=rule)
    assert "C-404" in alert.reason and "cannot see" in alert.reason
    assert not alert.about_wait  # a reference Kelpie cannot see is broken, not long
    assert run(blocked(None), rule=StaleBlocks(unlinked_alert_after=None)) == []


def test_unmet_condition_past_max_block_alerts() -> None:
    rule = StaleBlocks(max_block=180 * M)
    assert alerts(run(blocked(), cond(satisfied=False, at=None), rule=rule)) == ["overdue"]
    assert all(a.about_wait for a in run(blocked(), cond(satisfied=False, at=None), rule=rule))
    assert run(blocked(since=NOW - 179 * M), cond(satisfied=False, at=None), rule=rule) == []


def test_connector_that_stopped_checking_alerts_instead_of_unblocking() -> None:
    rule = StaleBlocks(stale_evidence_after=60 * M)
    assert alerts(run(blocked(), cond(checked=NOW - 61 * M), rule=rule)) == ["stale-evidence"]
    assert not any(a.about_wait for a in run(blocked(), cond(checked=NOW - 61 * M), rule=rule))
    assert alerts(run(blocked(), cond(satisfied=False, checked=None), rule=rule)) == [
        "stale-evidence"
    ]
    # Checked 30 minutes ago: too old to unblock (15m), too recent to alert (1h).
    assert run(blocked(), cond(checked=NOW - 30 * M), rule=rule) == []
    young = blocked(since=NOW - 30 * M)
    assert run(young, cond(checked=None), rule=rule) == []  # block younger than the threshold


# --- blocks that ask a person are rule 13's (#169) -----------------------------------------

INBOX = StaleBlocks(inbox=NeedsYou(), max_block=180 * M)
OWNERS = (Owner("ceo", "owner", OwnerKind.HUMAN), Owner("agent-1", "developer"))


def question(
    cid: str | None = None,
    reason: str = "waiting_human: needs_input: CEO: enable R2",
    owner: str | None = "agent-1",
) -> Task:
    since = NOW - 300 * M
    return Task("T-1", "t", Status.BLOCKED, since, owner_id=owner, block=Block(reason, since, cid))


def person(satisfied: bool | None = False, **params: object) -> Condition:
    return Condition(
        "C-1",
        ConditionKind.HUMAN_TASK,
        params={"task": "H-1", **params},
        satisfied=satisfied,
        satisfied_at=NOW - 60 * M if satisfied else None,
        checked_at=NOW - M,
    )


def human_task(status: Status = Status.READY, prework: bool = False) -> Task:
    return Task("H-1", "Approve", status, NOW - 600 * M, owner_id="ceo", prework=prework)


def on_farm(
    *tasks: Task,
    conditions: tuple[Condition, ...] = (),
    deps: tuple[Dependency, ...] = (),
    rule: StaleBlocks = INBOX,
) -> list:
    return list(rule.evaluate(Snapshot(NOW, tasks, OWNERS, deps, conditions)))


def test_a_question_the_inbox_lists_raises_no_alert_of_rule_4s() -> None:
    assert alerts(run(question())) == ["unlinked"]  # without rule 13 reaching people
    assert run(question(), rule=INBOX) == []  # an approval in the inbox
    # parked on a person's task they can do now: listed under it, so not overdue either
    parked = (question("C-1"), human_task())
    assert alerts(on_farm(*parked, conditions=(person(),))) == []
    off = replace(INBOX, inbox=None)
    assert alerts(on_farm(*parked, conditions=(person(),), rule=off)) == ["overdue"]


@pytest.mark.parametrize("prework", [False, True])  # in the backlog, or in triage
def test_a_question_the_inbox_does_not_list_keeps_rule_4s_alerts(prework: bool) -> None:
    """Rule 13 lists nothing for a person's task they cannot move yet: rule 4 must alert."""
    gate = human_task(Status.BACKLOG, prework)
    assert alerts(on_farm(question("C-1"), gate, conditions=(person(),))) == ["overdue"]
    # Linked by a dependency only, the reason naming the human task. In the backlog it holds
    # the task, which no list shows: rule 4's no-condition alert. A task in triage holds
    # nothing it merely depends on (incident 20), so the block is an approval in the inbox.
    named = question(reason="waiting_human: need H-1 done")
    found = on_farm(named, gate, deps=(Dependency("T-1", "H-1"),))
    assert alerts(found) == ([] if prework else ["unlinked"])
    snap = Snapshot(NOW, (named, gate), OWNERS, (Dependency("T-1", "H-1"),))
    assert ("T-1" in inbox_task_ids(snap, NeedsYou())) is prework


def test_behind_agent_work_the_inbox_lists_nothing_and_rule_4_alerts() -> None:
    work = Task("A-1", "prep", Status.IN_PROGRESS, NOW - 60 * M, owner_id="agent-1")
    deps = (Dependency("H-1", "A-1"),)
    found = on_farm(question("C-1"), human_task(), work, conditions=(person(),), deps=deps)
    assert alerts(found) == ["overdue"]


def test_a_persons_own_blocked_task_in_their_digest_raises_no_alert_of_rule_4s() -> None:
    """S9: an item of the digest, even with a dead end of its own; nothing behind it, no
    digest, so rule 4 alerts."""
    since = NOW - 300 * M
    ended = person(terminal=True, status="cancelled")
    mine = Task("H-1", "Sign", Status.BLOCKED, since, owner_id="ceo", block=Block("vendor", since))
    linked = replace(mine, block=Block("vendor", since, "C-1"))
    behind = Task("T-9", "next", Status.BACKLOG, since, owner_id="agent-1")
    deps = (Dependency("T-9", "H-1"),)
    assert alerts(on_farm(mine, behind, deps=deps)) == []
    assert (
        alerts(on_farm(linked, behind, conditions=(replace(ended, task_id="H-1"),), deps=deps))
        == []
    )
    assert alerts(on_farm(mine)) == ["unlinked"]


def test_a_broken_link_is_not_parked_on_a_persons_task() -> None:
    """Linked by a dependency and naming the person's task, but its block names a
    condition Kelpie cannot see: rule 4's alert, not an item under that task."""
    broken = question("C-404", reason="waiting_human: need H-1 done")
    found = on_farm(broken, human_task(), deps=(Dependency("T-1", "H-1"),))
    assert len(found) == 1 and "cannot see" in found[0].reason


def test_rule_13s_settings_decide_what_asks_a_person() -> None:
    merge = question(reason="needs_input: automerge PR #12")
    patterns = build({"needs_you": {"condition_reasons": [r"PR\s*#\d+"]}})
    assert run(merge, rule=INBOX) == []  # no pattern: a question by its kind
    assert alerts(run(merge, rule=patterns)) == ["unlinked"]
    own = build({"needs_you": {"approval_reasons": ["ask_human"]}})
    assert alerts(run(question(), rule=own)) == ["unlinked"]
    assert run(question(reason="ask_human: which key?"), rule=own) == []
    assert build({"needs_you": True}).inbox == NeedsYou()
    assert build({"needs_you": False}).inbox is None


def test_what_is_not_a_question_stays_rule_4s() -> None:
    # A linked condition other than a human task, still unmet: a wait (overdue).
    assert alerts(run(question("C-1"), cond(satisfied=False, at=None), rule=INBOX)) == ["overdue"]
    # A person's own blocked task: rule 13 does not list it, so rule 4 still alerts.
    assert alerts(on_farm(question(owner="ceo"))) == ["unlinked"]


def test_what_rule_13_does_not_list_still_alerts_and_the_block_still_clears() -> None:
    # The person's task it waited on was cancelled: a dead end, with what is stuck behind it
    # (rule 3 leaves that to this alert; rule 13 does not list the block).
    ended = person(terminal=True, status="cancelled")
    [dead] = run(question("C-1"), ended, rule=INBOX)
    assert dead.key.startswith("dead-end:") and "stuck_ids" in dead.params
    # A condition reference Kelpie cannot see is broken, whoever the block asks.
    [missing] = run(question("C-404"), rule=INBOX)
    assert "cannot see" in missing.reason
    # A connector that stopped checking the person's task.
    stale = replace(person(), checked_at=NOW - 120 * M)
    assert alerts(run(question("C-1"), stale, rule=INBOX)) == ["stale-evidence"]
    # The person's task is done: the block is stale and is cleared as before.
    [unblock] = run(question("C-1"), person(satisfied=True), rule=INBOX)
    assert unblock.kind is ActionKind.UNBLOCK


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
        {"needs_you": "yes"},
        {"needs_you": {"approval_reasons": []}},
        {"needs_you": {"condition_reasons": ["("]}},
        {"approval_reasons": ["x"]},  # rule 13's, not a key of this rule
    ):
        with pytest.raises(ValueError):
            build(bad)
