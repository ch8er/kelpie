"""Report sections: extra analyses render inside the one daily report."""

from __future__ import annotations

from datetime import UTC, date

import pytest

from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections import (
    SECTIONS,
    Advice,
    Section,
    SectionContext,
    register_section,
)

DAY = date(2026, 3, 2)


def test_a_section_renders_and_competes_for_the_top_3(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[SectionContext] = []

    def tokens(ctx: SectionContext) -> Section:
        seen.append(ctx)
        advice = Advice("Cache the system prompt", "hit rate 12% for reviewers", usd=9.5)
        return Section("Token optimization", ("- cache hit rate: 12%",), advice=(advice,))

    monkeypatch.setitem(SECTIONS, "tokens", tokens)
    monkeypatch.setitem(SECTIONS, "quiet", lambda ctx: None)
    report = build_report(ReportInputs(day=DAY, tz=UTC))
    assert [s.title for s in report.sections] == ["Token optimization"]
    assert report.recommendations[0].title == "Cache the system prompt"
    markdown = render_markdown(report)
    assert "## Token optimization\n\n- cache hit rate: 12%" in markdown
    assert markdown.index("## Top 3") < markdown.index("## Token optimization")
    [ctx] = seen
    assert ctx.start.date() == DAY and ctx.spans == [] and ctx.price([]).tokens == 0


def test_a_failing_section_does_not_fail_the_report(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(ctx: SectionContext) -> Section:
        raise RuntimeError("boom")

    monkeypatch.setitem(SECTIONS, "broken", broken)
    monkeypatch.setitem(SECTIONS, "a-first", lambda ctx: Section("First", ("ok",)))
    report = build_report(ReportInputs(day=DAY, tz=UTC))
    assert [s.title for s in report.sections] == ["First", "broken"]
    assert "This section failed (broken)" in render_markdown(report)


def test_names_are_unique() -> None:
    register_section("unique-test")(lambda ctx: None)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_section("unique-test")(lambda ctx: None)
    finally:
        del SECTIONS["unique-test"]
