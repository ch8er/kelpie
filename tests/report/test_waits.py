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
ENTRIES = (ledger("needs_you", escalate_to="person-a", waiting=["D3"], items=[]),)


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


def test_hold_and_rule_13_ledger_entries_name_tasks() -> None:
    entries = (
        ledger("bottlenecks", hold="freeze", held_tasks=["H1", 3]),
        ledger(
            "needs_you",
            items=[{"kind": "approval", "task": "A1", "behind": ["B1"]}, "junk"],
        ),
        ledger("bottlenecks", held_tasks=["X1"]),  # not a hold alert
    )
    of = WaitCauses((), entries)
    plain = {tid: of(Task(tid, tid, Status.WAITING, at(8))) for tid in ("H1", "A1", "B1", "X1")}
    assert plain == {"H1": "hold", "A1": "people", "B1": "people", "X1": UNKNOWN}


def test_waits_by_cause_per_role_and_the_biggest() -> None:
    roles = {
        r.role: r for r in throughput(TASKS, REVIEWED, OWNERS, (), START, END, wait_cause=causes())
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
    assert wait.work_hours == pytest.approx(8.0)  # four two-hour reviews
    assert wait.repo == "example/app"  # two of three named repositories
    assert wait.headline().startswith(
        "Biggest wait: reviewer tasks spent 64.0 task-hours waiting for CI (4 task(s), "
        "mostly on `example/app`), against 8.0 h of work. Suggestion: add CI capacity"
    )


def test_no_wait_no_headline() -> None:
    roles = throughput(TASKS, REVIEWED, OWNERS, (), START, END)  # no causes asked
    assert all(not r.waits for r in roles)
    assert biggest_wait(roles, TASKS, causes()) is None
    short = Task("S", "S", Status.WAITING, END - timedelta(hours=MIN_HOURS / 2), role="developer")
    roles = throughput((short,), (), OWNERS, (), START, END, wait_cause=causes())
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
