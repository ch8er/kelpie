"""What tasks waited for, and the day's biggest wait (kelpie.report.waits).

The day (2026-03-02, UTC): the reviewer's tasks R1-R4 wait for CI from 08:00 (16 task-hours
each, 64 in all) after a two-hour review; a developer task waits on a human task, one is
held by a hold label, one is on a person's inbox (rule 13), one waits on nothing known.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Owner,
    OwnerKind,
    Session,
    Status,
    Task,
)
from kelpie.report.daily import ReportInputs, build_report, render_markdown, render_summary
from kelpie.report.sections.throughput import throughput_section
from kelpie.report.throughput import throughput
from kelpie.report.waits import MIN_HOURS, UNKNOWN, WaitCauses, biggest_wait

DAY = date(2026, 3, 2)
START = datetime(2026, 3, 2, tzinfo=UTC)
END = START + timedelta(days=1)
OWNERS = (
    Owner("dev-1", "developer"),
    Owner("rev-1", "reviewer"),
    Owner("person-a", "lead", OwnerKind.HUMAN),
)


def at(hour: float) -> datetime:
    return START + timedelta(hours=hour)


def waiting(tid: str, role: str, **kw: Any) -> Task:
    owner = "rev-1" if role == "reviewer" else "dev-1"
    return Task(tid, tid, Status.WAITING, at(8), role=role, owner_id=owner, **kw)


def ci(tid: str, repo: str = "example/app", **kw: Any) -> Condition:
    return Condition(f"c-{tid}", ConditionKind.CI_STATUS, tid, {"repo": repo, "pr": 1}, **kw)


def ledger(rule: str, **params: Any) -> LedgerEntry:
    return LedgerEntry(
        at=at(9),
        fingerprint=f"{rule}-{sorted(params)}",
        subject="s",
        rule=rule,
        kind=ActionKind.ALERT,
        mode=Mode.ACT,
        outcome=Outcome.ALERTED,
        reason="r",
        params=params,
    )


def inbox(
    approvals: tuple[str, ...] | list[str] = (),
    waiting: tuple[str, ...] | list[str] = (),
    behind: tuple[str, ...] | list[str] = (),
) -> LedgerEntry:
    """A rule 13 escalation, params as the rule writes them."""
    return ledger(
        "needs_you",
        escalate_to="person-a",
        human_tasks=["H-1"],
        approvals=list(approvals),
        blocked_agent_tasks=len(behind),
        waiting_agent_tasks=len(waiting),
        waiting=list(waiting),
        cost_hours=3.0,
        items=[
            {
                "kind": "human_task",
                "task": "H-1",
                "title": "t",
                "behind": list(behind),
                "waiting_minutes": 60,
                "cost_hours": 3.0,
            }
        ],
    )


REVIEWED = tuple(Session(f"S{i}", f"R{i}", "rev-1", at(6), at(8)) for i in range(1, 5))
TASKS = (
    *(waiting(f"R{i}", "reviewer") for i in range(1, 5)),
    waiting("D1", "developer"),  # on a human task
    waiting("D2", "developer", labels=("merge-hold",)),
    waiting("D3", "developer"),  # on rule 13's list
    waiting("D4", "developer"),  # nothing known
)
CONDITIONS = (
    ci("R1"),
    ci("R2"),
    ci("R3", repo="example/lib"),
    ci("R4", satisfied=True),
    Condition("h-1", ConditionKind.HUMAN_TASK, "D1", {"task": "H-1"}),
)
ENTRIES = (inbox(waiting=["D3"]),)


def causes() -> WaitCauses:
    return WaitCauses(CONDITIONS, ENTRIES, ("merge-hold",))


def test_each_task_waits_for_its_cause() -> None:
    of = causes()
    by_id = {t.id: of(t) for t in TASKS}
    assert by_id == {
        "R1": "ci",
        "R2": "ci",
        "R3": "ci",
        "R4": "ci",
        "D1": "people",
        "D2": "hold",
        "D3": "people",
        "D4": UNKNOWN,
    }


def test_the_condition_a_block_names_wins_then_unsatisfied_then_latest() -> None:
    blocked = Task("B", "B", Status.BLOCKED, at(8), block=Block("x", at(8), condition_id="q"))
    conditions = (
        Condition("q", ConditionKind.QUOTA_RESET, None),
        Condition("m", ConditionKind.PR_MERGED, "B"),
    )
    assert WaitCauses(conditions, ())(blocked) == "quota"
    t = Task("T", "T", Status.WAITING, at(8))
    two = (
        Condition("old", ConditionKind.TIME, "T", registered_at=at(1)),
        Condition("new", ConditionKind.TASK_STATUS, "T", registered_at=at(2), satisfied=True),
    )
    assert WaitCauses(two, ())(t) == "timer"  # the unsatisfied one
    met = (replace(two[0], satisfied=True), two[1])
    assert WaitCauses(met, ())(t) == "tasks"  # all met: the latest registered


def test_rule_13_names_approvals_and_tasks_parked_on_a_person_only() -> None:
    entries = (
        inbox(approvals=["A1"], waiting=["W1"], behind=["B1"]),
        ledger("needs_you", state="needs_you:person-b"),  # a resolution notice
        ledger("bottlenecks", hold="freeze", held_tasks=["H1"]),  # held tasks are queued
    )
    of = WaitCauses((), entries)
    plain = {t: of(Task(t, t, Status.WAITING, at(8))) for t in ("A1", "W1", "B1", "H1")}
    assert plain == {"A1": "people", "W1": "people", "B1": UNKNOWN, "H1": UNKNOWN}


def test_what_a_task_waits_on_now_beats_rule_13() -> None:
    """Downstream of a human task but blocked on its own CI: a CI wait, not a person's."""
    blocked = Task("C1", "C1", Status.BLOCKED, at(8), block=Block("ci", at(8), "c-C1"))
    entries = (inbox(waiting=["C1", "C2"]),)
    assert WaitCauses((ci("C1"),), entries)(blocked) == "ci"
    met = WaitCauses((ci("C2", satisfied=True),), entries)  # CI green: the person it is
    assert met(waiting("C2", "reviewer")) == "people"


