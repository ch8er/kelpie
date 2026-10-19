"""Rule 13 (needs_you): the per-person inbox of everything waiting for a human."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    KelpieMark,
    Owner,
    OwnerKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules.needs_you import MAX_ITEMS, NeedsYou, inbox_task_ids

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
OWNERS = (
    Owner("human-owner", "owner", OwnerKind.HUMAN),
    Owner("human-lead", "lead", OwnerKind.HUMAN),
    Owner("agent-dev-1", "developer"),
    Owner("agent-ops-1", "ops"),
)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def human(tid: str, since: datetime, owner: str = "human-owner") -> Task:
    return Task(tid, f"human {tid}", Status.READY, since, role="owner", owner_id=owner)


def blocked(
    tid: str, since: datetime, reason: str, cond: str | None = None, role: str = "developer"
) -> Task:
    return Task(
        tid, f"agent {tid}", Status.BLOCKED, since, role=role, owner_id="agent-dev-1",
        block=Block(reason, since, cond),
    )  # fmt: skip


def waits_on(cid: str, task: str, human_task: str) -> Condition:
    return Condition(cid, ConditionKind.HUMAN_TASK, task, {"task": human_task}, satisfied=False)


def snap(
    tasks: list[Task], conditions: list[Condition] = (), deps: list[Dependency] = ()
) -> Snapshot:  # type: ignore[assignment]
    return Snapshot(NOW, tuple(tasks), OWNERS, tuple(deps), tuple(conditions))


def run(snapshot: Snapshot, **kw: Any) -> list[Any]:
    return NeedsYou(**kw).evaluate(snapshot)


def test_registered_and_planned() -> None:
    assert isinstance(REGISTRY["needs_you"]({}), NeedsYou)


def test_cost_of_delay_orders_items_and_people() -> None:
    tasks = [
        human("H-1", ago(hours=6)),  # 1 agent task, blocked 5h
        human("H-2", ago(hours=6)),  # 2 agent tasks, blocked 2h each: 4 agent-hours
        human("H-3", ago(hours=3), owner="human-lead"),  # 3 tasks, 1h each
        blocked("A-1", ago(hours=5), "waiting_human: H-1", "C-1"),
        blocked("A-2", ago(hours=2), "waiting_human: H-2", "C-2"),
        blocked("A-3", ago(hours=2), "waiting_human: H-2", "C-3"),
        blocked("A-4", ago(hours=1), "waiting_human", "C-4"),
        blocked("A-5", ago(hours=1), "waiting_human", "C-5"),
        blocked("A-6", ago(hours=1), "waiting_human", "C-6"),
    ]
    conds = [
        waits_on("C-1", "A-1", "H-1"),
        waits_on("C-2", "A-2", "H-2"),
        waits_on("C-3", "A-3", "H-2"),
        *(waits_on(f"C-{i}", f"A-{i}", "H-3") for i in (4, 5, 6)),
    ]
    first, second = run(snap(tasks, conds))
    assert first.params["escalate_to"] == "human-owner" and first.params["cost_hours"] == 9.0
    # for the report's agent-hours waiting on people: who waits right now
    assert first.params["waiting_agent_tasks"] == 3 and first.params["waiting"] == [
        "A-1",
        "A-2",
        "A-3",
    ]
    assert first.params["human_tasks"] == ["H-1", "H-2"]  # 5h before 4h
    assert first.kind is ActionKind.ESCALATE and first.state == "needs_you:human-owner"
    assert second.params["escalate_to"] == "human-lead" and second.params["cost_hours"] == 3.0
    assert "By cost of delay: H-1" in first.reason


def test_dependencies_and_transitive_work_count() -> None:
    tasks = [
        human("H-1", ago(hours=2)),
        Task("A-1", "next", Status.READY, ago(hours=2), role="developer"),
        Task("A-2", "after next", Status.BACKLOG, ago(hours=2), role="developer"),
    ]
    deps = [Dependency("A-1", "H-1"), Dependency("A-2", "A-1")]
    [action] = run(snap(tasks, deps=deps))
    # both are held; only the ready one waits (the backlog follow-up waits on A-1 first)
    assert action.params["blocked_agent_tasks"] == 2 and action.params["cost_hours"] == 2.0


def test_a_backlog_chain_behind_a_human_task_waits_at_its_head() -> None:
    """Incident 16 on a platform that keeps dependents in the backlog: the first step
    waits on the human task; the steps after it wait on the first."""
    steps = [Task(f"A-{i}", f"step {i}", Status.BACKLOG, ago(hours=6), role="developer")
             for i in range(1, 6)]  # fmt: skip
    deps = [Dependency("A-1", "H-1"), *(Dependency(f"A-{i + 1}", f"A-{i}") for i in range(1, 5))]
    [action] = run(snap([human("H-1", ago(hours=6)), *steps], deps=deps))
    assert action.params["blocked_agent_tasks"] == 5 and action.params["cost_hours"] == 6.0
    assert action.params["human_tasks"] == ["H-1"]


def test_thresholds() -> None:
    fresh = [human("H-1", ago(minutes=20)), blocked("A-1", ago(minutes=20), "x", "C-1")]
    assert run(snap(fresh, [waits_on("C-1", "A-1", "H-1")])) == []
    question = [blocked("A-1", ago(minutes=29), "needs_input: which region?")]
    assert run(snap(question), default_approver="human-lead") == []
    question = [blocked("A-1", ago(minutes=31), "needs_input: which region?")]
    assert len(run(snap(question), default_approver="human-lead")) == 1


def test_human_task_with_nothing_behind_is_not_in_the_inbox() -> None:
    assert run(snap([human("H-1", ago(days=3))])) == []


def test_unassigned_approvals_and_routing() -> None:
    tasks = [
        blocked("A-1", ago(hours=2), "Waiting_human: approve the schema", role="ops"),
        blocked("A-2", ago(hours=2), "approval needed for release"),
    ]
    by_person = {
        a.params["escalate_to"]: a for a in run(snap(tasks), approvers={"ops": "human-lead"})
    }
    assert set(by_person) == {"human-lead", None}
    unassigned = by_person[None]
    assert unassigned.params["approvals"] == ["A-2"] and "Nobody is assigned" in unassigned.reason
    assert unassigned.state == "needs_you:unassigned"


def test_a_block_tied_to_a_human_task_is_not_also_an_approval() -> None:
    tasks = [human("H-1", ago(hours=3)), blocked("A-1", ago(hours=2), "waiting_human: H-1", "C-1")]
    [action] = run(snap(tasks, [waits_on("C-1", "A-1", "H-1")]), default_approver="human-lead")
    assert action.params["human_tasks"] == ["H-1"] and action.params["approvals"] == []


def test_human_owned_blocked_tasks_are_not_approvals() -> None:
    task = Task(
        "H-9", "mine", Status.BLOCKED, ago(hours=3), owner_id="human-owner",
        block=Block("waiting_human: vendor reply", ago(hours=3)),
    )  # fmt: skip
    assert run(snap([task]), default_approver="human-lead") == []


def test_severity_and_key() -> None:
    tasks = [blocked(f"A-{i}", ago(hours=5), "needs_input") for i in range(6)]
    [action] = run(snap(tasks), default_approver="human-lead", critical_after_hours=24)
    assert action.severity is Severity.CRITICAL and "and 1 more" in action.reason
    assert action.key == "needs_you:human-lead:" + ",".join(sorted(f"A-{i}" for i in range(6)))
    [fewer] = run(snap(tasks[:2]), default_approver="human-lead")
    assert fewer.severity is Severity.WARNING and fewer.key != action.key
    assert fewer.since == ago(hours=5)


@pytest.mark.parametrize(
    "config",
    [
        {"human_wait_alert_after": "0m"},
        {"approval_wait_after": "soon"},
        {"approval_reasons": []},
        {"approvers": {"ops": ""}},
        {"default_approver": ""},
        {"critical_after_hours": 0},
        {"critical_after_hours": True},
        {"bogus": 1},
    ],
)
def test_config_errors(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        REGISTRY["needs_you"](config)


def test_config_values() -> None:
    rule = REGISTRY["needs_you"](
        {
            "human_wait_alert_after": "2h",
            "approval_wait_after": "10m",
            "approval_reasons": ["Ask_Human"],
            "approvers": {"ops": " human-lead "},
            "default_approver": "human-owner",
            "critical_after_hours": 8,
        }
    )
    assert isinstance(rule, NeedsYou)
    assert rule.approval_reasons == ("ask_human",) and rule.approvers == {"ops": "human-lead"}
    assert rule.human_wait_alert_after == timedelta(hours=2) and rule.critical_after_hours == 8.0


# --- review findings ------------------------------------------------------------------


def test_a_task_behind_a_human_task_is_not_also_an_approval() -> None:
    tasks = [human("H-1", ago(hours=3)), blocked("A-1", ago(hours=2), "waiting_human: H-1")]
    actions = run(snap(tasks, deps=[Dependency("A-1", "H-1")]), default_approver="human-lead")
    assert [(a.params["escalate_to"], a.params["approvals"]) for a in actions] == [
        ("human-owner", [])
    ]


def test_a_dead_human_task_condition_reaches_a_person_through_rule_4() -> None:
    """The human task it waited on was cancelled: rule 4's dead-end alert (with what is
    stuck behind it) tells the person, so the inbox does not list it a second time. A
    condition the connector could not evaluate is still a question for a person."""
    ended = Condition(
        "C-1", ConditionKind.HUMAN_TASK, "A-1", {"task": "H-gone", "terminal": True},
        satisfied=False, checked_at=ago(minutes=1),
    )  # fmt: skip
    tasks = [blocked("A-1", ago(hours=2), "waiting_human: H-gone", "C-1")]
    assert run(snap(tasks, [ended]), default_approver="human-lead") == []
    dead = REGISTRY["stale_blocks"]({"needs_you": {"default_approver": "human-lead"}})
    [alert] = dead.evaluate(snap(tasks, [ended]))
    assert alert.key.startswith("dead-end:A-1")
    unknown = replace(ended, params={"task": "H-gone"}, satisfied=None)
    [action] = run(snap(tasks, [unknown]), default_approver="human-lead")
    assert action.params["approvals"] == ["A-1"]
    # a block naming a condition Kelpie cannot see is rule 4's too
    assert run(snap([blocked("A-2", ago(hours=2), "needs_input: x", "C-404")])) == []


def test_items_carry_exact_fields_for_the_digest() -> None:
    """``task_id``, ``needed``, ``since`` and an uncut ``blocking``; the older keys stay."""
    chain = [blocked("A-0", ago(hours=5), "waiting_human: H-1", "C-0")] + [
        Task(f"A-{n}", f"step {n}", Status.BACKLOG, ago(hours=5), role="developer")
        for n in range(1, 12)
    ]
    deps = [Dependency(f"A-{n}", f"A-{n - 1}") for n in range(1, 12)]
    stuck = Task(
        "H-2", "Renew the signing certificate", Status.BLOCKED, ago(hours=4), role="owner",
        owner_id="human-owner", block=Block("waiting for the vendor's invoice", ago(hours=4)),
    )  # fmt: skip
    tasks = [
        human("H-1", ago(hours=6)),
        stuck,
        blocked("A-20", ago(hours=3), "waiting_human: H-2", "C-20"),
        *chain,
        blocked("Q-1", ago(hours=2), " needs_input: which certificate should I use? "),
    ]
    conds = [waits_on("C-0", "A-0", "H-1"), waits_on("C-20", "A-20", "H-2")]
    [action] = run(snap(tasks, conds, deps), default_approver="human-owner")
    items = {i["task_id"]: i for i in action.params["items"]}
    gate, blocked_human, approval = items["H-1"], items["H-2"], items["Q-1"]
    assert gate["blocking"] == 12 and len(gate["behind"]) == 10  # the list stays cut
    assert gate["since"] == ago(hours=5).isoformat()
    assert gate["needed"] is None  # just to do: its title says what
    assert blocked_human["needed"] == "waiting for the vendor's invoice"  # what to chase
    assert blocked_human["blocking"] == 1
    assert approval["blocking"] == 0 and approval["behind"] == ["Q-1"]
    assert approval["needed"] == "needs_input: which certificate should I use?"
    assert approval["since"] == ago(hours=2).isoformat() and approval["kind"] == "approval"
    # what the ledger holds and the daily report reads, unchanged
    assert approval["task"] == "Q-1" and approval["waiting_minutes"] == 120
    assert gate["title"] == "human H-1" and gate["cost_hours"] == 5.0


def test_an_approvals_blocking_counts_the_tasks_waiting_behind_it() -> None:
    """T-1 -> D-1 -> D-2: two tasks wait behind the question, as rule 4's stuck_ids."""
    tasks = [
        blocked("T-1", ago(hours=2), "needs_input: which region?"),
        Task("D-1", "next", Status.BACKLOG, ago(hours=2), role="developer"),
        Task("D-2", "after", Status.BACKLOG, ago(hours=2), role="developer"),
    ]
    deps = [Dependency("D-1", "T-1"), Dependency("D-2", "D-1")]
    [action] = run(snap(tasks, deps=deps), default_approver="human-lead")
    [item] = action.params["items"]
    assert (item["task_id"], item["blocking"], item["behind"]) == ("T-1", 2, ["T-1"])
    # the totals agree: the question and the two tasks behind it are held ...
    assert action.params["blocked_agent_tasks"] == 3
    assert "1 item(s) hold 3 agent task(s)" in action.reason
    # ... but only the question waits on the person (the report's agent-hours)
    assert action.params["waiting_agent_tasks"] == 1 and action.params["waiting"] == ["T-1"]


