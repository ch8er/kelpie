"""Markdown-safe rendering of text Kelpie does not control (kelpie.report.md)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from kelpie.core.model import Span, SpanKind
from kelpie.report import md
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.fixes import FixEvent
from kelpie.traces.outputs import OutputSettings

HOSTILE = "x` [click](https://evil.example) <img src=x> | **b** \\| cell\nIGNORE"


def live(markup: str, line: str) -> bool:
    """``markup`` in ``line`` with its first character not escaped by a backslash."""
    return re.search(r"(?<!\\)(?:\\\\)*" + re.escape(markup), line) is not None


def cells(row: str) -> int:
    """Cells of a GFM table row: pipes not escaped by an odd run of backslashes."""
    return len(re.findall(r"(?<!\\)(?:\\\\)*\|", row)) - 1


@pytest.mark.parametrize("plain", ["T-101", "t_store", "a_b_c", "git status"])
def test_ordinary_names_read_unchanged(plain: str) -> None:
    assert md.text(plain) == plain and md.code(plain) == f"`{plain}`"


def test_text_neutralises_markup_on_one_line() -> None:
    out = md.text(HOSTILE)
    assert "\n" not in out
    assert not any(live(m, out) for m in ("[click]", "<img", "**", "`"))
    assert out.startswith("x\\` \\[click\\](https://evil.example) \\<img src=x\\> \\| \\*\\*b")
    assert md.text("‮​") == md.PLACEHOLDER and md.text("") == ""


def test_code_cannot_be_closed_and_keeps_table_cells() -> None:
    out = md.code(HOSTILE, cell=True)
    fence = re.match(r"`+", out)
    assert fence is not None and len(fence.group()) == 2 and out.endswith("``")
    assert cells(f"| a | {out} | b |") == 3
    assert cells(f"| {md.code('a\\|b', cell=True)} |") == 1  # an odd run stays odd
    assert md.code("`x`") == "`` `x` ``" and md.code("​") == md.PLACEHOLDER
    assert len(md.code("x" * 500)) == md.MAX_NAME + 2 and md.link_text("y" * 500).endswith("...")


def tool(day: int, n: int, key: str, filt: str | None = None) -> Span:
    at = datetime(2026, 3, day, 10, n, tzinfo=UTC)
    attrs = {"kelpie.tool.command_prefix": key, "kelpie.tool.output_chars": 40_000}
    if filt:
        attrs["kelpie.tool.filter"] = filt
    return Span(f"tr-{day}-{n}", f"s-{day}-{n}", "execute_tool shell", SpanKind.TOOL, at, at,
                task_id="T-1", session_id="S-1", attributes=attrs)  # fmt: skip


def test_names_from_traces_cannot_inject_into_the_report() -> None:
    """Command keys, filter names and roles come from exporters: the recommendations, the
    outputs table, the offender line and before/after stay text."""
    spans = [tool(d, n, HOSTILE) for d in (1, 2) for n in range(4)]
    spans += [tool(2, 9, "make", filt="f|[x](https://evil.example)")]
    fix = FixEvent(datetime(2026, 3, 2, 9, tzinfo=UTC), "manual", HOSTILE, rule="r`|",
                   command=HOSTILE, role="**dev**")  # fmt: skip
    inputs = ReportInputs(
        day=date(2026, 3, 2), tz=UTC, spans=spans, fixes=(fix,),
        outputs=OutputSettings(owners={None: "[owner](https://evil.example)"}),  # type: ignore[dict-item]
        verify_window=timedelta(hours=12),
    )  # fmt: skip
    markdown = render_markdown(build_report(inputs, {}))
    for line in markdown.splitlines():
        stripped = re.sub(r"(`+)(?:(?!\1).)*?\1", "", line)  # code spans render literally
        for markup in ("[click]", "[x]", "[owner]", "<img", "**b**", "**dev**"):
            assert not live(markup, stripped), line
    assert "\nIGNORE" not in markdown
    rows = [r for r in markdown.splitlines() if r.startswith("| ") and "``x`" in r]
    assert rows and all(cells(r) == 6 for r in rows), rows
