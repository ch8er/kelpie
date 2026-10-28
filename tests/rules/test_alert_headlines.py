"""The lines each rule writes for its alerts (#169): ``headline``, ``needed``, ``blocking``.

A property over every fixture and every rule, then the wording of each alert kind, table
driven: fixtures where one shows the kind, a small farm built here where none does."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.alerts import render
from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent
from kelpie.core.engine import alert_items
from kelpie.core.incidents import is_outage_state
from kelpie.core.model import (
    Block,
    BudgetScope,
    Build,
    BuildStatus,
    Condition,
    ConditionKind,
    Dependency,
    LimitHit,
    ProviderStatus,
    Resource,
    ResourceKind,
    SessionWait,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules._alerts import MAX_HEADLINE, alert_params, count
from kelpie.rules._chains import behind_lookup
from kelpie.rules.orphan_status import OrphanStatus
from tests.incidents import Incident, load

FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"
ALL = sorted(FIXTURES.glob("*.yaml"))
# Rule 13's digest uses the catalog headline and per-item lines, not a rule headline.
NOT_YET = {"needs_you"}
ALERTS = (ActionKind.ALERT, ActionKind.ESCALATE)
NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def fixture(name: str) -> Incident:
    return load(FIXTURES / f"{name}.yaml")


def evaluate(rule: str, snapshot: Snapshot, config: dict[str, Any] | None = None) -> list[Action]:
    return list(REGISTRY[rule](dict(config or {})).evaluate(snapshot))


def rendered_headline(action: Action, snapshot: Snapshot) -> str:
    event = AlertEvent(
        action.rule,
        f"{action.kind} {action.task_id}",
        action.reason,
        action.severity,
        headline=action.params.get("headline"),
        items=alert_items(action, snapshot),
    )
    return render.layout(event).headline


# --- every fixture, every rule ---------------------------------------------------------


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.stem)
def test_every_alert_has_a_short_headline_and_task_lines_only_with_a_task(path: Path) -> None:
    incident = load(path)
    for rule in sorted(set(REGISTRY) - NOT_YET):
        config = incident.config if rule == incident.rule else {}
        for action in evaluate(rule, incident.snapshot, config):
            if action.kind not in ALERTS:
                assert "headline" not in action.params, action  # an effect says it its way
                continue
            headline = action.params.get("headline")
            assert isinstance(headline, str) and headline.strip(), action
            shown = rendered_headline(action, incident.snapshot)
            assert len(shown) <= MAX_HEADLINE, shown
            assert shown != render.one_line(action.reason, len(shown)), shown  # its own line
            if action.task_id is None:
                assert not {"needed", "blocking"} & action.params.keys(), action
            blocking = action.params.get("blocking")
            assert blocking is None or (isinstance(blocking, int) and blocking >= 0)


# --- each alert kind -------------------------------------------------------------------


@dataclass(frozen=True)
class Kind:
    rule: str
    farm: Callable[[], tuple[Snapshot, dict[str, Any]]]
    headline: str
    needed: str | None = None
    blocking: int | None = None


def from_fixture(
    name: str, change: Callable[[Snapshot], Snapshot] | None = None, **config: Any
) -> Callable[[], tuple[Snapshot, dict[str, Any]]]:
    def farm() -> tuple[Snapshot, dict[str, Any]]:
        incident = fixture(name)
        snapshot = change(incident.snapshot) if change else incident.snapshot
        return snapshot, {**incident.config, **config}

    return farm


def of_other_rule(name: str) -> Callable[[], tuple[Snapshot, dict[str, Any]]]:
    """A fixture made for another rule, read by this one with its default settings."""
    return lambda: (fixture(name).snapshot, {})


def built(snapshot: Snapshot, **config: Any) -> Callable[[], tuple[Snapshot, dict[str, Any]]]:
    return lambda: (snapshot, config)


def ci_build(minutes: float) -> Build:
    return Build(
        "B-1",
        source="ci",
        repo="example/repo",
        workflow="ci",
        branch="task/B-1",
        sha="b1",
        status=BuildStatus.SUCCESS,
        started_at=NOW - 2 * H,
        finished_at=NOW - 2 * H + 10 * M,
        minutes=minutes,
        event="pull_request",
    )


def ci_quota(used: float | None) -> Resource:
    return Resource("ci:x", ResourceKind.CI_MINUTES, used=used, limit=3000.0, unit="minutes")


def provider(**kw: Any) -> ProviderStatus:
    return ProviderStatus("main", **{"ok": True, "auth_ok": True, "checked_at": NOW - M, **kw})


def waiting(block: Block | None = None) -> Task:
    return Task("T-1", "Wait for the release", Status.WAITING, NOW - H, block=block)


def task_budgets(estimated: bool) -> Callable[[Snapshot], Snapshot]:
    """The fixture's budgets, of one task (and estimated, as in subscription mode)."""

    def change(snapshot: Snapshot) -> Snapshot:
        budgets = tuple(
            replace(b, scope=BudgetScope.TASK, subject="T-F302", estimated=estimated)
            for b in snapshot.budgets
        )
        return replace(snapshot, budgets=budgets)

    return change


