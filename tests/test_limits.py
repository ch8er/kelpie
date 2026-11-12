"""Provider limit hits from traces and the concurrency they suggest (kelpie.core.limits)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.limits import is_limit_hit, provider_limits, provider_of
from kelpie.core.model import Span, SpanKind

T0 = datetime(2026, 3, 2, 12, tzinfo=UTC)
HIT = {"status_code": 429}


def call(
    n: int,
    minute: float,
    session: str | None,
    attrs: dict[str, Any] | None = None,
    *,
    length: float = 2.0,
    model: str = "claude-x",
    kind: SpanKind = SpanKind.LLM,
) -> Span:
    start = T0 + timedelta(minutes=minute)
    return Span(
        trace_id=f"{n:032x}",
        span_id=f"{n:016x}",
        name="llm",
        kind=kind,
        start=start,
        end=start + timedelta(minutes=length),
        session_id=session,
        model=model,
        outcome="error" if attrs else "ok",
        attributes=attrs or {},
    )


def by_model(model: str | None) -> str | None:
    return {"claude-x": "anthropic", "gpt-y": "openai"}.get(model or "")


@pytest.mark.parametrize(
    "attrs",
    [
        {"status_code": 429},
        {"status_code": "429"},
        {"error.type": "rate_limit_error"},
        {"error.type": "RateLimitError"},
        {"error.type": "openai.RateLimitError"},
        {"error.type": "429"},
        {"error.type": "Too Many Requests"},
        {"error.type": "RESOURCE_EXHAUSTED"},
    ],
)
def test_a_call_refused_for_a_limit_is_a_hit(attrs: dict[str, Any]) -> None:
    assert is_limit_hit(call(1, 0, "s", attrs))


@pytest.mark.parametrize(
    "attrs",
    [
        {},
        {"status_code": 529},  # overloaded: the provider's capacity, not the farm's limit
        {"error.type": "overloaded_error"},
        {"status_code": 500},
        {"status_code": True},
        {"error.type": "timeout"},
        {"error.type": 429},  # not text
    ],
)
def test_other_calls_are_not_hits(attrs: dict[str, Any]) -> None:
    assert not is_limit_hit(call(1, 0, "s", attrs))


def test_a_tool_span_is_never_a_hit() -> None:
    assert not is_limit_hit(call(1, 0, "s", HIT, kind=SpanKind.TOOL))


def test_the_provider_from_the_span_else_its_model() -> None:
    assert provider_of(call(1, 0, "s", {"gen_ai.provider.name": "OpenAI"}), by_model) == "openai"
    assert provider_of(call(1, 0, "s", {"gen_ai.system": "anthropic"}), by_model) == "anthropic"
    assert provider_of(call(1, 0, "s", model="gpt-y"), by_model) == "openai"
    assert provider_of(call(1, 0, "s", model="local-z"), by_model) == "unknown"


def test_hits_with_several_sessions_running_suggest_fewer() -> None:
    spans = [
        call(1, 0, "a", length=10),
        call(2, 1, "b", length=10),
        call(3, 2, "c", length=10),
        call(4, 3, "d", HIT, length=1),  # 4 sessions running when it was refused
        call(5, 20, "a"),
        call(6, 21, "b", HIT, length=1),  # a and b: 2 running (a ended within the slack)
        call(7, 0, "x", model="gpt-y"),  # another provider, no hits: not listed
    ]
    [found] = provider_limits(spans, T0, T0 + timedelta(hours=1), by_model)
    assert (found.provider, found.hits, found.calls) == ("anthropic", 2, 6)
    assert found.at_hits == (2, 4) and found.peak == 4
    assert found.suggested == 1
    assert "suggested concurrency 1" in found.line()


def test_a_limit_reached_by_one_session_suggests_nothing() -> None:
    spans = [call(1, 0, "a"), call(2, 30, "a", HIT)]
    [found] = provider_limits(spans, T0, T0 + timedelta(hours=1), by_model)
    assert found.suggested is None and found.at_hits == (1, 1)
    assert "concurrency is not what reaches it" in found.line()


def test_only_calls_ending_in_the_window_count() -> None:
    spans = [call(1, -120, "a", HIT), call(2, -119, "b", HIT)]
    assert provider_limits(spans, T0, T0 + timedelta(hours=1), by_model) == ()


def test_a_session_counts_once_however_many_calls_it_has_running() -> None:
    spans = [call(n, 0, "a", length=5) for n in range(1, 5)] + [call(9, 1, "b", HIT, length=1)]
    [found] = provider_limits(spans, T0, T0 + timedelta(hours=1), by_model)
    assert found.at_hits == (2, 2) and found.suggested == 1


def test_the_same_spans_give_the_same_answer_in_any_order() -> None:
    spans = [call(n, n, f"s{n % 3}", HIT if n % 4 == 0 else None) for n in range(1, 30)]
    one = provider_limits(spans, T0, T0 + timedelta(hours=1), by_model)
    two = provider_limits(list(reversed(spans)), T0, T0 + timedelta(hours=1), by_model)
    assert one == two and one


def test_limit_gauges_from_the_ticks_spans() -> None:
    from kelpie.app import ControlLoop
    from kelpie.core.engine import Engine
    from kelpie.core.model import Snapshot
    from kelpie.metrics import Metrics

    spans = (
        call(1, 0, "a", length=10),
        call(2, 1, "b", length=10),
        call(3, 2, "c", HIT, length=1),
        call(4, 3, "d", {"status_code": 429, "gen_ai.provider.name": 'evil"} 1\nx'}),
    )
    metrics = Metrics()
    metrics.set_limit_provider(by_model)
    now = T0 + timedelta(minutes=30)
    ControlLoop(Engine([]), lambda: Snapshot(now=now, spans=spans), metrics).tick_once()
    text = metrics.render()
    assert 'kelpie_llm_provider_trace_limit_hits{provider="anthropic"} 1' in text
    assert 'kelpie_llm_provider_suggested_concurrency{provider="anthropic"} 2' in text
    assert '\nx"' not in text  # a provider named by a span is escaped in its label
    # Bogus names sorting first do not crowd a real provider out: the most hits first.
    bogus = tuple(
        call(100 + n, 4, f"z{n}", {"status_code": 429, "gen_ai.provider.name": f"a{n:02d}"})
        for n in range(25)
    )
    more = (call(200, 5, "e", HIT), call(201, 5.5, "f", HIT))
    crowded = Metrics()
    crowded.set_limit_provider(by_model)
    snap = Snapshot(now=now, spans=spans + bogus + more)
    ControlLoop(Engine([]), lambda: snap, crowded).tick_once()
    assert 'kelpie_llm_provider_trace_limit_hits{provider="anthropic"} 3' in crowded.render()
    off = Metrics()
    ControlLoop(Engine([]), lambda: Snapshot(now=now, spans=spans), off).tick_once()
    assert "kelpie_llm_provider_trace_limit_hits" not in off.render()


def test_the_report_lists_hits_and_claude_codes_own_metrics() -> None:
    from datetime import date

    from kelpie.report.daily import ReportInputs, build_report, render_markdown

    spans = (call(1, 0, "a", length=10), call(2, 1, "b", length=10), call(3, 2, "c", HIT))
    inputs = ReportInputs(
        day=date(2026, 3, 2), tz=UTC, spans=spans, metered=(("claude-x", 1200, 0.5),)
    )
    text = render_markdown(build_report(inputs))
    section = text.split("## Provider limits")[1].split("\n## ")[0]
    assert "anthropic: 1 limit hit(s) in 3 model call(s), with 3 session(s)" in section
    assert "suggested concurrency 2" in section
    assert "claude-x 1,200 tokens, $0.50 reported" in section
    quiet = render_markdown(build_report(ReportInputs(day=date(2026, 3, 2), tz=UTC)))
    assert "## Provider limits" not in quiet