def test_every_item_is_sent_for_the_digest_to_count() -> None:
    tasks = [blocked(f"A-{n:02d}", ago(hours=1 + n), "needs_input") for n in range(14)]
    [action] = run(snap(tasks), default_approver="human-lead")
    assert action.params["items_total"] == 14 and len(action.params["items"]) == 14
    many = [blocked(f"A-{n:03d}", ago(hours=1), "needs_input") for n in range(MAX_ITEMS + 5)]
    [big] = run(snap(many), default_approver="human-lead")
    assert big.params["items_total"] == MAX_ITEMS + 5 and len(big.params["items"]) == MAX_ITEMS


def test_inbox_task_ids_is_what_the_inbox_lists_without_its_waits() -> None:
    """Rule 4 leaves exactly these to rule 13; its own thresholds then apply."""
    tasks = [
        human("H-1", ago(hours=3)),
        blocked("A-1", ago(minutes=5), "waiting_human: H-1", "C-1"),  # under H-1, not yet due
        blocked("Q-1", ago(minutes=5), "needs_input: which key?"),  # approval, not yet due
        blocked("B-1", ago(hours=5), "waiting for CI"),  # not a question
    ]
    farm = snap(tasks, [waits_on("C-1", "A-1", "H-1")])
    assert run(farm) == []  # nothing has waited long enough to be announced
    assert inbox_task_ids(farm, NeedsYou()) == {"H-1", "A-1", "Q-1"}  # items and waits