def cond(cid: str, kind: ConditionKind, *, checked: timedelta = M, **params: Any) -> Condition:
    """An unmet condition of T-1, last checked ``checked`` ago."""
    return Condition(
        cid, kind, task_id="T-1", params=params, satisfied=False, checked_at=NOW - checked
    )


def stuck_farm(*conditions: Condition, hours: float = 5, behind: int = 0) -> Snapshot:
    """T-1 blocked for ``hours`` on a reason that names nothing, with ``behind`` tasks that
    depend on it and the given conditions linked to it."""
    since = NOW - timedelta(hours=hours)
    stuck = Task(
        "T-1", "Ship the importer", Status.BLOCKED, since, block=Block("the vendor", since)
    )
    after = tuple(Task(f"T-{n}", "Next", Status.BACKLOG, since) for n in range(2, 2 + behind))
    return Snapshot(
        now=NOW,
        tasks=(stuck, *after),
        dependencies=tuple(Dependency(t.id, "T-1") for t in after),
        conditions=conditions,
    )


def no_merged_prs(snapshot: Snapshot) -> Snapshot:
    return replace(snapshot, closed_pull_requests=())


KINDS = {
    # ci_economy
    "ci quota runs out": Kind(
        "ci_economy",
        built(
            Snapshot(now=NOW, builds=(ci_build(240.0),), resources=(ci_quota(2900.0),)),
            waste_min_minutes=1000,
        ),
        "CI minutes quota ci:x runs out in about 10.0h",
    ),
    "ci over quota": Kind(
        "ci_economy",
        built(
            Snapshot(now=NOW, builds=(ci_build(240.0),), resources=(ci_quota(None),)),
            waste_min_minutes=1000,
        ),
        "CI would use more than quota ci:x within 30 days",
    ),
    "ci quota exhausted": Kind(
        "ci_economy",
        from_fixture("05-ci-minutes-exhausted"),
        "CI minutes quota ci:example is exhausted: builds will not start",
    ),
    "ci waste": Kind(
        "ci_economy",
        from_fixture("05-superseded-builds"),
        "CI waste: 2 of 7 builds burned 30 min for nothing",
    ),
    # resources
    "credential expires": Kind(
        "resources",
        built(Snapshot(now=NOW, providers=(provider(expires_at=NOW + 5 * H + 30 * M),))),
        "Credential main expires in 5h30m",
    ),
    "credential expired": Kind(
        "resources",
        from_fixture("06-llm-quota-and-expired-auth"),
        "Credential auth:agent-cli on host-a expired",
    ),
    "key rejected": Kind(
        "resources",
        from_fixture("06-provider-quota-exhausted"),
        "LLM provider provider-b rejected its key",
    ),
    "rate limit": Kind(
        "resources",
        built(
            Snapshot(
                now=NOW,
                resources=(
                    Resource("main", ResourceKind.LLM_QUOTA, used=1, limit=100, events=19, at=NOW),
                ),
                limit_hits=(LimitHit("main", NOW - M),),
            )
        ),
        "LLM provider main hit its limit 20 times in 24h",
    ),
    "quota low": Kind(
        "resources",
        from_fixture("06-llm-quota-and-expired-auth"),
        "LLM provider llm:provider-main has 4% of its quota left",
    ),
    "quota exhausted": Kind(
        "resources",
        from_fixture("06-provider-quota-exhausted"),
        "LLM provider provider-main has no quota left: 2 agent tasks stuck",
    ),
    "disk": Kind("resources", from_fixture("03-disk-full"), "Disk host-a:/ at 99%"),
    "memory": Kind(
        "resources", from_fixture("04-host-overload-heavy-queue"), "Memory on host-a at 96%"
    ),
    "load": Kind(
        "resources",
        from_fixture("04-host-overload-heavy-queue"),
        "Load on host-a at 3.4x its cores",
    ),
    "host overload": Kind(
        "resources",
        from_fixture("04-host-overload-heavy-queue"),
        "Host host-a overloaded: 3 heavy tasks at once (limit 1)",
    ),
    "heavy tasks unplaced": Kind(
        "resources",
        from_fixture("04-heavy-tasks-without-host"),
        "3 heavy tasks at once while 1 host is overloaded",
    ),
    "disk fills up": Kind(
        "resources",
        from_fixture("19-disk-growth-spike"),
        "Disk host-b:/ fills up in about 2.7 days (64h)",
    ),
    # loop_breaker
    "soft budget": Kind(
        "loop_breaker",
        from_fixture("field-03-budget-overrun"),
        "Budget agent-daily passed its soft threshold: $41.20 of $40.00",
    ),
    "hard budget": Kind(
        "loop_breaker",
        from_fixture("field-03-budget-overrun"),
        "Budget agent-daily passed its hard limit: $52.80 of $50.00",
    ),
    "hard budget of a task, estimated": Kind(
        "loop_breaker",
        from_fixture("field-03-budget-overrun", task_budgets(estimated=True)),
        "Budget agent-daily passed its hard limit: about $52.80 of $50.00 (estimate)",
        "Raise the budget, or stop the task's work",
    ),
    "budget forecast": Kind(
        "loop_breaker",
        from_fixture("06-subscription-limit-forecast"),
        "Budget subscription-weekly runs out in 2.9 h (estimate)",
    ),
    "provider limit": Kind(
        "loop_breaker",
        from_fixture("06-subscription-limit-forecast"),
        "Provider limit expected in 80 min for budget subscription-weekly",
    ),
    "bill gap": Kind(
        "loop_breaker",
        from_fixture("field-06-bill-disagrees"),
        "The anthropic bill and the traces disagree by $43.30 for 2026-03-04",
    ),
    "runaway": Kind(
        "loop_breaker",
        from_fixture("field-01-heartbeat-runaway"),
        "Runaway run on T-F101: 900 identical consecutive `heartbeat_check` calls",
        "Stop the run if it is still going, then check the task",
    ),
    "error streak": Kind(
        "loop_breaker",
        from_fixture("field-01-heartbeat-runaway"),
        "Runaway run on T-F102: 5 consecutive tool errors",
        "Stop the run if it is still going, then check the task",
    ),
    # orphan_status
    "ownerless": Kind(
        "orphan_status",
        from_fixture("11-orphan-accepted-task"),
        "T-1101 is 'accepted' with no owner",
        "Let agent-merge-1 take it, or pick another owner",
        7,
    ),
    "review undriven": Kind(
        "orphan_status",
        from_fixture("11-review-nobody-driving"),
        "Nothing drives the review of T-1131",
        "Assign a reviewer, or move the task on",
        1,
    ),
    "no next action": Kind(
        "orphan_status",
        from_fixture("11-successful-run-no-next-action"),
        "Nobody acts on T-1141: its run declared 'accepted'",
        "Apply the declared status, or send the task back",
        0,
    ),
    "dependents not woken": Kind(
        "orphan_status",
        from_fixture("11-successful-run-no-next-action"),
        "2 tasks still in backlog after T-1143 finished",
        "Move the waiting tasks to ready",
    ),
    "one dependent not woken": Kind(
        "orphan_status",
        from_fixture("17-prework-not-backlog"),
        "1 task still in backlog after t_c220 finished",
        "Move the waiting tasks to ready",
    ),
    "recovery deep": Kind(
        "orphan_status",
        from_fixture("11-recovery-loop"),
        "Recovery tasks 3 deep, never reaching T-1122",
        "Look at the original problem of T-1122",
    ),
    "recovery cycle": Kind(
        "orphan_status",
        from_fixture("11-recovery-loop"),
        "Recovery tasks recover each other in a cycle from T-1126",
        "Look at the original problem of T-1126",
    ),
    "parent left open": Kind(
        "orphan_status",
        from_fixture("17-parent-left-open"),
        "Parent T-1700 left open: all subtasks finished, PRs merged",
        "Close the parent task",
        2,
    ),
    "parent PRs unmerged": Kind(
        "orphan_status",
        from_fixture("17-parent-prs-unmerged"),
        "Parent t_a080: subtasks finished, but PRs are not merged",
        "Redo or reopen the unmerged PRs, or decide whether to close the parent",
        0,
    ),
    "parent unverified": Kind(
        "orphan_status",
        from_fixture("17-parent-left-open", no_merged_prs),
        "Parent T-1700: subtasks finished, landing not confirmed",
        "Check the family's PRs, then close the parent or reopen work",
        2,
    ),
    "stuck before work": Kind(
        "orphan_status",
        from_fixture("17-card-stuck-scheduled"),
        "T-1750 sits in 'backlog' while 2 tasks wait",
        "Triage it, start it, or drop the dependency",
        2,
    ),
    "stuck before one task": Kind(
        "orphan_status",
        from_fixture("17-prework-not-backlog"),
        "t_c200 sits in 'backlog' while 1 task waits",
        "Triage it, start it, or drop the dependency",
        1,
    ),
    "dead end chain": Kind(
        "orphan_status",
        from_fixture("11-blocker-chain-dead-end"),
        "Dead end in front of T-1171: T-9999 is not on the board (archived or out of reach)",
        "Replace the blocker that can never clear",
        1,
    ),
    # no_progress
    "runaway context": Kind(
        "no_progress",
        from_fixture("field-07-runaway-context"),
        "Session S-F701: 190k-token context, +10,000 per step",
        "Compact the context or bound the session",
    ),
    # wait_condition
    "overdue": Kind(
        "wait_condition",
        from_fixture("10-condition-satisfied-wake"),
        "T-1013 has waited 8.0h for its condition",
        "Check why the condition has not cleared",
        0,
    ),
    "no condition": Kind(
        "wait_condition",
        from_fixture("10-condition-satisfied-wake"),
        "Nothing will wake T-1014: no condition registered",
        "Register a condition for the task, or move it on",
        0,
    ),
    "condition not visible": Kind(
        "wait_condition",
        built(Snapshot(now=NOW, tasks=(waiting(Block("waiting_ci", NOW - H, "C-gone")),))),
        "Nothing will wake T-1: its condition C-gone is not visible",
        "Fix the condition the task points at, or move the task on",
        0,
    ),
    "condition dead end": Kind(
        "wait_condition",
        built(
            Snapshot(
                now=NOW,
                tasks=(waiting(), Task("T-2", "Ship", Status.BACKLOG, NOW - H)),
                dependencies=(Dependency("T-2", "T-1"),),
                conditions=(
                    Condition(
                        "C-1",
                        ConditionKind.HUMAN_TASK,
                        task_id="T-1",
                        params={"task": "H-1", "status": "cancelled", "terminal": True},
                        satisfied=False,
                        checked_at=NOW - M,
                    ),
                ),
            )
        ),
        "Nothing will wake T-1: its condition C-1 can never be met",
        "Replace the condition, or move the task on",
        1,
    ),
    "waits inside sessions": Kind(
        "wait_condition",
        built(
            Snapshot(
                now=NOW,
                waits=tuple(
                    SessionWait(f"S-{i}", NOW - i * M, "sleep", waited=20 * M, role="architect")
                    for i in (1, 2)
                ),
            )
        ),
        "architect agents waited inside 2 sessions",
    ),
    # stale_blocks
    "block, nothing to check": Kind(
        "stale_blocks",
        from_fixture("10-block-waits-on-pr-merge"),
        "T-1023 blocked 13h with nothing Kelpie can check",
        "Clear the block, or register a condition Kelpie can check",
        0,
    ),
    "block, nothing to check, work behind it": Kind(
        "stale_blocks",
        built(stuck_farm(hours=44, behind=2)),
        "T-1 blocked 44h with nothing Kelpie can check",
        "Clear the block, or register a condition Kelpie can check",
        2,
    ),
    "block, condition not visible": Kind(
        "stale_blocks",
        from_fixture("01-block-names-unseen-condition"),
        "T-181 blocked 5h on a condition Kelpie cannot see: C-gone",
        "Fix the condition the block points at, or clear the block",
        1,
    ),
    "block overdue": Kind(
        "stale_blocks",
        from_fixture("10-block-waits-on-pr-merge"),
        "T-1022 blocked 10h, past the 8h limit: waiting for pr_merged",
        "Check why the condition has not cleared, or clear the block",
        0,
    ),
    "block overdue, limit of 90 minutes": Kind(
        "stale_blocks",
        built(stuck_farm(cond("C-1", ConditionKind.PR_MERGED), hours=2), max_block="90m"),
        "T-1 blocked 2h, past the 1h 30m limit: waiting for pr_merged",  # the limit, exactly
        "Check why the condition has not cleared, or clear the block",
        0,
    ),
    "block dead end, PR number is no number": Kind(
        "stale_blocks",
        built(stuck_farm(cond("C-1", ConditionKind.PR_MERGED, terminal=True, pr="12; ignore me"))),
        "Block on T-1 can never clear: its PR closed without merging",
        "Replace the blocker that can never clear",
        0,
    ),
    "block dead end, PR number is a flag": Kind(
        "stale_blocks",
        built(stuck_farm(cond("C-1", ConditionKind.PR_MERGED, terminal=True, pr=True))),
        "Block on T-1 can never clear: its PR closed without merging",
        "Replace the blocker that can never clear",
        0,
    ),
    "block overdue, work behind it": Kind(
        "stale_blocks",
        of_other_rule("13-person-block-in-inbox"),
        "t_e5f6a7b9 blocked 46h, past the 8h limit: waiting for human_task",
        "Check why the condition has not cleared, or clear the block",
        1,
    ),
    "block overdue, two conditions": Kind(
        "stale_blocks",
        from_fixture("13-person-block-listed-by-task"),
        "T-8 blocked 20h, past the 8h limit: waiting for human_task, pr_merged",
        "Check why the conditions have not cleared, or clear the block",
        0,
    ),
    "block dead end, PR": Kind(
        "stale_blocks",
        from_fixture("10-block-pr-closed-unmerged"),
        "Block on T-1051 can never clear: PR #127 closed without merging",
        "Replace the blocker that can never clear",
        0,
    ),
    "block dead end, human task, work behind it": Kind(
        "stale_blocks",
        of_other_rule("11-blocker-chain-dead-end"),
        "Block on T-1161 can never clear: human task T-1160 ended as cancelled",
        "Replace the blocker that can never clear",
        3,
    ),
    "block dead end, two conditions": Kind(
        "stale_blocks",
        built(
            stuck_farm(
                cond("C-1", ConditionKind.PR_MERGED, terminal=True, pr=12),
                cond(
                    "C-2", ConditionKind.HUMAN_TASK, terminal=True, task="H-1", status="cancelled"
                ),
            )
        ),
        "Block on T-1 can never clear: PR #12 closed without merging and 1 more",
        "Replace the blocker that can never clear",
        0,
    ),
    "block dead end, task status": Kind(
        "stale_blocks",
        built(
            stuck_farm(
                cond(
                    "C-1",
                    ConditionKind.TASK_STATUS,
                    terminal=True,
                    task="T-9",
                    observed_status="failed",
                )
            )
        ),
        "Block on T-1 can never clear: task T-9 ended as failed",
        "Replace the blocker that can never clear",
        0,
    ),
    "block dead end, other condition": Kind(
        "stale_blocks",
        built(stuck_farm(cond("C-1", ConditionKind.CI_STATUS, terminal=True))),
        "Block on T-1 can never clear: ci_status condition C-1 can no longer be met",
        "Replace the blocker that can never clear",
        0,
    ),
    "block dead end, PR without a number": Kind(
        "stale_blocks",
        built(stuck_farm(cond("C-1", ConditionKind.PR_MERGED, terminal=True))),
        "Block on T-1 can never clear: its PR closed without merging",
        "Replace the blocker that can never clear",
        0,
    ),
    "block evidence stale": Kind(
        "stale_blocks",
        from_fixture("01-guard-block-after-new-push"),
        "Evidence for T-112's block is stale: ci_status not checked for 3h",
        "Check that whatever checks ci_status conditions is running "
        "(kelpie doctor shows the connectors)",
        0,
    ),
    "block evidence never checked": Kind(
        "stale_blocks",
        of_other_rule("10-condition-satisfied-wake"),
        "Evidence for T-1015's block is stale: ci_status never checked",
        "Check that whatever checks ci_status conditions is running "
        "(kelpie doctor shows the connectors)",
        0,
    ),
    "block evidence stale, two connectors, work behind it": Kind(
        "stale_blocks",
        built(
            stuck_farm(
                cond("C-1", ConditionKind.CI_STATUS, checked=3 * H + 30 * M),
                cond("C-2", ConditionKind.PR_MERGED, checked=2 * H),
                behind=1,
            )
        ),
        "Evidence for T-1's block is stale: ci_status, pr_merged not checked for 2h",
        "Check that whatever checks ci_status and pr_merged conditions is running "
        "(kelpie doctor shows the connectors)",
        1,
    ),
    # bottlenecks
    "idle with one queued task": Kind(
        "bottlenecks",
        from_fixture("17-foreign-pr-mention"),
        "1 agent idle while 1 queued task waits",
    ),
    "idle with one PR": Kind(
        "bottlenecks",
        from_fixture("14-merge-hold-on-pr-labels"),
        "2 agents idle while 1 mergeable PR waits",
    ),
    "human gate, one task": Kind(
        "bottlenecks",
        from_fixture("20-planned-wait-gates"),
        "Human tasks gate 1 agent task while 2 agents sit idle",
    ),
    "forgotten hold": Kind(
        "bottlenecks",
        from_fixture("14-forgotten-merge-hold"),
        "Hold 'merge-hold' looks forgotten: nothing moved for 5h",
    ),
    "chain": Kind(
        "bottlenecks",
        from_fixture("09-sequential-chain-idle"),
        "3 idle agents while the work is one chain of 5 tasks",
    ),
    "role bottleneck": Kind(
        "bottlenecks",
        from_fixture("10-review-queue-bottleneck"),
        "Role 'reviewer' is the bottleneck: 4 tasks queued, 1 agent of other roles idle",
    ),
    "human gate escalated": Kind(
        "bottlenecks",
        from_fixture("16-human-task-gates-chain", escalate_human_gates=True),
        "Human task T-1600 gates 28 agent tasks",
        "Do it first, or remove the link if the agent work does not need it",
        28,
    ),
    # return_causes
    "dominant cause": Kind(
        "return_causes",
        from_fixture("07-disjoint-files"),
        "Top cause of task returns: merge conflict (86% of 7)",
    ),
    "hot files": Kind(
        "return_causes",
        from_fixture("07-shared-file-conflicts"),
        "Merge conflicts concentrate on src/app/routes.py and 1 more file",
    ),
    "flaky test": Kind(
        "return_causes",
        from_fixture("18-flaky-test-returns"),
        "Flaky test LoginTests.testSessionTimeout sent 2 tasks back",
    ),
}


