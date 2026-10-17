"""Cards stuck before work that block nothing: listed in the report with their age, never
counted in the cost of chaos (kelpie.report.daily._stuck_idle)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Severity
from kelpie.core.model import Dependency, Snapshot, Status, Task
from kelpie.core.states import OpenState
from kelpie.report.daily import ReportInputs, build_report, render_markdown, render_summary
from kelpie.report.store import StateRow
from kelpie.rules.orphan_status import OrphanStatus, build

PREWORK_AFTER = timedelta(hours=4)  # rule 3's default

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
    young = card("C-7", END - PREWORK_AFTER + timedelta(minutes=1))
    old, older = card("C-8", at(2)), card("C-9", at(1), status="triage")
    finished = Task("C-0", "c", Status.DONE, at(1), prework=True)
    tasks = (young, old, older, finished)
    assert [c.task_id for c in report(tasks=tasks).stuck_idle] == ["C-9", "C-8"]
    off = build({"prework_after": False}).stuck_before_work
    assert report(tasks=tasks, stuck_check=off).stuck_idle == ()
    sooner = build({"prework_after": "1m"}).stuck_before_work
    assert [c.task_id for c in report(tasks=tasks, stuck_check=sooner).stuck_idle] == [
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
    assert OrphanStatus().prework_after == PREWORK_AFTER


def test_held_and_ignored_cards_are_parked_on_purpose() -> None:
    held = Task("C-1", "c", Status.BACKLOG, at(1), labels=("freeze",), prework=True)
    ignored = card("PARKED-1", at(1))
    plain = card("C-2", at(1))
    check = build({"hold_labels": ["freeze"], "ignore_tasks": ["PARKED-*"]}).stuck_before_work
    r = report(tasks=(held, ignored, plain), stuck_check=check)
    assert [c.task_id for c in r.stuck_idle] == ["C-2"]


def test_dependents_are_walked_as_rule_3_walks_them() -> None:
    # C-1 <- T-1 (done) <- U-1 (open): rule 3 stops at the done task, so nothing waits on C-1
    through_done = (card("C-1", at(1)), Task("T-1", "t", Status.DONE, at(2)),
                    Task("U-1", "u", Status.READY, at(2)))  # fmt: skip
    deps = (Dependency("T-1", "C-1"), Dependency("U-1", "T-1"))
    assert [c.task_id for c in report(tasks=through_done, dependencies=deps).stuck_idle] == ["C-1"]
    # C-2 <- T-2 (open) <- U-2 (open), with a cycle back to T-2: work waits, not listed
    through_open = (card("C-2", at(1)), Task("T-2", "t", Status.READY, at(2)),
                    Task("U-2", "u", Status.READY, at(2)))  # fmt: skip
    cyc = (Dependency("T-2", "C-2"), Dependency("U-2", "T-2"), Dependency("T-2", "U-2"))
    assert report(tasks=through_open, dependencies=cyc).stuck_idle == ()


def test_rule_3_and_the_list_split_the_stuck_cards() -> None:
    """Whatever the graph, a stuck card is either alerted by rule 3 or listed, never both."""
    import random

    rule = OrphanStatus()
    rng = random.Random(7)
    for _ in range(50):
        tasks = [card(f"C-{i}", at(rng.choice([1, 10, 22]))) for i in range(4)]
        tasks += [Task(f"T-{i}", "t", rng.choice(list(Status)), at(1)) for i in range(4)]
        ids = [t.id for t in tasks]
        deps = tuple({Dependency(*rng.sample(ids, 2)) for _ in range(5)})
        snapshot = Snapshot(now=END, tasks=tuple(tasks), dependencies=deps)
        actions = rule.evaluate(snapshot)
        alerts = {a.task_id for a in actions if "stuck-before-work" in (a.state or "")}
        listed = {c.task_id for c in report(tasks=tuple(tasks), dependencies=deps).stuck_idle}
        lone = {t.id for t in tasks if rule.stuck_before_work(t, snapshot) == []}
        blocking = {t.id for t in tasks if rule.stuck_before_work(t, snapshot)}
        assert listed == lone and alerts <= blocking and not alerts & listed


def test_a_card_whose_alert_closed_before_the_day_is_listed_again() -> None:
    old = card("C-1", at(0, day=1))
    yesterday = stuck_state("C-1", at(4, day=1), at(20, day=1))  # its dependent finished
    assert [c.task_id for c in report(tasks=(old,), states=(yesterday,)).stuck_idle] == ["C-1"]
    later = card("C-2", at(1, day=3))  # moved into pre-work after the day
    assert report(tasks=(later,)).stuck_idle == ()


def test_links_and_the_report_time_zone() -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 3, 2, tzinfo=tz)
    lone = card("C-1", start + timedelta(hours=3))
    r = build_report(ReportInputs(day=DAY, tz=tz, tasks=(lone,)), sections={})
    markdown = render_markdown(r, "https://board.example/t/{id}")
    line = "- [C-1](https://board.example/t/C-1) (scheduled) for 21.0 h, since 2026-03-02 03:00 CET"
    assert line in markdown
