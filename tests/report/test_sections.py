"""Report sections: extra analyses render inside the one daily report."""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import kelpie.report.daily as daily
import kelpie.report.sections as sections_pkg
from kelpie.core.model import Span, SpanKind
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections import (
    SECTIONS,
    Advice,
    Section,
    SectionContext,
    load_sections,
    register_section,
)
from kelpie.report.store import StateRow

DAY = date(2026, 3, 2)


def inputs(**kw: Any) -> ReportInputs:
    return ReportInputs(**{"day": DAY, "tz": UTC, **kw})


def test_a_section_renders_and_competes_for_the_top_3() -> None:
    seen: list[SectionContext] = []

    def cache(ctx: SectionContext) -> Section:
        seen.append(ctx)
        advice = Advice("Cache the system prompt", "hit rate 12% for reviewers", usd=9.5)
        return Section("Token optimization", ("- cache hit rate: 12%",), advice=(advice,))

    report = build_report(inputs(), {"test-cache": cache, "test-quiet": lambda ctx: None})
    assert [s.title for s in report.sections] == ["Token optimization"]
    assert report.recommendations[0].title == "Cache the system prompt"
    markdown = render_markdown(report)
    assert "## Token optimization\n\n- cache hit rate: 12%" in markdown
    assert markdown.index("## Top 3") < markdown.index("## Token optimization")
    [ctx] = seen
    assert ctx.start.date() == DAY and ctx.spans == () and ctx.spend.tokens == 0
    assert ctx.price([]).tokens == 0


def test_context_is_read_only() -> None:
    span = Span(
        "tr", "s1", "chat", SpanKind.LLM, datetime(2026, 3, 2, 9, tzinfo=UTC),
        session_id="S-1", model="m", input_tokens=10,
    )  # fmt: skip

    def meddle(ctx: SectionContext) -> Section | None:
        with pytest.raises(TypeError):
            ctx.counted[("x", "y")] = "z"  # type: ignore[index]
        with pytest.raises(TypeError):
            ctx.roles["S-1"] = "x"  # type: ignore[index]
        assert isinstance(ctx.spans, tuple) and len(ctx.spans) == 1
        return None

    build_report(inputs(spans=[span]), {"test-meddle": meddle})


@pytest.mark.parametrize("bad", [lambda ctx: ["not", "a", "section"], lambda ctx: 1 / 0])
def test_a_failing_section_does_not_fail_the_report(bad: Any) -> None:
    report = build_report(inputs(), {"test-bad": bad, "test-ok": lambda ctx: Section("Ok", ("x",))})
    assert [s.title for s in report.sections] == ["test-bad", "Ok"]
    assert "This section failed (test-bad)" in render_markdown(report)


def test_empty_sections_are_left_out_but_their_advice_counts() -> None:
    assert build_report(inputs(), {"test-empty": lambda ctx: Section("E", ())}).sections == ()
    only = Section("Only", (), advice=(Advice("Only advice", "d", usd=5.0),))
    report = build_report(inputs(), {"test-only": lambda ctx: only})
    assert [a.title for a in report.recommendations] == ["Only advice"]
    assert "## Only" not in render_markdown(report)


@pytest.mark.parametrize(
    "bad",
    [
        Section("T", (1,)),  # type: ignore[arg-type]
        Section("T", ("x",), advice=(Advice("A", "d", usd="5"),)),  # type: ignore[arg-type]
        Section("T", ("x",), advice=(Advice("A", "d", usd=float("nan")),)),
        Section("T", ("x",), advice=[Advice("A", "d")]),  # type: ignore[arg-type]
    ],
)
def test_malformed_sections_are_noted(bad: Section) -> None:
    report = build_report(inputs(), {"test-bad": lambda ctx: bad})
    assert "This section failed (test-bad)" in render_markdown(report)


def test_the_best_advice_of_a_title_wins() -> None:
    low = Section("S", ("x",), advice=(Advice("Reduce blocks that outlived their cause", "d"),))
    stuck = StateRow(
        _state("stale_blocks:T-1", "stale_blocks", datetime(2026, 3, 2, 8, tzinfo=UTC))
    )
    report = build_report(inputs(states=[stuck]), {"test-low": lambda ctx: low})
    [top] = report.recommendations
    assert top.hours == pytest.approx(2.0)  # the built-in one, with its hours


def test_section_advice_ranks_with_built_in_advice_and_titles_are_unique() -> None:
    stuck = StateRow(
        _state("stale_blocks:T-1", "stale_blocks", datetime(2026, 3, 2, 8, tzinfo=UTC))
    )
    section = Section(
        "S",
        ("x",),
        advice=(
            Advice("Cheaper model for triage", "d", usd=5.0),
            Advice("Cheaper model for triage", "duplicate", usd=4.0),
            Advice("Shorter heartbeats", "d", hours=0.5),
        ),
    )
    report = build_report(inputs(states=[stuck]), {"test-s": lambda ctx: section})
    titles = [a.title for a in report.recommendations]
    # alternating: best by dollars, best by hours, then the next by hours
    assert titles == [
        "Cheaper model for triage",
        "Reduce blocks that outlived their cause",
        "Shorter heartbeats",
    ]
    report = build_report(inputs(states=[stuck], hour_usd=10.0), {"test-s": lambda ctx: section})
    assert report.recommendations[0].title == "Reduce blocks that outlived their cause"


def _state(sid: str, rule: str, since: datetime) -> Any:
    from kelpie.core.actions import Severity
    from kelpie.core.states import OpenState

    return OpenState(
        sid, rule, since, since, since + timedelta(hours=2), 0, Severity.WARNING, "s", "T-1"
    )


def test_modules_in_the_package_register_and_failures_are_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = tmp_path / "extra_sections"
    extra.mkdir()
    (extra / "zz_probe.py").write_text(
        "from kelpie.report.sections import Section, register_section\n"
        "@register_section('zz-probe')\n"
        "def probe(ctx):\n"
        "    return Section('Probe', ('probe ran',))\n"
    )
    (extra / "zz_broken.py").write_text("raise ImportError('missing optional dependency')\n")
    monkeypatch.setattr(sections_pkg, "__path__", [*sections_pkg.__path__, str(extra)])
    monkeypatch.setattr(sections_pkg, "SECTIONS", dict(SECTIONS))
    monkeypatch.setattr(daily, "SECTIONS", sections_pkg.SECTIONS)
    monkeypatch.setattr(sys, "modules", dict(sys.modules))  # the probe modules go away after
    assert load_sections() == ["zz_broken"]
    assert "zz-probe" in sections_pkg.SECTIONS
    markdown = render_markdown(build_report(inputs()))  # default: load and run the registered
    assert "probe ran" in markdown and "This section failed to load (zz_broken)" in markdown


def test_names_are_unique() -> None:
    register_section("unique-test")(lambda ctx: None)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_section("unique-test")(lambda ctx: None)
    finally:
        del SECTIONS["unique-test"]