def test_human_tasks_the_person_cannot_do_yet_are_left_out() -> None:
    tasks = [
        Task("W-1", "agent work first", Status.IN_PROGRESS, ago(hours=4), role="developer",
             owner_id="agent-dev-1"),
        human("H-1", ago(hours=4)),
        blocked("A-1", ago(hours=3), "waiting_human: H-1", "C-1"),
    ]  # fmt: skip
    deps = [Dependency("H-1", "W-1")]
    assert run(snap(tasks, [waits_on("C-1", "A-1", "H-1")], deps)) == []


def test_an_agent_task_behind_two_human_tasks_counts_once() -> None:
    tasks = [
        human("H-1", ago(hours=5)),
        human("H-2", ago(hours=5)),
        Task("A-1", "needs both", Status.READY, ago(hours=3), role="developer"),
    ]
    deps = [Dependency("A-1", "H-1"), Dependency("A-1", "H-2")]
    [action] = run(snap(tasks, deps=deps))
    assert action.params["cost_hours"] == 3.0 and action.severity is Severity.WARNING
    assert len(action.params["human_tasks"]) == 2


def test_a_status_change_of_the_human_task_does_not_reset_the_wait() -> None:
    started = Task("H-1", "h", Status.IN_PROGRESS, ago(minutes=5), role="owner",
                   owner_id="human-owner")  # fmt: skip
    follower = Task("A-1", "next", Status.READY, ago(hours=10), role="developer")
    [action] = run(snap([started, follower], deps=[Dependency("A-1", "H-1")]))
    assert action.params["cost_hours"] == 10.0


