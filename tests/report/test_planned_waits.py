"""Planned waits are not waiting (incident 20): agent tasks behind work planned for later
stay out of the cost of chaos, its dollars and the headline, and are listed apart."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.states import OpenState
from kelpie.report.daily import ReportInputs, build_report, render_markdown, render_summary
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


def report(**kw: Any) -> Any:
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
    assert "Cost of chaos: 8.0 hours lost" in markdown
    assert "Planned" not in render_summary(r) and "144" not in render_summary(r)


def test_older_alerts_without_planned_params_show_no_line() -> None:
    r = report(states=(opened(at(9), at(11)),), actions=(inbox(at(9), ["A-1"]),))
    assert r.planned is None and "Planned waits" not in render_markdown(r)


def test_only_planned_work_behind_an_inbox_costs_nothing() -> None:
    r = report(states=(opened(at(9), at(13)),), actions=(inbox(at(9), [], ["P-1", "P-2"]),))
    assert r.chaos == () and r.chaos_usd == 0
    assert r.planned is not None and r.planned.agent_hours == pytest.approx(8.0)
