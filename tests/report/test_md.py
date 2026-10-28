"""Markdown-safe rendering of text Kelpie does not control (kelpie.report.md)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

import pytest

from kelpie.core.model import Owner, Session, Span, SpanKind
from kelpie.core.spans import span_key
from kelpie.report import md
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.fixes import FixEvent
from kelpie.report.verify import Evidence, verify
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
        outputs=OutputSettings(owners={"dev": "[owner](https://evil.example)"}),
        owners=(Owner("agent-1", "dev"),),
        sessions=(Session("S-1", "T-1", "agent-1", datetime(2026, 3, 1, 9, tzinfo=UTC)),),
        verify_window=timedelta(hours=12),
    )  # fmt: skip
    markdown = render_markdown(build_report(inputs, {}))
    for line in markdown.splitlines():
        stripped = re.sub(r"(`+)(?:(?!\1).)*?\1", "", line)  # code spans render literally
        for markup in ("[click]", "[x]", "[owner]", "<img", "**b**", "**dev**"):
            assert not live(markup, stripped), line
    assert "\nIGNORE" not in markdown
    assert "\\[owner\\](https://evil.example): add errors-only for" in markdown  # rendered
    rows = [r for r in markdown.splitlines() if r.startswith("| ") and "``x`" in r]
    assert rows and all(cells(r) == 6 for r in rows), rows


def test_a_hostile_role_in_a_before_after_metric_keeps_its_cell() -> None:
    """The role of an automatic filter fix comes from the board: its tokens-per-day row
    renders with the role escaped, in one cell."""
    role = "dev | [x](https://evil.example) <img src=x>"
    at = datetime(2026, 3, 2, 12, tzinfo=UTC)
    spans = [
        Span(f"tr-{h}", f"s-{h}", "chat", SpanKind.LLM, at + timedelta(hours=h),
             at + timedelta(hours=h), session_id="S-1", model="m", input_tokens=100)
        for h in (-3, -2, 2, 3)
    ]  # fmt: skip
    event = FixEvent(at, "manual", "note", rule=None, role=role)
    ev = Evidence(end=at + timedelta(hours=6), spans=spans,
                  counted={span_key(s): "m" for s in spans}, roles={"S-1": role},
                  window=timedelta(hours=6))  # fmt: skip
    [metric] = [m for m in verify(event, ev).metrics if m.name.startswith("tokens per day")]
    row = f"| {metric.name} | x |"
    assert cells(row) == 2 and not live("[x]", row) and not live("<img", row)


@pytest.mark.parametrize(
    "value",
    ["r" * 300, "&" * 300, "*<[|" * 80, "a&b " * 90, "x", "a" * 80, "a" * 81, "&" * 16, "&" * 17,
     "", " ", "\t\n", "\x00\x01\u200b"],
)  # fmt: skip
def test_short_text_caps_the_escaped_text(value: str) -> None:
    from kelpie.report.md import MAX_NAME, short_text, text

    out = short_text(value)
    assert len(out) <= MAX_NAME
    if len(text(value)) <= MAX_NAME:
        assert out == text(value)  # what fits is not cut
    else:
        assert out.endswith("...") and not out.endswith("\\...")  # no escape split


@pytest.mark.parametrize(
    "value",
    ["`" * 300, "a`" * 200, "example/" + "b" * 300, "app", "b" * 79, "b" * 80, "b" * 81, "", " "],
)
def test_short_code_caps_the_whole_span(value: str) -> None:
    from kelpie.report.md import MAX_NAME, code, short_code

    out = short_code(value)
    assert len(out) <= MAX_NAME + 2
    if "`" not in value:
        assert out == code(value)  # a plain name renders as md.code renders it