def test_waits_by_cause_per_role_and_the_biggest() -> None:
    roles = {
        r.role: r
        for r in throughput(TASKS, REVIEWED, OWNERS, (), START, END, wait_cause=causes().split)
    }
    rev, dev = roles["reviewer"], roles["developer"]
    assert rev.waits == {"ci": pytest.approx(64.0)}
    assert rev.waited == {"ci": ("R1", "R2", "R3", "R4")}
    assert dev.waits == {
        "people": pytest.approx(32.0),
        "hold": pytest.approx(16.0),
        UNKNOWN: pytest.approx(16.0),
    }
    wait = biggest_wait(list(roles.values()), TASKS, causes())
    assert wait is not None
    assert (wait.role, wait.cause, wait.hours) == ("reviewer", "ci", pytest.approx(64.0))
    assert wait.work_hours == pytest.approx(2.0)  # one reviewer, sessions 06-08
    assert wait.repo == "example/app"  # two of three named repositories
    assert wait.headline().startswith(
        "Biggest wait: reviewer tasks spent 64.0 task-hours waiting for CI (4 task(s), "
        "mostly on `example/app`), against 2.0 agent-hours of work. Suggestion: add CI capacity"
    )


def test_no_wait_no_headline() -> None:
    roles = throughput(TASKS, REVIEWED, OWNERS, (), START, END)  # no causes asked
    assert all(not r.waits for r in roles)
    assert biggest_wait(roles, TASKS, causes()) is None
    short = Task("S", "S", Status.WAITING, END - timedelta(hours=MIN_HOURS / 2), role="developer")
    roles = throughput((short,), (), OWNERS, (), START, END, wait_cause=causes().split)
    assert biggest_wait(roles, (short,), causes()) is None


def inputs(**kw: Any) -> ReportInputs:
    base: dict[str, Any] = {
        "day": DAY,
        "tz": UTC,
        "tasks": TASKS,
        "owners": OWNERS,
        "sessions": REVIEWED,
        "conditions": CONDITIONS,
        "actions": ENTRIES,
        "hold_labels": ("merge-hold",),
    }
    return ReportInputs(**{**base, **kw})


