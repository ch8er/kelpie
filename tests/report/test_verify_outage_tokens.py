"""Before/after leaves out the tokens an outage held (#332, after #197 item 6): the calls an
outage's tasks made while it held them are the outage's spend, so an outage in the before
or after window reads as neither a fix's effect nor a regression; the section says how many
tokens it left out."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from kelpie.core.model import SessionUsage, Span, SpanKind, Status, Task
from kelpie.core.spans import span_key
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.fixes import MANUAL, FixEvent
from kelpie.report.verify import INSTRUCTIONS_RULE, Evidence, verify
from tests.report.test_outage_kinds import storm_day

STORMED = "t_5a0c2e70"  # one of the storm's seven tasks; the storm holds it 11:00-17:00
PLATFORM = "hermes-session-ios-1-42"  # the spans name the platform's session only (#271)
CALL = 11_000  # tokens per call


def at(hour: float) -> datetime:
    """Hours from the start of the report's day (2026-03-02, UTC)."""
    return datetime.fromisoformat("2026-03-02T00:00:00+00:00") + timedelta(hours=hour)


def call(i: int, hour: float) -> Span:
    return Span(
        trace_id="tr",
        span_id=f"c{i}",
        name="chat",
        kind=SpanKind.LLM,
        start=at(hour),
        end=at(hour) + timedelta(seconds=5),
        session_id=PLATFORM,
        model="model-large",
        input_tokens=10_000,
        output_tokens=1_000,
    )


RECORD = SessionUsage(
    session_id=PLATFORM,
    platform_session_id=PLATFORM,
    started_at=at(-24),
    last_at=at(20),
    task_id=STORMED,
    role="developer",
)


def day(fix_hour: float, *hours: float, outage: bool = True) -> ReportInputs:
    inputs = storm_day()
    if not outage:
        inputs = replace(inputs, states=(), actions=())
    spans = tuple(call(i, h) for i, h in enumerate(hours))
    fix = FixEvent(at(fix_hour), MANUAL, "dispatcher guard relaxed")
    return replace(inputs, spans=spans, usage=(RECORD,), fixes=(fix,))


def tokens_per_day(inputs: ReportInputs) -> tuple[float | None, float | None, tuple[int, int]]:
    [v] = build_report(inputs, sections={}).verifications
    [m] = [m for m in v.metrics if m.name == "tokens per day"]
    return m.before, m.after, v.outage_tokens


def test_an_outage_in_the_after_window_only_is_left_out() -> None:
    # fix at 08:00: before 03-01 08:00 to 08:00, after 08:00 to the day's end (16h)
    inputs = day(8, -15, -10, 12, 14, 18)  # 12:00 and 14:00: the storm held the task
    before, after, held = tokens_per_day(inputs)
    assert before == pytest.approx(2 * CALL)
    assert after == pytest.approx(CALL / (16 / 24))  # 18:00 alone
    assert held == (0, 2 * CALL)
    markdown = render_markdown(build_report(inputs, sections={}))
    assert "Left out as outage tokens: 0 before, 22,000 after" in markdown


def test_an_outage_overlapping_both_windows_is_left_out_on_both_sides() -> None:
    # fix at 13:00, inside the storm (11:00-17:00): 12:00 before it, 14:00 after it
    inputs = day(13, -10, 12, 14, 18)
    before, after, held = tokens_per_day(inputs)
    assert before == pytest.approx(CALL)  # 03-01 14:00 alone
    assert after == pytest.approx(CALL / (11 / 24))  # 18:00 alone, over 13:00-24:00
    assert held == (CALL, CALL)
    markdown = render_markdown(build_report(inputs, sections={}))
    assert "Left out as outage tokens: 11,000 before, 11,000 after" in markdown


def test_without_an_outage_nothing_is_left_out_and_nothing_is_said() -> None:
    inputs = day(8, -15, -10, 12, 14, 18, outage=False)
    before, after, held = tokens_per_day(inputs)
    assert before == pytest.approx(2 * CALL)
    assert after == pytest.approx(3 * CALL / (16 / 24))  # every call counts, as before #332
    assert held == (0, 0)
    assert "outage tokens" not in render_markdown(build_report(inputs, sections={}))


def test_tokens_per_completed_task_leaves_held_calls_out() -> None:
    fix = at(8)
    spans = [call(0, -23), call(1, -12), call(2, 12), call(3, 18)]
    done = [Task(f"T-{i}", "t", Status.DONE, at(h)) for i, h in enumerate((-20, -2, 10, 20))]
    split = FixEvent(fix, MANUAL, "skills split", rule=INSTRUCTIONS_RULE)
    base = Evidence(
        end=at(24), spans=spans, counted={span_key(s): s.model for s in spans}, tasks=done
    )
    held = replace(base, outage_held=frozenset({span_key(spans[2])}))

    def per_task(ev: Evidence) -> tuple[float | None, float | None, tuple[int, int]]:
        v = verify(split, ev)
        [m] = [m for m in v.metrics if m.name == "tokens per completed task"]
        return m.before, m.after, v.outage_tokens

    assert per_task(base) == (CALL, CALL, (0, 0))  # 2 calls / 2 tasks on each side
    assert per_task(held) == (CALL, CALL / 2, (0, CALL))  # the held call is the outage's