@pytest.mark.parametrize(
    "reason",
    [
        "approved by reviewer; waiting for CI",
        "approval-bot: CI red",
        "Waiting_human_review_bot",
        "needs_input_schema_migration failed",
        "waiting_human: transient: provider 503",
    ],
)
def test_reasons_that_do_not_ask_a_person(reason: str) -> None:
    assert run(snap([blocked("A-1", ago(hours=2), reason)]), default_approver="human-lead") == []


@pytest.mark.parametrize(
    "reason", ["waiting_human: needs_input: region?", "needs_input", "Approval: release 1.2"]
)
def test_reasons_that_ask_a_person(reason: str) -> None:
    assert run(snap([blocked("A-1", ago(hours=2), reason)]), default_approver="human-lead")


def test_tasks_kelpie_stopped_say_so() -> None:
    task = blocked("A-1", ago(hours=1), "waiting_human: needs_input: Stopped by Kelpie: loop")
    stopped = replace(task, block=replace(task.block, kelpie=KelpieMark.STOP))
    [action] = run(snap([stopped]), default_approver="human-lead")
    assert "stopped by Kelpie and needs unblocking" in action.reason
    # The words alone (an agent can write them) only ask for input.
    [action] = run(snap([task]), default_approver="human-lead")
    assert "stopped by Kelpie" not in action.reason and "waits for your input" in action.reason


