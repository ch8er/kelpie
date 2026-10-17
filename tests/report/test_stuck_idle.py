"""Cards stuck before work that block nothing: listed in the report with their age, never
counted in the cost of chaos (kelpie.report.daily._stuck_idle)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Severity
from kelpie.core.model import Dependency, Status, Task
from kelpie.core.states import OpenState
from kelpie.report.daily import (
    DEFAULT_PREWORK_AFTER,
    ReportInputs,
    build_report,
    render_markdown,
    render_summary,
)
from kelpie.report.store import StateRow

DAY = date(2026, 3, 2)
END = datetime(2026, 3, 3, tzinfo=UTC)


def at(hour: float, day: int = 2) -> datetime:
    return datetime(2026, 3, day, tzinfo=UTC) + timedelta(hours=hour)


def card(tid: str, since: datetime, status: str = "scheduled") -> Task:
    return Task(tid, tid, Status.BACKLOG, since, prework=True, platform_status=status)


def stuck_state(tid: str, since: datetime, last: datetime) -> StateRow:
    sid = f"orphan_status:stuck-before-work:{tid}:{since.isoformat()}"
    state = OpenState(sid, "orphan_status", since, since, last, 0, Severity.WARNING, "s", tid)
    return StateRow(state)


def report(**kw: Any) -> Any:
    return build_report(ReportInputs(day=DAY, tz=UTC, **kw), sections={})


def test_a_lone_stuck_card_is_listed_and_costs_nothing() -> None:
    lone = card("C-1", at(3))  # 21 h in scheduled by midnight
    r = report(tasks=(lone,), hour_usd=50.0)
    [c] = r.stuck_idle
    assert (c.task_id, c.status, c.age_hours) == ("C-1", "scheduled", pytest.approx(21.0))
    assert r.chaos == () and r.chaos_hours == 0 and r.chaos_usd == 0
    markdown = render_markdown(r)
    assert "**Stuck, blocking nothing**" in markdown
    assert "- `C-1` (scheduled) for 21.0 h, since 2026-03-02 03:00 UTC" in markdown
    assert "Stuck, blocking nothing" not in render_summary(r)  # the headline stays as it was


def test_alerted_and_blocking_cards_are_not_listed() -> None:
    alerted = card("C-2", at(1))  # rule 3 reported it (work waited on it): a chaos line
    behind = Task("T-9", "t", Status.READY, at(0))
    blocking = card("C-3", at(1))  # an open task depends on it, though no alert was kept
    queued = card("C-4", at(1))  # it waits for its own unfinished parent
    parent = Task("P-1", "p", Status.IN_PROGRESS, at(0))
    freed = card("C-5", at(1))  # what depended on it is done: it blocks nothing now
    done = Task("T-8", "t", Status.DONE, at(2))
    deps = (
        Dependency("T-9", "C-3"),
        Dependency("C-4", "P-1"),
        Dependency("T-8", "C-5"),
    )
    r = report(
        tasks=(alerted, behind, blocking, queued, parent, freed, done),
        dependencies=deps,
        states=(stuck_state("C-2", at(5), at(20)),),
    )
    assert [c.task_id for c in r.stuck_idle] == ["C-5"]
    [line] = r.chaos
    assert line.rule == "orphan_status:prework" and line.tasks == ("C-2",)


def test_a_card_reported_earlier_in_the_day_is_not_counted_twice() -> None:
    earlier = card("C-6", at(0, day=1))
    r = report(tasks=(earlier,), states=(stuck_state("C-6", at(4, day=1), at(9)),))
    assert r.stuck_idle == ()
    assert [c.tasks for c in r.chaos] == [("C-6",)]


def test_threshold_order_and_switch() -> None:
    young = card("C-7", END - DEFAULT_PREWORK_AFTER + timedelta(minutes=1))
    old, older = card("C-8", at(2)), card("C-9", at(1), status="triage")
    finished = Task("C-0", "c", Status.DONE, at(1), prework=True)
    tasks = (young, old, older, finished)
    assert [c.task_id for c in report(tasks=tasks).stuck_idle] == ["C-9", "C-8"]
    assert report(tasks=tasks, prework_after=None).stuck_idle == ()
    assert [c.task_id for c in report(tasks=tasks, prework_after=timedelta(0)).stuck_idle] == [
        "C-9",
        "C-8",
        "C-7",
    ]


def test_the_list_is_capped_and_escaped() -> None:
    many = tuple(card(f"C-{i:02}", at(1), status="sch|ed*") for i in range(12))
    markdown = render_markdown(report(tasks=many))
    assert markdown.count("(sch\\|ed\\*) for 23.0 h") == 10
    assert "- and 2 more" in markdown


def test_the_default_threshold_is_rule_3_s() -> None:
    from kelpie.rules.orphan_status import OrphanStatus

    assert OrphanStatus().prework_after == DEFAULT_PREWORK_AFTER


def test_prework_after_comes_from_rule_3_config() -> None:
    from kelpie.report.cli import _prework_after

    assert _prework_after({}) == DEFAULT_PREWORK_AFTER
    assert _prework_after({"prework_after": False}) is None
    assert _prework_after({"prework_after": "2h"}) == timedelta(hours=2)
    assert _prework_after({"prework_after": "soon"}) == DEFAULT_PREWORK_AFTER