@pytest.mark.parametrize("name", sorted(KINDS))
def test_each_alert_kind_says_what_happened(name: str) -> None:
    kind = KINDS[name]
    snapshot, config = kind.farm()
    found = {
        a.params["headline"]: a for a in evaluate(kind.rule, snapshot, config) if a.kind in ALERTS
    }
    assert kind.headline in found, sorted(found)
    action = found[kind.headline]
    assert action.params.get("needed") == kind.needed
    assert action.params.get("blocking") == kind.blocking
    assert rendered_headline(action, snapshot) == kind.headline  # fits: shown whole


def test_done_without_evidence_says_whether_any_one_would_do() -> None:
    config = {
        "done_evidence": [
            {"name": "tests", "artifacts": ["*test*"]},
            {"name": "ci", "condition": "ci_status"},
        ],
        "done_evidence_action": "alert",
    }
    incident = fixture("02-done-without-evidence")
    task = next(t for t in incident.snapshot.tasks if t.status is Status.DONE)
    run = evaluate("exit_contract", incident.snapshot, config)
    (anyone,) = [a for a in run if a.kind is ActionKind.ALERT and a.task_id == task.id]
    assert anyone.params["headline"] == (
        f"{task.id} moved to done without evidence: one of tests, ci is enough"
    )
    assert anyone.params["needed"] == "Add any one of the listed evidence, or reopen the task"
    every = evaluate("exit_contract", incident.snapshot, {**config, "done_evidence_require": "all"})
    (alert,) = [a for a in every if a.kind is ActionKind.ALERT and a.task_id == task.id]
    assert alert.params["headline"] == (
        f"{task.id} moved to done without evidence: missing tests, ci"
    )
    assert alert.params["needed"] == "Add the missing evidence, or reopen the task"