def test_a_kelpie_stop_always_waits_for_a_person() -> None:
    """Whatever approval_reasons say: the adapter verified that Kelpie stopped the run."""
    task = blocked("A-1", ago(hours=1), "Stopped by Kelpie: loop")
    stopped = replace(task, block=replace(task.block, kelpie=KelpieMark.STOP))
    assert run(snap([task]), approval_reasons=("approval",)) == []
    [action] = run(snap([stopped]), approval_reasons=("approval",))
    assert action.params["approvals"] == ["A-1"]


def test_routing_falls_back_to_the_owner_role() -> None:
    task = Task("A-1", "t", Status.BLOCKED, ago(hours=1), owner_id="agent-ops-1",
                block=Block("needs_input", ago(hours=1)))  # fmt: skip
    [action] = run(snap([task]), approvers={"ops": "human-lead"})
    assert action.params["escalate_to"] == "human-lead"


def test_work_waiting_on_something_else_does_not_count() -> None:
    """Only tasks waiting on the human task itself make its cost: a step deeper in the
    chain blocked on CI, or ready behind agent work, waits on something else."""
    tasks = [
        human("H-1", ago(hours=12)),
        Task("A-1", "next", Status.READY, ago(minutes=10), role="developer"),
        blocked("B-1", ago(hours=48), "waiting_ci: build 41"),
    ]
    deps = [Dependency("A-1", "H-1"), Dependency("B-1", "A-1")]
    assert run(snap(tasks, deps=deps)) == []  # A-1 waited 10 minutes: below the threshold
    tasks = [
        human("H-1", ago(hours=12)),
        Task("A-1", "next", Status.READY, ago(hours=1, minutes=5), role="developer"),
        Task("P-1", "agent work", Status.IN_PROGRESS, ago(hours=1), role="developer",
             owner_id="agent-dev-1"),
        Task("R-1", "after it", Status.READY, ago(hours=9), role="developer"),
    ]  # fmt: skip
    deps = [Dependency("A-1", "H-1"), Dependency("P-1", "H-1"), Dependency("R-1", "P-1")]
    [action] = run(snap(tasks, deps=deps))
    assert action.params["cost_hours"] == 1.1 and action.params["blocked_agent_tasks"] == 3


def test_a_blocked_human_task_is_still_its_owners_to_chase() -> None:
    task = Task(
        "H-1",
        "vendor contract",
        Status.BLOCKED,
        ago(hours=5),
        role="owner",
        owner_id="human-owner",
        block=Block("waiting for the vendor", ago(hours=5)),
    )
    agent = blocked("A-1", ago(hours=3), "waiting_human: H-1", "C-1")
    [action] = run(snap([task, agent], [waits_on("C-1", "A-1", "H-1")]),
                   default_approver="human-lead")  # fmt: skip
    assert action.params["escalate_to"] == "human-owner" and action.params["cost_hours"] == 3.0
    assert "is blocked: waiting for the vendor" in action.reason
    before = Task("W-1", "agent work first", Status.IN_PROGRESS, ago(hours=4),
                  role="developer", owner_id="agent-dev-1")  # fmt: skip
    gated = snap([before, task, agent], [waits_on("C-1", "A-1", "H-1")],
                 [Dependency("H-1", "W-1")])  # fmt: skip
    assert run(gated) == []  # agent work comes before it: not the person's to move yet


