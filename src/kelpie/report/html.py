"""The daily report as one self-contained HTML page (``kelpie demo --web``).

A renderer for the Markdown the report writes, not a general one: ATX headings, paragraphs,
``-`` and numbered lists (continuation lines indented), GitHub tables with alignment,
``**bold**``, ``*italic*``, code spans of any fence, ``[text](url)`` links and backslash
escapes (:mod:`kelpie.report.md`). Anything else is shown as text.

Everything is escaped: text from agents and the board reaches the page only through
:func:`html.escape`, a link only when its address is http(s), and the page loads nothing
(inline style, no script, no font or image), so it opens offline and cannot call out.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator, Sequence

_HEADING = re.compile(r"(#{1,6}) (.*)")
_ITEM = re.compile(r"(-|\d+\.) (.*)")
_DIVIDER = re.compile(r"\s*:?-{3,}:?\s*")
_INLINE = re.compile(
    r"(?P<escape>\\[\\`*_\[\]<>|#()!&-])"
    # A code span opens on a whole backtick run (taken atomically, so a long run is tried
    # once, never at every length) and closes on a run exactly as long (CommonMark).
    r"|(?P<code>(?<!`)(?P<fence>`++)(?P<body>.+?)(?<!`)(?P=fence)(?!`))"
    r"|(?P<link>\[(?P<label>(?:\\.|[^\]\\])+)\]\((?P<url>[^)\s]+)\))"
    # Emphasis bodies step over escapes, so an escaped ``*`` never closes them.
    r"|(?P<strong>\*\*(?P<strong_body>(?:\\.|[^*\\]|\*(?!\*))+?)\*\*)"
    r"|(?P<em>\*(?P<em_body>(?:\\.|[^*\\])+?)\*)"
)
_SAFE_URL = re.compile(r"https?://", re.IGNORECASE)

STYLE = """
:root { color-scheme: light dark; --fg: #1d2125; --muted: #5f6b76; --line: #d7dde3;
  --head: #f2f5f7; --accent: #0b6e4f; --code: #eef1f4; --bg: #ffffff; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e3e7ea; --muted: #9aa5ae; --line: #38424b; --head: #222a31;
    --accent: #4fc39a; --code: #262e35; --bg: #161b20; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 980px; margin: 0 auto; padding: 32px 16px 64px; }
header p { color: var(--muted); margin: 4px 0 24px; }
h1 { font-size: 1.6rem; margin: 0; }
h2 { font-size: 1.2rem; margin: 32px 0 8px; padding-bottom: 4px;
  border-bottom: 1px solid var(--line); }
h3 { font-size: 1.05rem; margin: 24px 0 6px; }
p, li { max-width: 80ch; }
.table { overflow-x: auto; }
table { border-collapse: collapse; margin: 8px 0 12px; font-size: 14px; }
th, td { border: 1px solid var(--line); padding: 5px 9px; vertical-align: top; }
th { background: var(--head); text-align: left; }
.r { text-align: right; font-variant-numeric: tabular-nums; }
.c { text-align: center; }
code { background: var(--code); padding: 1px 4px; border-radius: 4px; font-size: 0.92em; }
a { color: var(--accent); }
"""


def render(markdown: str, title: str, subtitle: str | None = None) -> str:
    """A complete HTML document for the report ``markdown``."""
    lines = markdown.splitlines()
    heading = ""
    if lines and (m := _HEADING.fullmatch(lines[0])) and len(m[1]) == 1:
        heading, lines = m[2], lines[1:]
    head = f"<h1>{inline(heading)}</h1>" if heading else f"<h1>{html.escape(title)}</h1>"
    if subtitle:
        head += f"\n<p>{html.escape(subtitle)}</p>"
    body = "\n".join(blocks(lines))
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n"
        f"<body>\n<main>\n<header>\n{head}\n</header>\n{body}\n</main>\n</body>\n</html>\n"
    )


def blocks(lines: Sequence[str]) -> Iterator[str]:
    """The block elements of ``lines``."""
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif m := _HEADING.fullmatch(line):
            level = min(len(m[1]) + 1, 6) if len(m[1]) == 1 else len(m[1])
            yield f"<h{level}>{inline(m[2])}</h{level}>"
            i += 1
        elif line.startswith("|") and i + 1 < len(lines) and _is_divider(lines[i + 1]):
            end = i + 2
            while end < len(lines) and lines[end].startswith("|"):
                end += 1
            yield _table(lines[i], lines[i + 1], lines[i + 2 : end])
            i = end
        elif _ITEM.fullmatch(line):
            ordered = line[0].isdigit()
            items: list[str] = []
            while (
                i < len(lines)
                and (m := _ITEM.fullmatch(lines[i]))
                and (m[1][0].isdigit() == ordered)
            ):
                text = m[2]
                i += 1
                while i < len(lines) and lines[i].startswith(" ") and lines[i].strip():
                    text += " " + lines[i].strip()
                    i += 1
                items.append(f"<li>{inline(text)}</li>")
            tag = "ol" if ordered else "ul"
            yield f"<{tag}>\n" + "\n".join(items) + f"\n</{tag}>"
        else:
            text = []
            while i < len(lines) and lines[i].strip() and not _starts_block(lines, i):
                text.append(lines[i].strip())
                i += 1
            yield f"<p>{inline(' '.join(text))}</p>"


def inline(text: str) -> str:
    """Inline Markdown to HTML; everything else escaped."""
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        out.append(_text(text[pos : m.start()]))
        pos = m.end()
        if m["escape"]:
            out.append(html.escape(m["escape"][1]))
        elif m["code"]:
            body = m["body"]
            if body.startswith(" ") and body.endswith(" ") and body.strip():
                body = body[1:-1]
            out.append(f"<code>{html.escape(body)}</code>")
        elif m["link"]:
            label, url = inline(m["label"]), m["url"]
            if _SAFE_URL.match(url):
                out.append(f'<a href="{html.escape(url, quote=True)}">{label}</a>')
            else:
                out.append(label)
        elif m["strong"]:
            out.append(f"<strong>{inline(m['strong_body'])}</strong>")
        elif m["em_body"].strip() == m["em_body"]:
            out.append(f"<em>{inline(m['em_body'])}</em>")
        else:  # "a * b * c" is not emphasis
            out.append(f"*{inline(m['em_body'])}*")
    out.append(_text(text[pos:]))
    return "".join(out)


def _text(run: str) -> str:
    """Plain text: entities the Markdown wrote (``&amp;``) become their character, then
    everything is escaped again for HTML."""
    return html.escape(html.unescape(run), quote=False)


def _starts_block(lines: Sequence[str], i: int) -> bool:
    line = lines[i]
    if _HEADING.fullmatch(line) or _ITEM.fullmatch(line):
        return True
    return line.startswith("|") and i + 1 < len(lines) and _is_divider(lines[i + 1])


def _is_divider(line: str) -> bool:
    cells = _cells(line)
    return bool(cells) and all(_DIVIDER.fullmatch(c) for c in cells)


def _cells(row: str) -> list[str]:
    """A table row's cells: split on ``|`` not escaped by an odd run of backslashes; in a
    cell, ``\\|`` stands for ``|`` (also inside code spans, as GitHub reads tables)."""
    body = row.strip()
    body = body[1:] if body.startswith("|") else body
    cells: list[str] = []
    current: list[str] = []
    slashes = 0
    for char in body:
        if char == "|" and slashes % 2 == 0:
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
        slashes = slashes + 1 if char == "\\" else 0
    if "".join(current).strip():
        cells.append("".join(current))
    return [c.strip().replace("\\|", "|") for c in cells]


def _table(header: str, divider: str, rows: Sequence[str]) -> str:
    aligns = []
    for cell in _cells(divider):
        right, left = cell.endswith(":"), cell.startswith(":")
        aligns.append(' class="c"' if left and right else ' class="r"' if right else "")

    def row(cells: list[str], tag: str) -> str:
        padded = cells + [""] * (len(aligns) - len(cells))
        return (
            "<tr>"
            + "".join(
                f"<{tag}{aligns[k] if k < len(aligns) else ''}>{inline(c)}</{tag}>"
                for k, c in enumerate(padded)
            )
            + "</tr>"
        )

    head = row(_cells(header), "th")
    body = "\n".join(row(_cells(r), "td") for r in rows)
    return (
        f'<div class="table"><table>\n<thead>{head}</thead>\n<tbody>\n{body}\n</tbody>\n'
        "</table></div>"
    )
