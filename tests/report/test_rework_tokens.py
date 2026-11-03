"""Tokens per return (#197 item 7): the model calls on a returned task from its first
return of the day on, per task and by the role that made them."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Owner, Return, Session, SessionUsage, Span, SpanKind, Status, Task
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.rework import rework_tokens

DAY = date(2026, 3, 2)
PRICES = PriceTable((("model-large", Price(input=3.0, output=15.0, cached_input=0.3)),))
OWNERS = (Owner("dev-1", "developer"), Owner("rev-1", "reviewer"))


def at(hour: float) -> datetime:
    return datetime(2026, 3, 2, tzinfo=UTC) + timedelta(hours=hour)


def call(i: int, hour: float, session: str, task: str | None = None) -> Span:
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


def farm(returns: tuple[Return, ...]) -> ReportInputs:
    """T-1, a developer's task, sent back by review at 12:00 (and maybe again); the
    developer's sessions name the platform's session only, its usage record the task."""
    task = Task("T-1", "fix", Status.IN_PROGRESS, at(9), owner_id="dev-1", returns=returns)
    other = Task("T-2", "other", Status.IN_PROGRESS, at(9), owner_id="dev-1")
    sessions = (
        Session("s-rev", "T-1", "rev-1", at(11), at(12), final_status=None),
        Session("s-rev-2", "T-1", "rev-1", at(16), at(17)),
    )
    record = SessionUsage(
        session_id="p-dev",
        platform_session_id="p-dev",
        started_at=at(9),
        last_at=at(15),
        task_id="T-1",
        role="developer",
    )
    spans = (
        call(1, 10.0, "p-dev"),  # the first attempt: not rework
        call(2, 11.5, "s-rev"),  # the review that sent it back: not rework
        call(3, 13.0, "p-dev"),  # the redo: developer
        call(4, 14.0, "p-dev"),  # developer
        call(5, 16.5, "s-rev-2"),  # the next look: reviewer
        call(6, 13.0, "p-other", "T-2"),  # another task: not rework
    )
    return ReportInputs(
        day=DAY,
        tz=UTC,
        tasks=(task, other),
        owners=OWNERS,
        sessions=sessions,
        spans=spans,
        usage=(record,),
        prices=PRICES,
    )


def test_rework_is_the_calls_after_the_first_return_by_who_made_them() -> None:
    report = build_report(farm((Return(at(12), "review: tests missing"),)))
    [section] = [s for s in report.sections if s.title == "Farm throughput"]
    text = "\n".join(section.lines)
    assert (
        "**Tokens on rework** (1 task(s) returned, 1 return(s)): 33,000 tokens, $0.14, "
        "33,000 per return: developer 22,000 ($0.09); reviewer 11,000 ($0.04)."
    ) in text
    assert "- `T-1`, 1 return(s): 33,000 tokens ($0.14), 33,000 per return: developer 22,000; " \
        "reviewer 11,000" in text  # fmt: skip
    assert "Tokens on rework" in render_markdown(report)


def test_two_returns_count_from_the_first_and_divide_by_both() -> None:
    from kelpie.report.attribution import call_owners

    inputs = farm((Return(at(15), "again"), Return(at(12), "review")))
    roles = {"s-rev": "reviewer", "s-rev-2": "reviewer"}  # the session owners' roles
    owners = call_owners(inputs.tasks, inputs.owners, inputs.sessions, roles, None, inputs.usage)
    counted = {(s.trace_id, s.span_id): s.model for s in inputs.spans}
    [t] = rework_tokens(inputs.tasks, inputs.spans, counted, owners, at(0), at(24))
    assert (t.task_id, t.returns, t.since) == ("T-1", 2, at(12))
    assert {r: sum(u.calls for u in rows) for r, rows in t.per_role.items()} == {
        "developer": 2,
        "reviewer": 1,
    }


def test_a_return_before_the_day_is_not_counted() -> None:
    report = build_report(farm((Return(at(-2), "yesterday"),)))
    assert "Tokens on rework" not in render_markdown(report)


def test_no_return_no_rework_lines() -> None:
    report = build_report(farm(()))
    assert "Tokens on rework" not in render_markdown(report)


def test_a_returned_task_with_no_call_since_is_listed_with_none() -> None:
    inputs = farm((Return(at(20), "late"),))
    report = build_report(inputs)
    text = render_markdown(report)
    assert "**Tokens on rework** (1 task(s) returned, 1 return(s)): 0 tokens, $0.00, 0 per " \
        "return." in text  # fmt: skip
    assert "- `T-1`, 1 return(s): 0 tokens ($0.00), 0 per return: no model calls" in text


def test_a_call_that_ends_at_the_return_counts() -> None:
    """A call counts by when it ended (as the cost of chaos burns them)."""
    from dataclasses import replace

    inputs = farm((Return(at(12) + timedelta(seconds=5), "at once"),))
    report = build_report(replace(inputs, spans=(call(9, 12.0, "p-dev"),)))
    assert "1 return(s)): 11,000 tokens" in render_markdown(report)