def test_a_direct_dependent_parked_on_ci_does_not_count() -> None:
    tasks = [
        human("H-1", ago(hours=12)),
        Task("A-1", "next", Status.READY, ago(minutes=10), role="developer"),
        blocked("B-1", ago(hours=48), "waiting_ci: red"),
    ]
    deps = [Dependency("A-1", "H-1"), Dependency("B-1", "H-1")]
    assert run(snap(tasks, deps=deps)) == []  # only A-1 waits on H-1, for 10 minutes


@pytest.mark.parametrize("ready_before", [True, False])
def test_a_question_deeper_behind_a_human_task_still_reaches_a_person(
    ready_before: bool,
) -> None:
    """Deep in the chain (the human task can be done now) or behind a human task that
    waits on agent work: the agent's own question is not the human task's."""
    tasks = [
        human("H-1", ago(hours=12)),
        Task("A-1", "agent work", Status.IN_PROGRESS, ago(hours=1), role="developer",
             owner_id="agent-dev-1"),
        blocked("C-1", ago(hours=3), "waiting_human: needs_input: which region?"),
    ]  # fmt: skip
    deps = (
        [Dependency("A-1", "H-1"), Dependency("C-1", "A-1")]
        if ready_before
        else [Dependency("H-1", "A-1"), Dependency("C-1", "H-1")]
    )
    [action] = run(snap(tasks, deps=deps), default_approver="human-lead")
    assert action.params["approvals"] == ["C-1"] and action.params["human_tasks"] == []


def test_agents_parked_on_their_human_parent_count() -> None:
    """A dispatch gate parks dependents in ``waiting`` on the parent's status; a platform
    may block them with a reason naming the parent. Both wait on the human task."""
    gated = [
        Task(f"A-{i}", f"step {i}", Status.WAITING, ago(hours=5), role="developer",
             block=Block("dispatch gate", ago(hours=5), f"G-{i}"))
        for i in (1, 2, 3)
    ]  # fmt: skip
    conds = [
        Condition(f"G-{i}", ConditionKind.TASK_STATUS, f"A-{i}", {"task": "H-1"}, satisfied=False)
        for i in (1, 2, 3)
    ]
    deps = [Dependency(f"A-{i}", "H-1") for i in (1, 2, 3)]
    [action] = run(snap([human("H-1", ago(hours=6)), *gated], conds, deps))
    assert action.params["cost_hours"] == 15.0 and action.params["human_tasks"] == ["H-1"]
    named = blocked("A-9", ago(hours=5), "dependency: H-1 not done")
    [action] = run(snap([human("H-1", ago(hours=6)), named], deps=[Dependency("A-9", "H-1")]))
    assert action.params["cost_hours"] == 5.0


def test_a_question_behind_a_human_task_nobody_can_do_yet() -> None:
    """Behind a human task that waits on agent work: a block naming it is the human
    task's (nothing to do yet); the agent's own question is still an approval."""
    tasks = [
        Task("P-1", "agent work", Status.IN_PROGRESS, ago(hours=1), role="developer",
             owner_id="agent-dev-1"),
        Task("H-1", "h", Status.BACKLOG, ago(hours=6), role="owner", owner_id="human-owner"),
        blocked("C-1", ago(hours=3), "waiting_human: H-1"),
        blocked("C-2", ago(hours=3), "needs_input: which region?"),
    ]  # fmt: skip
    deps = [Dependency("H-1", "P-1"), Dependency("C-1", "H-1"), Dependency("C-2", "H-1")]
    [action] = run(snap(tasks, deps=deps), default_approver="human-lead")
    assert action.params["approvals"] == ["C-2"]


def test_a_plain_waiting_dependent_counts_and_one_on_ci_does_not() -> None:
    plain = Task("A-1", "next", Status.WAITING, ago(hours=2), role="developer")
    on_ci = blocked("A-2", ago(hours=40), "dependency: H-1, then CI", "CI-1")
    ci = Condition("CI-1", ConditionKind.CI_STATUS, "A-2", {"pr": 7}, satisfied=False)
    deps = [Dependency("A-1", "H-1"), Dependency("A-2", "H-1")]
    [action] = run(snap([human("H-1", ago(hours=6)), plain, on_ci], [ci], deps))
    assert action.params["cost_hours"] == 2.0  # A-2 waits on CI first


# --- a block that waits on a condition is not a question (live 24 h review) -------------