@pytest.mark.parametrize("path", ALL, ids=lambda p: p.stem)
def test_stale_blocks_counts_what_waits_as_rules_2_3_and_13_do(path: Path) -> None:
    """``blocking`` is the size of the shared graph's ``behind`` (also the alert's
    ``stuck_ids`` where it has them), and the block's own words are in no line of the rule's."""
    incident = load(path)
    snapshot = incident.snapshot
    behind = behind_lookup(snapshot)
    config = incident.config if incident.rule == "stale_blocks" else {}
    for action in evaluate("stale_blocks", snapshot, config):
        if action.kind is not ActionKind.ALERT or is_outage_state(action.state or ""):
            continue  # a block storm is about many tasks (tests/rules/test_block_storm.py)
        assert action.task_id is not None
        waiting = behind(action.task_id)
        assert action.params["blocking"] == len(waiting), action
        assert action.params.get("stuck_ids", waiting) == waiting, action
        task = snapshot.task(action.task_id)
        assert task is not None and task.block is not None
        lines = f"{action.params['headline']} {action.params.get('needed', '')}"
        assert task.block.reason not in lines, action


def blocking_by_task(rule: str, incident: Incident, **config: Any) -> dict[str, int]:
    """What a rule's alerts say waits behind each task: ``blocking`` of an alert about one
    task, or of an item of rule 13's digest (listed or snoozed)."""
    settings = {**(incident.config if incident.rule == rule else {}), **config}
    found: dict[str, int] = {}
    for action in evaluate(rule, incident.snapshot, settings):
        if action.kind not in ALERTS:
            continue
        if rule == "needs_you":
            for item in (*action.params.get("items", ()), *action.params.get("snoozed", ())):
                found[item["task_id"]] = item["blocking"]
        elif action.task_id is not None and "blocking" in action.params:
            found[action.task_id] = action.params["blocking"]
    return found