def test_the_report_headlines_the_biggest_wait() -> None:
    report = build_report(inputs(), sections={"throughput": throughput_section})
    assert report.biggest_wait is not None and report.biggest_wait.cause == "ci"
    markdown = render_markdown(report)
    head = markdown.split("## Cost of chaos")[0]
    assert "Biggest wait: reviewer tasks spent 64.0 task-hours waiting for CI" in head
    assert "Biggest wait: reviewer" in render_summary(report)
    titles = [a.title for a in report.recommendations]
    assert "Cut the reviewer wait for CI" in titles
    assert "**Waiting for conditions, by cause**" in markdown
    assert "- developer: people (approvals, human tasks) 32.0 (2); holds 16.0 (1);" in markdown


def test_an_unknown_biggest_wait_is_headlined_without_advice() -> None:
    lone = (waiting("D4", "developer"),)
    report = build_report(inputs(tasks=lone, sessions=(), conditions=(), actions=()), sections={})
    assert report.biggest_wait is not None and report.biggest_wait.cause == UNKNOWN
    assert "register a condition" in render_markdown(report)
    assert not any(a.title.startswith("Cut the") for a in report.recommendations)


def test_role_names_are_escaped_in_the_headline() -> None:
    odd = (Task("Z", "Z", Status.WAITING, at(8), role="dev|*ops*"),)
    report = build_report(inputs(tasks=odd, sessions=(), conditions=(), actions=()), sections={})
    assert report.biggest_wait is not None
    assert "dev\\|\\*ops\\* tasks" in render_markdown(report)


def test_only_the_day_s_ledger_counts() -> None:
    yesterday = replace(inbox(waiting=["D4"]), at=at(-20))
    report = build_report(
        inputs(
            tasks=(waiting("D4", "developer"),), sessions=(), conditions=(), actions=(yesterday,)
        ),
        sections={},
    )
    assert report.biggest_wait is not None and report.biggest_wait.cause == UNKNOWN


def test_waiting_for_a_review_is_no_work() -> None:
    in_review = Task("V", "V", Status.REVIEW, at(0), role="reviewer", owner_id="rev-1")
    tasks = (*TASKS, in_review)
    roles = throughput(tasks, REVIEWED, OWNERS, (), START, END, wait_cause=causes().split)
    wait = biggest_wait(roles, tasks, causes())
    assert wait is not None and wait.work_hours == pytest.approx(2.0)


def test_a_wait_a_block_reason_declares_counts_by_its_pattern_kind() -> None:
    import re

    from kelpie.report.waits import BlockKind

    kinds = (
        BlockKind(
            re.compile(r"waiting for CI of #(?P<pr>\d+)", re.I),
            ConditionKind.CI_STATUS,
            "example/app",
        ),
        BlockKind(re.compile(r"merge of (?P<pr>\S+#\d+)", re.I), ConditionKind.PR_MERGED),
    )
    of = WaitCauses((), (inbox(waiting=["B1"]),), (), kinds)
    ci_wait = Task("B1", "B1", Status.BLOCKED, at(8), block=Block("Waiting for CI of #12", at(8)))
    assert of(ci_wait) == "ci"  # not the person rule 13 lists it under
    found = of.condition(ci_wait)
    assert found is not None and found.params == {"repo": "example/app"}
    merge = Task("B2", "B2", Status.BLOCKED, at(8), block=Block("merge of example/lib#3", at(8)))
    assert of(merge) == "merge"
    assert of.condition(merge).params == {"repo": "example/lib"}  # type: ignore[union-attr]
    other = Task("B3", "B3", Status.BLOCKED, at(8), block=Block("flaky disk", at(8)))
    assert of(other) == UNKNOWN
    known = WaitCauses((Condition("q", ConditionKind.QUOTA_RESET, "B1"),), (), (), kinds)
    assert known(ci_wait) == "quota"  # a registered unmet condition comes first


