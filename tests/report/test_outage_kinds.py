"""Outages of every kind in the report (#229, incident 20): a block storm's waits count once,
under the outage, ahead of the person its reason names and the stale blocks of its tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.incidents import BLOCK_STORM, is_outage_state
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.states import OpenState
from kelpie.report.daily import OUTAGE, PEOPLE, ReportInputs, build_report, render_markdown
from kelpie.report.store import StateRow
from kelpie.report.waits import SHARED_BLOCKER, WAIT_CAUSES
from kelpie.rules import REGISTRY
from tests.incidents import load

FIXTURE = Path(__file__).parents[1] / "fixtures" / "incidents" / "20-block-storm.yaml"
STORM_TASKS = [f"t_5a0c2e7{i}" for i in range(7)]


def entry(at: datetime, rule: str, params: dict[str, object], kind: ActionKind) -> LedgerEntry:
    return LedgerEntry(at=at, fingerprint=f"{rule}-{at:%H%M}", subject="s", rule=rule,
                       kind=kind, mode=Mode.OBSERVE, outcome=Outcome.OBSERVED, reason="r",
                       params=params)  # fmt: skip


def row(sid: str, rule: str, since: datetime, opened: datetime, last: datetime,
        task: str | None = None) -> StateRow:  # fmt: skip
    return StateRow(OpenState(sid, rule, since, opened, last, 0, Severity.WARNING, "s", task))


def storm_day() -> ReportInputs:
    """The 2026-10-17 shape with the storm check switched on: rule 4's storm (its own alert
    on the fixture), plus what the live farm also had: the seven tasks in the person's inbox
    and a stale block on each."""
    incident = load(FIXTURE)
    now = incident.snapshot.now  # 11:00, the storm's first sighting
    [storm] = [
        a
        for a in REGISTRY["stale_blocks"](incident.config).evaluate(incident.snapshot)
        if a.state is not None and is_outage_state(a.state)
    ]
    assert storm.params["kind"] == BLOCK_STORM and storm.params["tasks"] == STORM_TASKS
    until = now + timedelta(hours=6)  # still blocked at 17:00
    since = datetime.fromisoformat("2026-03-02T10:00:00+00:00")
    states = [
        row(storm.state or "", "stale_blocks", since, now, until),
        row("needs_you:ceo", PEOPLE, since, since, until, "H-1"),
        *(row(f"stale_blocks:{t}:block", "stale_blocks", now, now, until, t) for t in STORM_TASKS),
    ]
    actions = [
        entry(now, "stale_blocks", dict(storm.params), ActionKind.ALERT),
        entry(since, PEOPLE, {"escalate_to": "ceo", "waiting_agent_tasks": 7,
                              "waiting": STORM_TASKS}, ActionKind.ESCALATE),
    ]  # fmt: skip
    return ReportInputs(day=now.astimezone(UTC).date(), tz=UTC, states=tuple(states),
                        actions=tuple(actions), tasks=incident.snapshot.tasks,
                        owners=incident.snapshot.owners, hour_usd=10.0)  # fmt: skip


def test_a_block_storms_hours_count_once_under_the_outage() -> None:
    report = build_report(storm_day(), sections={})
    lines = {c.rule: c for c in report.chaos}
    # 11:00-17:00, seven tasks: the outage's; 10:00-11:00 before it opened: the person's
    assert lines[OUTAGE].hours == pytest.approx(7 * 6)
    assert set(lines[OUTAGE].tasks) == set(STORM_TASKS)
    assert lines[PEOPLE].hours == pytest.approx(7 * 1)
    assert lines[PEOPLE].also_hours == pytest.approx(7 * 6)
    assert lines["stale_blocks"].hours == 0
    assert lines["stale_blocks"].also_hours == pytest.approx(7 * 6)
    assert report.chaos_hours == pytest.approx(49)  # once; the live report summed all three
    markdown = render_markdown(report)
    assert "Cost of chaos: 49.0 hours lost" in markdown


def test_the_outages_list_names_the_storm_and_who_can_end_it() -> None:
    report = build_report(storm_day(), sections={})
    [line] = report.outages
    assert line.kind == BLOCK_STORM and line.person == "ceo" and line.prs == 7 and line.open
    markdown = render_markdown(report)
    assert "**Outages**" in markdown
    assert (
        "- block storm `needs_input: waiting for CEO (Xcode first launch)` since 2026-03-02 "
        "10:00 UTC, still open: 7 task(s) blocked on it, 0 alert(s) folded under it; only "
        "ceo can end it"
    ) in markdown


def test_a_storm_explains_only_the_tasks_it_names() -> None:
    from kelpie.report.daily import day_bounds, wait_causes

    inputs = storm_day()
    start, end = day_bounds(inputs.day, UTC)
    causes = wait_causes(inputs, start, end)
    by_id = {t.id: t for t in inputs.tasks}
    at = datetime.fromisoformat("2026-03-02T11:00:00+00:00")
    stormed = causes.split(by_id["t_5a0c2e70"], at, at + timedelta(hours=2))
    assert stormed == {SHARED_BLOCKER: pytest.approx(2.0)}
    other = causes.split(by_id["t_7b1d0001"], at, at + timedelta(hours=2))  # same person
    assert SHARED_BLOCKER not in other
    assert "shared blocker" in WAIT_CAUSES[SHARED_BLOCKER]


def test_a_storm_still_in_observe_is_listed_not_counted() -> None:
    from dataclasses import replace

    from kelpie.core.ledger import OBSERVE_ONLY

    inputs = storm_day()
    trial = tuple(
        replace(e, params={**e.params, OBSERVE_ONLY: True}) if e.rule == "stale_blocks" else e
        for e in inputs.actions
    )
    report = build_report(replace(inputs, actions=trial), sections={})
    lines = {c.rule: c for c in report.chaos}
    assert OUTAGE not in lines  # switching the check on is what moves the hours
    assert lines[PEOPLE].hours == pytest.approx(7 * 7)  # 10:00-17:00, the person's
    [line] = report.outages
    assert line.observe_only
    assert "(observe only: its check is not switched on" in render_markdown(report)
