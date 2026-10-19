"""Planned waits are not waiting (incident 20): agent tasks behind work planned for later
stay out of the cost of chaos, its dollars and the headline, and are listed apart."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.states import OpenState
from kelpie.report.daily import (
    DailyReport,
    ReportInputs,
    build_report,
    render_markdown,
    render_summary,
)
from kelpie.report.store import StateRow

DAY = date(2026, 3, 2)


def at(hour: int) -> datetime:
    return datetime(2026, 3, 2, hour, tzinfo=UTC)


def inbox(when: datetime, waiting: list[str], planned: list[str] | None = None) -> LedgerEntry:
    params: dict[str, Any] = {
        "escalate_to": "person-a",
        "cost_hours": 1.0,
        "waiting_agent_tasks": len(waiting),
        "waiting": waiting,
    }
    if planned is not None:  # rule 13 after track B's planned waits
        params |= {"planned_agent_tasks": len(planned), "planned": planned}
    return LedgerEntry(
        at=when, fingerprint="f", subject="s", rule="needs_you", kind=ActionKind.ESCALATE,
        mode=Mode.ACT, outcome=Outcome.ALERTED, reason="r", params=params,
    )  # fmt: skip


def opened(since: datetime, last: datetime) -> StateRow:
    state = OpenState(
        "needs_you:person-a", "needs_you", since, since, last, 0, Severity.WARNING, "s"
    )
    return StateRow(state)


def report(**kw: Any) -> DailyReport:
    return build_report(ReportInputs(day=DAY, tz=UTC, hour_usd=20.0, **kw), sections={})


def test_planned_waits_are_listed_never_counted() -> None:
    # 09-17: one agent task waits on the person, 18 more sit behind a planned release task
    behind = [f"P-{i:02}" for i in range(18)]
    r = report(
        states=(opened(at(9), at(17)),),
        actions=(inbox(at(9), ["A-1"], behind), inbox(at(13), ["A-1"], behind)),
    )
    [people] = r.chaos
    assert people.rule == "needs_you" and people.hours == pytest.approx(8.0)  # A-1 only
    assert r.chaos_hours == pytest.approx(8.0) and r.chaos_usd == pytest.approx(160.0)
    assert r.planned is not None and r.planned.agent_hours == pytest.approx(18 * 8)
    assert r.planned.tasks == tuple(behind)
    markdown = render_markdown(r)
    assert "**Planned waits**: 144.0 agent-hours" in markdown and "not counted" in markdown
    assert "`P-00`" in markdown
    assert "Cost of chaos: 8.0 hours lost" in markdown
    assert "Planned" not in render_summary(r) and "144" not in render_summary(r)


def test_older_alerts_without_planned_params_show_no_line() -> None:
    r = report(states=(opened(at(9), at(11)),), actions=(inbox(at(9), ["A-1"]),))
    assert r.planned is None and "Planned waits" not in render_markdown(r)


def test_only_planned_work_behind_an_inbox_costs_nothing() -> None:
    r = report(states=(opened(at(9), at(13)),), actions=(inbox(at(9), [], ["P-1", "P-2"]),))
    assert r.chaos == () and r.chaos_usd == 0
    assert r.planned is not None and r.planned.agent_hours == pytest.approx(8.0)


def test_inboxes_add_up_and_notices_or_old_episodes_do_not() -> None:
    other = StateRow(
        OpenState("needs_you:unassigned", "needs_you", at(10), at(10), at(12), 0,
                  Severity.WARNING, "s")
    )  # fmt: skip
    unassigned = inbox(at(10), [], ["Q-1"])
    unassigned = replace(unassigned, params={**unassigned.params, "escalate_to": None})
    notice = LedgerEntry(
        at=at(11), fingerprint="g", subject="s", rule="needs_you", kind=ActionKind.ALERT,
        mode=Mode.ACT, outcome=Outcome.ALERTED, reason="resolved",
        params={"state": "needs_you:person-b"},
    )  # fmt: skip
    earlier = inbox(at(8), [], ["OLD-1", "OLD-2", "OLD-3"])  # before this episode opened
    r = report(
        states=(opened(at(9), at(11)), other),
        actions=(earlier, inbox(at(9), ["A-1"], ["P-1"]), unassigned, notice),
    )
    assert r.planned is not None
    assert r.planned.agent_hours == pytest.approx(1 * 2 + 1 * 2)  # person-a 09-11, unassigned
    assert r.planned.tasks == ("P-1", "Q-1")
    assert r.chaos_hours == pytest.approx(2.0)  # A-1 only
    assert r.biggest_wait is None  # planned tasks never wait


def test_a_count_without_ids_and_task_links() -> None:
    entry = inbox(at(9), [])
    entry = replace(entry, params={**entry.params, "planned_agent_tasks": 2})
    r = report(states=(opened(at(9), at(10)),), actions=(entry,))
    markdown = render_markdown(r)
    assert r.planned is not None and r.planned.tasks == ()
    assert "ids per alert)\n" in markdown + "\n" and "alert):" not in markdown
    linked = report(states=(opened(at(9), at(10)),), actions=(inbox(at(9), [], ["P-1"]),))
    assert "[P-1](https://board.example/t/P-1)" in render_markdown(
        linked, "https://board.example/t/{id}"
    )


def test_an_inbox_outside_the_day_counts_nothing() -> None:
    gone = StateRow(
        OpenState("needs_you:person-a", "needs_you", at(1) - timedelta(days=2),
                  at(1) - timedelta(days=2), at(1) - timedelta(days=1), 0, Severity.WARNING, "s")
    )  # fmt: skip
    assert report(states=(gone,), actions=(inbox(at(9), [], ["P-1"]),)).planned is None


def test_verify_and_cost_of_chaos_never_read_planned() -> None:
    from kelpie.report.daily import state_hours

    state = opened(at(9), at(17)).state
    entries = [inbox(at(9), ["A-1"], [f"P-{i}" for i in range(10)])]
    assert state_hours(entries, state, at(9), at(17)) == pytest.approx(8.0)


def test_rule_13_on_incident_20_feeds_the_report() -> None:
    """The contract end to end: rule 13's escalation on the planned-wait fixture, as the
    ledger stores it, gives a planned line and costs nothing for the planned tasks."""
    from pathlib import Path

    from kelpie.rules import REGISTRY
    from tests.incidents import load

    fixture = Path(__file__).parents[1] / "fixtures" / "incidents" / "20-planned-wait.yaml"
    incident = load(fixture)
    now = incident.snapshot.now
    actions = [a for a in REGISTRY["needs_you"](incident.config).evaluate(incident.snapshot)]
    rows, entries = [], []
    for a in actions:
        assert a.state is not None
        since = a.since or now
        rows.append(StateRow(OpenState(a.state, "needs_you", since, since, now, 0,
                                       Severity.WARNING, "s")))  # fmt: skip
        entries.append(LedgerEntry(at=since, fingerprint=a.key or "k", subject="s",
                                   rule="needs_you", kind=a.kind, mode=Mode.ACT,
                                   outcome=Outcome.ALERTED, reason=a.reason,
                                   params=dict(a.params)))  # fmt: skip
    day = now.astimezone(UTC).date()
    r = build_report(ReportInputs(day=day, tz=UTC, hour_usd=20.0, states=tuple(rows),
                                  actions=tuple(entries)), sections={})  # fmt: skip
    planned_ids = {t for a in actions for t in a.params.get("planned", [])}
    assert planned_ids  # the fixture puts planned work behind a person's inbox
    assert r.planned is not None and set(r.planned.tasks) == planned_ids
    counted = {t for c in r.chaos for t in c.tasks}
    assert not counted & planned_ids  # never in the cost of chaos