def test_a_declared_ci_wait_heads_the_report() -> None:
    import re

    from kelpie.report.waits import BlockKind

    kinds = (BlockKind(re.compile(r"CI of (?P<pr>\S*)", re.I), ConditionKind.CI_STATUS),)
    reason = "waiting for CI of https://github.example/example/app/pull/12"
    blocked = tuple(
        Task(f"B{i}", "b", Status.BLOCKED, at(8), role="reviewer", block=Block(reason, at(8)))
        for i in range(3)
    )
    report = build_report(
        inputs(tasks=blocked, sessions=(), conditions=(), actions=(), block_kinds=kinds),
        sections={},
    )
    assert report.biggest_wait is not None and report.biggest_wait.cause == "ci"
    assert "waiting for CI (3 task(s), mostly on `example/app`)" in render_markdown(report)
    empty = Block("CI of ", at(8))  # no PR named: no declared wait
    of = WaitCauses((), (), (), kinds)
    assert of(Task("E", "e", Status.BLOCKED, at(8), block=empty)) == UNKNOWN


# --- CI outage (incident 20, track F's rule 9 incident) ---------------------------------


def outage_state(key: str, since: datetime, last: datetime) -> Any:
    from kelpie.core.actions import Severity
    from kelpie.core.states import OpenState
    from kelpie.report.store import StateRow

    sid = f"ci_economy:outage:{key}"
    return StateRow(OpenState(sid, "ci_economy", since, since, last, 0, Severity.WARNING, "s"))


def outage_sample(when: datetime, **params: Any) -> LedgerEntry:
    return replace(ledger("ci_economy", **params), at=when)


def test_a_red_ci_comes_before_the_merge_it_holds_up() -> None:
    t = Task("M1", "m", Status.WAITING, at(8))
    both = (
        Condition("ci", ConditionKind.CI_STATUS, "M1", {"repo": "x/y"}, registered_at=at(1)),
        Condition("mg", ConditionKind.PR_MERGED, "M1", {"repo": "x/y"}, registered_at=at(2)),
    )
    assert WaitCauses(both, ())(t) == "ci"
    green = (replace(both[0], satisfied=True), both[1])
    assert WaitCauses(green, ())(t) == "merge"
    from kelpie.report.waits import WAIT_CAUSES

    assert "merges" not in WAIT_CAUSES["merge"]  # not "waiting for merges" while CI may be red


def test_waits_inside_a_ci_outage_count_under_it() -> None:
    from kelpie.report.waits import CI_OUTAGE, outages

    state = outage_state("app-xcode", at(10), at(16)).state
    samples = [
        outage_sample(at(10), signature="build / xcodebuild", repos=["Example/App"], tasks=["N1"]),
        outage_sample(at(11), signature="build / xcodebuild", repos=["example/app"], tasks=["N2"]),
        outage_sample(at(12), signature="other", outage="another", tasks=["X1"]),  # not this one
        outage_sample(at(20), signature="build / xcodebuild", tasks=["LATE"]),  # after it ended
    ]
    [o] = outages([state], samples)
    assert o.repos == {"example/app"} and o.tasks == {"N1", "N2"}
    assert o.signature == "build / xcodebuild"
    of = WaitCauses((ci("R1"), ci("L1", repo="example/lib")), samples, (), (), [o])
    r1 = waiting("R1", "reviewer")  # waits on CI in the outage's repository
    assert of.split(r1, at(8), at(24)) == {CI_OUTAGE: pytest.approx(6), "ci": pytest.approx(10)}
    named = waiting("N1", "developer")  # no condition, but the outage names it
    assert of.split(named, at(12), at(14)) == {CI_OUTAGE: pytest.approx(2)}
    other_repo = waiting("L1", "reviewer")
    assert of.split(other_repo, at(10), at(16)) == {"ci": pytest.approx(6)}


def test_the_report_headlines_a_ci_outage() -> None:
    state = outage_state("app", at(2), at(20))
    samples = (outage_sample(at(3), signature="build / xcodebuild", repos=["example/app"]),)
    report = build_report(inputs(states=(state,), actions=samples), sections={})
    wait = report.biggest_wait
    assert wait is not None and wait.cause == "ci_outage" and wait.role == "reviewer"
    assert wait.hours == pytest.approx(3 * 12)  # R1-R3 from 08:00 to 20:00 (R4's CI is green)
    markdown = render_markdown(report)
    assert "waiting for a CI outage" in markdown and "fix the CI outage first" in markdown
