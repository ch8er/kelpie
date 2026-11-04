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


def roled(i: int, hour: float, session: str) -> Span:
    return replace(call(i, hour), span_id=f"r{i}", session_id=session)


ROLES = {"D-1": "developer", "R-1": "reviewer"}


def role_evidence(spans: list[Span], held: list[Span]) -> Evidence:
    return Evidence(
        end=at(24),
        spans=spans,
        counted={span_key(s): s.model for s in spans},
        roles=ROLES,
        outage_held=frozenset(span_key(s) for s in held),
    )


def test_a_role_fix_counts_only_its_roles_held_tokens() -> None:
    dev = [roled(0, -15, "D-1"), roled(1, 12, "D-1"), roled(2, 14, "D-1")]
    rev = [roled(3, -15, "R-1"), roled(4, 12, "R-1")]
    ev = role_evidence([*dev, *rev], [dev[1], rev[1]])  # one held call per role
    v = verify(FixEvent(at(8), MANUAL, "prompt trimmed", role="developer"), ev)
    [m] = [m for m in v.metrics if m.name == "tokens per day (developer)"]
    assert (m.before, m.after) == pytest.approx((CALL, CALL / (16 / 24)))
    assert v.outage_tokens == (0, CALL)  # the reviewer's held call is not this fix's


def test_a_group_says_what_each_fixs_own_row_left_out() -> None:
    from kelpie.report.verify import verify_group

    dev = [roled(0, -15, "D-1"), roled(1, 12, "D-1"), roled(2, 14, "D-1")]
    rev = [roled(3, -15, "R-1"), roled(4, 12, "R-1"), roled(5, 15, "R-1")]
    ev = role_evidence([*dev, *rev], [dev[1], rev[1]])
    fixes = (
        FixEvent(at(8), MANUAL, "developer prompt trimmed", role="developer"),
        FixEvent(at(9), MANUAL, "reviewer reads the diff only", role="reviewer"),
    )
    v = verify_group(fixes, ev)
    assert v.together and v.outage_tokens == (0, 2 * CALL)  # the group: every role
    own = {c.event.role: c.outage_tokens for c in v.own if c.metric.name.startswith("tokens")}
    assert own == {"developer": (0, CALL), "reviewer": (0, CALL)}
    report = replace(build_report(storm_day(), sections={}), verifications=(v,))
    markdown = render_markdown(report)
    assert "Left out as outage tokens: 0 before, 22,000 after" in markdown
    assert "Left out of these rows as outage tokens:" in markdown
    assert "manual, tokens per day (developer): 0 before, 11,000 after." in markdown
