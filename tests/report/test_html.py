"""The daily report as a self-contained HTML page."""

from __future__ import annotations

import re

from kelpie.report import md
from kelpie.report.html import blocks, inline, render


def test_inline_markdown_and_escapes() -> None:
    assert inline("**bold** and *it* and `code`") == (
        "<strong>bold</strong> and <em>it</em> and <code>code</code>"
    )
    assert inline("a \\* b \\` c \\| d") == "a * b ` c | d"
    assert inline("``a`b``") == "<code>a`b</code>"
    assert inline("`` `x` ``") == "<code>`x`</code>"
    assert inline("x **bold `code`** y") == "x <strong>bold <code>code</code></strong> y"
    assert inline("``a```") == "``a```"  # no closing run of exactly two backticks
    assert inline("a * b * c") == "a * b * c"  # spaced stars are not emphasis


def test_an_escaped_star_never_closes_emphasis() -> None:
    assert inline(f"**Needs you, {md.text('a*')}**") == "<strong>Needs you, a*</strong>"
    assert inline(f"*{md.text('x*')}*") == "<em>x*</em>"


def test_agent_text_stays_text() -> None:
    hostile = "<script>alert(1)</script> & &lt;b&gt; [x](javascript:alert(1)) **"
    out = inline(md.text(hostile))
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "&amp;lt;b&amp;gt;" in out  # an entity the agent wrote shows as written
    assert "href" not in out
    assert inline(md.code("<b>|`")) == "<code>&lt;b&gt;|`</code>"


def test_only_http_links_are_links() -> None:
    assert inline("[t_1](https://board.example/t_1)") == (
        '<a href="https://board.example/t_1">t_1</a>'
    )
    assert inline("[t_1](javascript:alert(1))") == "t_1)"  # no link; the URL ends at ")"
    assert inline('[x](https://e.example/"onmouseover=)') == (
        '<a href="https://e.example/&quot;onmouseover=">x</a>'
    )


def test_tables_with_alignment_and_escaped_pipes() -> None:
    table = [
        "| Cause | Hours | Tasks |",
        "|---|---:|:---:|",
        "| a \\| b | 1.5 | `x\\|y` |",
        "| short |",
    ]
    (out,) = blocks(table)
    assert '<th>Cause</th><th class="r">Hours</th><th class="c">Tasks</th>' in out
    assert "<td>a | b</td>" in out and "<code>x|y</code>" in out
    assert '<tr><td>short</td><td class="r"></td><td class="c"></td></tr>' in out


def test_lists_headings_and_paragraphs() -> None:
    text = [
        "## Needs a human",
        "",
        "- **one**: first",
        "  continued",
        "- two",
        "1. ranked",
        "2. second",
        "Some text",
        "on two lines.",
        "### Small",
    ]
    out = list(blocks(text))
    assert out[0] == "<h2>Needs a human</h2>"
    assert out[1] == "<ul>\n<li><strong>one</strong>: first continued</li>\n<li>two</li>\n</ul>"
    assert out[2] == "<ol>\n<li>ranked</li>\n<li>second</li>\n</ol>"
    assert out[3] == "<p>Some text on two lines.</p>"
    assert out[4] == "<h3>Small</h3>"


def test_a_page_is_self_contained() -> None:
    page = render("# Kelpie daily report: 2026-03-03\n\nCost of chaos.\n", "Report <x>", "sub & co")
    assert page.startswith("<!doctype html>")
    assert "<title>Report &lt;x&gt;</title>" in page
    assert "<h1>Kelpie daily report: 2026-03-03</h1>" in page and "sub &amp; co" in page
    assert not re.search(r"<script|<link|<img|src=|url\(|@import", page)


def test_a_page_without_a_heading_uses_its_title_as_text() -> None:
    page = render("Just text.\n", "**not bold** <x>")
    assert "<h1>**not bold** &lt;x&gt;</h1>" in page and "<header>\n<h1>" in page
    assert (
        "<p>Just text.</p>" in page
        and "<header>\n<h1>**not bold** &lt;x&gt;</h1>\n</header>" in page
    )
