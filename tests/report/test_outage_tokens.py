"""Outage tokens (#197 item 6): what an outage's tasks spent while it held them is the
outage's, per role, under Outages and in its cost-of-chaos line, not the roles' own spend."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from kelpie.core.budgets import Price, PriceTable
from kelpie.core.ledger import OBSERVE_ONLY
from kelpie.core.model import SessionUsage, Span, SpanKind
from kelpie.report.attribution import call_owners
from kelpie.report.daily import OUTAGE, ReportInputs, build_report, render_markdown
from tests.report.test_outage_kinds import storm_day

PRICES = PriceTable((("model-large", Price(input=3.0, output=15.0, cached_input=0.3)),))
STORMED = "t_5a0c2e70"  # one of the storm's seven tasks (agent-ios-1, developer)
OTHER = "t_7b1d0001"  # waits on the same person, not in the storm (agent-web-1)
# Hermes spans name the platform's session only; its state.db names the task (#271)
PLATFORM = "hermes-session-ios-1-42"


def at(hour: float) -> datetime:
    return datetime.fromisoformat("2026-03-02T00:00:00+00:00") + timedelta(hours=hour)


def call(i: int, hour: float, session: str | None, task: str | None = None) -> Span:
    return Span(
        trace_id="tr",
        span_id=f"c{i}",
        name="chat",
        kind=SpanKind.LLM,
        start=at(hour),
        end=at(hour) + timedelta(seconds=5),
        task_id=task,
        session_id=session,
        model="model-large",
        input_tokens=10_000,
        output_tokens=1_000,
    )


def with_calls(inputs: ReportInputs) -> ReportInputs:
    spans = (
        call(1, 10.5, PLATFORM),  # before the storm's first sighting (11:00): the role's
        call(2, 12.0, PLATFORM),  # while the storm held the task: the outage's
        call(3, 14.0, PLATFORM),  # the outage's
        call(4, 12.0, None, OTHER),  # not in the storm: the role's
        call(5, 18.0, None, STORMED),  # after the storm's last sighting (17:00): the role's
    )
    record = SessionUsage(
        session_id=PLATFORM,
        platform_session_id=PLATFORM,
        started_at=at(10),
        last_at=at(18),
        task_id=STORMED,
        role="developer",
    )
    return replace(inputs, spans=spans, usage=(record,), prices=PRICES)


def test_an_outages_tasks_spend_while_it_held_them_is_the_outages() -> None:
    report = build_report(with_calls(storm_day()))
    [line] = report.outages
    [(role, spend)] = line.spend
    assert role == "developer"
    assert spend.tokens == 2 * 11_000  # calls 2 and 3
    assert spend.usd == pytest.approx(2 * (10_000 * 3 + 1_000 * 15) / 1e6)
    [chaos] = [c for c in report.chaos if c.rule == OUTAGE]
    assert chaos.spend.tokens == 2 * 11_000  # its line in the cost of chaos burns them
    markdown = render_markdown(report)
    assert (
        "Its tasks spent 22,000 tokens ($0.09) while it held them: developer 22,000 ($0.09)"
    ) in markdown
    # the throughput section moves it out of the role's own spend, and says so
    assert (
        "Spend of tasks while an outage held them (under Outages, not in the cost per "
        "completed task): developer 22,000 tokens, $0.09."
    ) in markdown


def test_spend_outside_the_outage_or_its_tasks_stays_the_roles() -> None:
    report = build_report(with_calls(storm_day()))
    assert report.spend.tokens == 5 * 11_000  # the day's spend is unchanged
    [line] = report.outages
    assert sum(s.tokens for _, s in line.spend) == 2 * 11_000


def test_a_storm_in_observe_holds_no_spend() -> None:
    inputs = with_calls(storm_day())
    trial = tuple(
        replace(e, params={**e.params, OBSERVE_ONLY: True}) if e.rule == "stale_blocks" else e
        for e in inputs.actions
    )
    report = build_report(replace(inputs, actions=trial))
    [line] = report.outages
    assert line.spend == ()
    assert all(c.rule != OUTAGE for c in report.chaos)
    markdown = render_markdown(report)
    assert "while it held them" not in markdown
    assert "while an outage held them" not in markdown


def test_without_the_usage_record_a_session_only_call_names_no_task() -> None:
    """A span with only the platform's session id and no record of its task is no
    outage's: the report cannot tell which task it worked for."""
    report = build_report(replace(with_calls(storm_day()), usage=()), sections={})
    [line] = report.outages
    assert line.spend == ()