def test_a_block_naming_a_condition_is_not_an_approval() -> None:
    asks = blocked("T-1", ago(hours=2), reason="needs_input: automerge PR #12")
    patterns = (re.compile(r"\bPR\s*#\d+", re.IGNORECASE),)
    assert run(snap([asks]), condition_reasons=patterns) == []
    assert run(snap([asks]))  # without the pattern it reads as a question
    # A linked condition other than a human task: the condition is what it waits on.
    linked = blocked("T-2", ago(hours=2), reason="needs_input: merge first", cond="c-pr")
    pr = Condition("c-pr", ConditionKind.PR_MERGED, "T-2", satisfied=False)
    assert run(snap([linked], [pr])) == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("PR", "list of regular expressions"),
        ([" "], "list of regular expressions"),
        (["(unclosed"], "invalid pattern"),
        (["x?"], "matches an empty reason"),
    ],
)
def test_condition_reasons_are_validated(value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        REGISTRY["needs_you"]({"condition_reasons": value})


def test_a_condition_that_holds_no_longer_hides_a_question() -> None:
    asks = blocked("T-2", ago(hours=2), reason="needs_input: which certificate?", cond="c-pr")
    met = Condition("c-pr", ConditionKind.PR_MERGED, "T-2", satisfied=True)
    [item] = run(snap([asks], [met]))
    assert item.params["approvals"] == ["T-2"]


def test_a_dependent_waiting_on_a_merge_is_not_parked_on_the_human_task() -> None:
    """Behind a ready human task, an agent task blocked on a merge waits on the merge first:
    held, but not the human task's cost."""
    gate = human("H-1", ago(hours=3))
    merge = blocked("A-1", ago(hours=2), reason="needs_input: automerge PR #12")
    deps = [Dependency("A-1", "H-1")]
    patterns = (re.compile(r"\bPR\s*#\d+", re.IGNORECASE),)
    [item] = run(snap([gate, merge], deps=deps))
    assert item.params["human_tasks"] == ["H-1"]
    assert run(snap([gate, merge], deps=deps), condition_reasons=patterns) == []
    # A reason that names the human task is parked on it, pattern or not.
    named = blocked("A-1", ago(hours=2), reason="needs_input: waiting for H-1 (see PR #12)")
    [item] = run(snap([gate, named], deps=deps), condition_reasons=patterns)
    assert item.params["human_tasks"] == ["H-1"]


# --- planned waits are not waiting (incident 20) ----------------------------------------


def test_a_planned_dependent_is_not_waiting_on_the_human_task() -> None:
    gate = human("H-1", ago(hours=150))
    later = Task("A-1", "after release", Status.BACKLOG, ago(hours=143), prework=True)
    behind_later = Task("A-2", "follow-up", Status.BACKLOG, ago(hours=143))
    labelled = Task("A-3", "retro", Status.BACKLOG, ago(hours=143), labels=("post-release",))
    deps = [Dependency("A-1", "H-1"), Dependency("A-2", "A-1"), Dependency("A-3", "H-1")]
    tasks = [gate, later, behind_later, labelled]
    assert run(snap(tasks, deps=deps), planned_labels=frozenset({"post-release"})) == []
    # Without the label, the labelled one waits: 143 agent-hours.
    [item] = run(snap(tasks, deps=deps))
    assert item.params["waiting"] == ["A-3"] and item.params["cost_hours"] == 143.0


def test_a_started_task_waits_on_what_it_names_labels_or_not() -> None:
    """Blocked or waiting, it was started: parked on the human task, or asking a person."""
    gate = human("H-1", ago(hours=3))
    parked = replace(blocked("A-1", ago(hours=2), "waiting_human: H-1", "C-1"), labels=("later",))
    c = waits_on("C-1", "A-1", "H-1")
    for labels in (frozenset({"later"}), frozenset()):
        [item] = run(snap([gate, parked], [c]), planned_labels=labels)
        assert item.params["human_tasks"] == ["H-1"] and item.params["waiting"] == ["A-1"]
    asks = replace(blocked("A-2", ago(hours=2), "needs_input: which key?"), labels=("later",))
    [item] = run(snap([asks]), planned_labels=frozenset({"later"}))
    assert item.params["approvals"] == ["A-2"]


def test_a_planned_human_task_and_a_label_on_a_pr_read_as_rule_6_does() -> None:
    from kelpie.core.model import PullRequest

    deps = [Dependency("A-1", "H-1")]
    dependent = Task("A-1", "next", Status.BACKLOG, ago(hours=10))
    later = replace(human("H-1", ago(hours=150)), labels=("later",))
    assert run(snap([later, dependent], deps=deps), planned_labels=frozenset({"later"})) == []
    # A task a condition parks on the planned human task still waits on it.
    parked = blocked("A-2", ago(hours=5), "waiting_human: H-1", "C-2")
    c = waits_on("C-2", "A-2", "H-1")
    [item] = run(snap([later, parked], [c]), planned_labels=frozenset({"later"}))
    assert item.params["human_tasks"] == ["H-1"] and item.params["waiting"] == ["A-2"]
    pr = PullRequest("org/repo", 12, "A-1: next", branch="a-1", labels=("hold",), task_ids=("A-1",))
    on_pr = replace(snap([human("H-1", ago(hours=150)), dependent], deps=deps), pull_requests=(pr,))
    assert run(on_pr, planned_labels=frozenset({"hold"})) == []
    assert run(on_pr)  # without the label the dependent waits


def test_a_run_kelpie_stopped_needs_a_person_even_when_planned() -> None:
    stopped = blocked("A-1", ago(hours=2), "kelpie: stopped a runaway loop")
    stopped = replace(
        stopped, labels=("later",), block=replace(stopped.block, kelpie=KelpieMark.STOP)
    )
    [item] = run(snap([stopped]), planned_labels=frozenset({"later"}))
    assert item.params["approvals"] == ["A-1"]


@pytest.mark.parametrize("value", ["later", [""], [1]])
def test_planned_labels_are_validated(value: object) -> None:
    with pytest.raises(ValueError, match="planned_labels must be a list"):
        REGISTRY["needs_you"]({"planned_labels": value})
    with pytest.raises(ValueError, match="planned_labels must be a list"):
        REGISTRY["bottlenecks"]({"planned_labels": value})


def test_planned_work_behind_a_human_task_is_reported_apart() -> None:
    from kelpie.rules.needs_you import PLANNED_AGENT_TASKS, PLANNED_TASKS

    gate = human("H-1", ago(hours=6))
    waiting = Task("A-1", "next", Status.BACKLOG, ago(hours=3))
    later = Task("A-2", "after release", Status.BACKLOG, ago(hours=140), prework=True)
    behind_later = Task("A-3", "follow-up", Status.BACKLOG, ago(hours=140))
    deps = [Dependency("A-1", "H-1"), Dependency("A-2", "H-1"), Dependency("A-3", "A-2")]
    [esc] = run(snap([gate, waiting, later, behind_later], deps=deps))
    assert esc.params["waiting"] == ["A-1"] and esc.params["cost_hours"] == 3.0
    assert esc.params[PLANNED_TASKS] == ["A-2"] and esc.params[PLANNED_AGENT_TASKS] == 1
    # Nothing planned: the params are there, empty.
    [esc] = run(snap([gate, waiting], deps=deps[:1]))
    assert esc.params[PLANNED_TASKS] == [] and esc.params[PLANNED_AGENT_TASKS] == 0


def test_planned_params_dedupe_skip_people_and_cap() -> None:
    from kelpie.rules.needs_you import PLANNED_AGENT_TASKS, PLANNED_TASKS

    gates = [human("H-1", ago(hours=6)), human("H-2", ago(hours=6))]
    waiting = Task("A-1", "next", Status.BACKLOG, ago(hours=3))
    shared = Task("P-1", "later", Status.BACKLOG, ago(hours=9), prework=True)
    person = Task(
        "P-2", "a person's", Status.BACKLOG, ago(hours=9), prework=True, owner_id="human-lead"
    )
    many = [
        Task(f"P-{i:02}", "later", Status.BACKLOG, ago(hours=9), prework=True)
        for i in range(10, 35)
    ]
    deps = [Dependency("A-1", "H-1"), Dependency("A-1", "H-2"), Dependency("P-1", "H-1")]
    deps += [Dependency("P-1", "H-2"), Dependency("P-2", "H-1")]
    deps += [Dependency(t.id, "H-1") for t in many]
    [esc] = run(snap([*gates, waiting, shared, person, *many], deps=deps))
    assert "P-2" not in esc.params[PLANNED_TASKS]  # a person's own task is no agent work
    assert esc.params[PLANNED_AGENT_TASKS] == 26  # P-1 once, across both human tasks
    assert len(esc.params[PLANNED_TASKS]) == 20  # capped