def test_blocking_agrees_with_rules_2_3_and_13_on_every_task_they_share() -> None:
    """Rule 4 says the same number as rules 2, 3 and 13 for a task they all alert on, with
    rule 13 off and reaching people (which decides which of them alerts)."""
    shared = 0
    for path in ALL:
        incident = load(path)
        for inbox in (False, True):
            ours = blocking_by_task("stale_blocks", incident, needs_you=inbox)
            for rule in ("wait_condition", "orphan_status", "needs_you"):
                theirs = blocking_by_task(rule, incident)
                for task_id in ours.keys() & theirs.keys():
                    assert ours[task_id] == theirs[task_id], (path.stem, rule, task_id, inbox)
                    shared += 1
    assert shared >= 10  # the check is not vacuous


def test_counts_agree_with_their_verbs() -> None:
    assert count(1, "task") == "1 task" and count(0, "task") == "0 tasks"
    assert count(1, "task", "wait") == "1 task waits"
    assert count(3, "task", "wait") == "3 tasks wait"
    assert count(1, "agent", "sit") == "1 agent sits"
    assert count(1, "host", "are") == "1 host is" and count(2, "host", "are") == "2 hosts are"
    assert count(1, "return", "were") == "1 return was"
    assert count(1, "mergeable PR", "wait") == "1 mergeable PR waits"