def test_call_owners_order_of_sources() -> None:
    from kelpie.core.model import Owner, Session, Status, Task
    from kelpie.core.spans import Attribution

    task = Task("T-1", "t", Status.IN_PROGRESS, at(1), owner_id="dev-1")
    owners = (Owner("dev-1", "developer"), Owner("rev-1", "reviewer"))
    record = SessionUsage(
        session_id="s-9",
        platform_session_id="p-9",
        started_at=at(1),
        last_at=at(2),
        task_id="T-9",
        role="pm",
    )
    found = call_owners(
        (task,),
        owners,
        (Session("s-1", "T-1", "rev-1", at(1)),),
        {"s-1": "reviewer"},
        Attribution(session_tasks={"s-2": "T-1", "p-9": "T-1"}),
        (record,),
    )
    assert found.task_of(None, "s-1") == "T-1"  # the board's session
    assert found.task_of(None, "s-2") == "T-1"  # remembered
    assert found.task_of(None, "p-9") == "T-1"  # remembered beats the usage record
    assert found.task_of(None, "s-9") == "T-9"  # the usage record
    assert found.task_of("T-5", "s-1") == "T-5"  # the span's own task first
    # a role: the task the span names, else the session owner's, never the session's task
    assert found.role_of("T-1", "s-1") == "developer"
    assert found.role_of(None, "s-1") == "reviewer"
    assert found.role_of(None, "s-9") == "pm"  # the usage record's
    assert found.role_of(None, "nobody") == "unknown"


def test_a_call_a_loop_also_covers_is_burned_once_under_the_outage() -> None:
    """A runaway-loop state on the same session over 12:00-13:30: call 2 is in both, and
    the outage, first, burns it; the loop keeps none of it twice."""
    from kelpie.report.daily import day_bounds
    from tests.report.test_outage_kinds import row

    inputs = with_calls(storm_day())
    loop = row(f"loop_breaker:identical_calls:{PLATFORM}", "loop_breaker", at(12), at(11.9),
               at(13.5))  # fmt: skip
    report = build_report(replace(inputs, states=(*inputs.states, loop)), sections={})
    lines = {c.rule: c for c in report.chaos}
    assert lines[OUTAGE].spend.tokens == 2 * 11_000
    assert "loop_breaker" not in lines or lines["loop_breaker"].spend.tokens == 0
    burned = sum(c.spend.tokens for c in report.chaos)
    assert burned == 2 * 11_000
    assert day_bounds(inputs.day, inputs.tz)[0] == at(0)


def test_without_an_outage_a_reviewers_call_stays_the_reviewers() -> None:
    """No outage: a call on the reviewer's session for a developer's task is priced to the
    reviewer, as before the session's task was looked up (single-farm output unchanged)."""
    from kelpie.core.model import Owner, Session, Status, Task

    task = Task("T-1", "t", Status.DONE, at(15), owner_id="dev-1")
    inputs = ReportInputs(
        day=at(0).date(),
        tz=at(0).tzinfo,  # type: ignore[arg-type]
        tasks=(task,),
        owners=(Owner("dev-1", "developer"), Owner("rev-1", "reviewer")),
        sessions=(Session("s-rev", "T-1", "rev-1", at(12), at(13)),),
        spans=(call(1, 12.5, "s-rev"),),
        prices=PRICES,
    )
    markdown = render_markdown(build_report(inputs))
    assert "| developer |" in markdown
    assert "*no spend*" in markdown  # the developer's completed task: no call of its own
    assert "| reviewer | 1 |" in markdown  # listed: its spend is in no developer cell