def test_alert_params() -> None:
    assert alert_params("A  headline\n") == {"headline": "A headline"}
    assert alert_params("H", "Do it", 0) == {"headline": "H", "needed": "Do it", "blocking": 0}
    assert alert_params("H", None, None) == {"headline": "H"}


# --- orphan_status counts what waits as rules 2, 4 and 13 do ---------------------------


def orphan_farm(*others: Task, **snapshot: Any) -> tuple[Snapshot, Action]:
    orphan = Task("T-1", "Fix the importer", Status.IN_PROGRESS, NOW - 2 * H, role="developer")
    farm = Snapshot(now=NOW, tasks=(orphan, *others), **snapshot)
    (alert,) = [
        a
        for a in OrphanStatus().evaluate(farm)
        if a.kind is ActionKind.ALERT and a.task_id == "T-1"
    ]
    return farm, alert


def test_a_task_parked_on_the_orphan_by_a_condition_counts_as_blocked() -> None:
    """Case A: no dependency, a ``task_status`` condition names the orphan. Rule 3's open
    dependents miss it (the severity stays theirs); the graph of rules 2, 4 and 13 has it."""
    parked = Task(
        "T-2",
        "Release notes",
        Status.BLOCKED,
        NOW - H,
        block=Block("waiting on T-1", NOW - H, "C-T1"),
    )
    on_it = Condition("C-T1", ConditionKind.TASK_STATUS, task_id="T-2", params={"task": "T-1"})
    farm, alert = orphan_farm(parked, conditions=(on_it,))
    assert alert.params["dependents"] == 0 and alert.severity is Severity.WARNING
    assert alert.params["blocking"] == len(behind_lookup(farm)("T-1")) == 1


def test_a_chain_through_a_task_in_review_follows_the_shared_graph() -> None:
    """Case B: T-2 (in review, owned) depends on the orphan and T-3 on T-2. A task in
    review is moving, so the shared graph stops there: nothing counts as blocked, while
    rule 3's dependents (and its severity) still see both."""
    review = Task("T-2", "Review", Status.REVIEW, NOW - H, owner_id="rev-1")
    after = Task("T-3", "Deploy", Status.BACKLOG, NOW - H)
    deps = (Dependency("T-2", "T-1"), Dependency("T-3", "T-2"))
    farm, alert = orphan_farm(review, after, dependencies=deps)
    assert alert.params["dependents"] == 2 and alert.severity is Severity.WARNING
    assert alert.params["blocking"] == len(behind_lookup(farm)("T-1")) == 0
    # The reason's count reads as "Blocking: none" does.
    assert "2 open task(s) depend on it (none of them waiting on it yet)." in alert.reason
    _, waited = orphan_farm(
        replace(review, status=Status.BACKLOG, owner_id=None), after, dependencies=deps
    )
    assert waited.params["blocking"] == 2 and "waiting on it yet" not in waited.reason
