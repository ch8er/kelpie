"""How the daily report's chat summary reads: the alert style, for a summary.

An event with a :class:`~kelpie.core.alerts.Summary` is laid out as::

    Kelpie daily report 2026-03-02
    🟡 <headline, bold>

    Biggest wait: …
    Needs a human: 2 open condition(s).
    1. <recommendation>
    Open report: https://reports.example.org/2026-03-02

The title line stands only when the report gave one; "Open report" only when it gave a link
(``http(s)``, validated here). The icon is the event's severity, "Open report" and "and N
more" come from :mod:`kelpie.alerts.i18n`, and everything else (the title, the headline, the
lines) is the report's own English, passed through as it is, cleaned like any untrusted
text (no control or invisible characters, @mentions broken) and escaped by each renderer.
One line each is cut to :data:`MAX_LINE` characters (the report's longest, the biggest wait,
is about half of that); at most :data:`MAX_LINES` are shown and the rest counted ("and 3
more"). Slack gets the lines in blocks of at most ``SLACK_BLOCK`` characters. Telegram's
limit is measured on the escaped text, and lines are dropped from the end until it fits.

Escaping, defanging, Telegram HTML and Slack ``rich_text`` (text elements Slack shows as
they are; the link as a ``link`` element) work as for alerts (:mod:`kelpie.alerts.render`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from kelpie.alerts import i18n
from kelpie.alerts.render import (
    DEFAULT_RENDERING,
    MAX_TEXT,
    PLAIN,
    SLACK_BLOCK,
    SLACK_BLOCKS,
    SLACK_PLAIN,
    SLACK_TEXT,
    TELEGRAM_HTML,
    Link,
    RenderSettings,
    Style,
    el,
    fit,
    one_line,
    plain_http_url,
    rich,
)
from kelpie.core.alerts import AlertEvent

MAX_LINES = 10  # lines shown; the rest are counted ("and 3 more")
MAX_LINE = 800  # characters of one line
MAX_TITLE = 200
MAX_HEADLINE = 600
# Output budgets after escaping, so that a title, a headline and a few lines always fit
# Telegram's limit together (the link's address, up to MAX_URL characters, comes on top).
_BUDGET = {"title": 400, "headline": 1000, "line": 1600}
MIN_LINE = 80  # least a line is cut to before whole lines are dropped


@dataclass(frozen=True, slots=True)
class SummaryView:
    icon: str
    title: str | None
    headline: str
    lines: tuple[str, ...]
    hidden: int  # lines not shown ("and 3 more")
    link: Link | None
    language: str = i18n.DEFAULT

    @property
    def more(self) -> str | None:
        return i18n.text(self.language, "label.more", n=self.hidden) if self.hidden else None

    def without_last_line(self) -> SummaryView:
        return replace(self, lines=self.lines[:-1], hidden=self.hidden + 1)


def view(event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING) -> SummaryView:
    summary = event.summary
    assert summary is not None, "not a summary"
    lang = settings.language
    lines = [line for line in (one_line(x, MAX_LINE) for x in summary.lines) if line]
    title = one_line(summary.title, MAX_TITLE) if summary.title else None
    headline = one_line(event.headline or "", MAX_HEADLINE) or title or "Kelpie"
    url = plain_http_url(summary.link)  # anything else is no link
    return SummaryView(
        icon=i18n.ICONS[event.severity],
        title=title if title != headline else None,
        headline=headline,
        lines=tuple(lines[:MAX_LINES]),
        hidden=max(len(lines) - MAX_LINES, 0),
        link=Link(i18n.text(lang, "label.open_report"), url) if url else None,
        language=lang,
    )


def text(v: SummaryView, style: Style = PLAIN, limit: int = MAX_TEXT) -> str:
    """The summary as one text in ``style``, at most ``limit`` units. What does not fit goes
    in this order: the longest lines are cut shorter (all lines to one common length, no
    less than ``MIN_LINE``, so a short line such as a numbered recommendation stays whole),
    then lines are counted in "and N more", the last one first, then the link."""
    while True:
        longest = _BUDGET["line"]
        if style.measure(out := _text(v, style, longest)) <= limit:
            return out
        if style.measure(out := _text(v, style, MIN_LINE)) <= limit:
            # `low` fits, `high` does not: narrow down to the longest cut that fits
            low, high = MIN_LINE, longest
            while high - low > 1:
                mid = (low + high) // 2
                if style.measure(_text(v, style, mid)) <= limit:
                    low = mid
                else:
                    high = mid
            return _text(v, style, low)
        if v.lines:
            v = v.without_last_line()
        elif v.link is not None:  # only a pathological address is this long
            v = replace(v, link=None)
        else:
            return out


def _text(v: SummaryView, style: Style, line_budget: int) -> str:
    out = [fit(v.title, _BUDGET["title"], style)] if v.title else []
    out.append(f"{v.icon} {style.bold(fit(v.headline, _BUDGET['headline'], style))}")
    if v.lines or v.hidden:
        out += ["", *(fit(line, line_budget, style) for line in v.lines)]
        if v.more:
            out.append(style.esc(v.more))
    if v.link is not None:
        out += ["", style.link(style.esc(v.link.label), v.link.url)]
    return "\n".join(out)


def plain(event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING) -> str:
    return text(view(event, settings), PLAIN)


def telegram_html(event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING) -> str:
    return text(view(event, settings), TELEGRAM_HTML)


def telegram_payload(
    event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING, *, html_format: bool = True
) -> dict[str, Any]:
    """The message fields of a ``sendMessage`` request."""
    if html_format:
        return {"text": telegram_html(event, settings), "parse_mode": "HTML"}
    return {"text": plain(event, settings)}


def slack_blocks(v: SummaryView) -> list[dict[str, Any]]:
    """Block Kit ``rich_text``: the title and bold headline, the lines, the link."""
    head = [el(f"{v.title}\n")] if v.title else []
    blocks = [rich([*head, el(f"{v.icon} "), el(v.headline, bold=True)])]
    for group in _packed([*v.lines, *([v.more] if v.more else [])]):
        blocks.append(rich([el("\n".join(group))]))
    if v.link is not None:
        blocks.append(rich([{"type": "link", "url": v.link.url, "text": v.link.label}]))
    return blocks[:SLACK_BLOCKS]


def _packed(lines: list[str]) -> list[list[str]]:
    """The lines in groups that each stay within ``SLACK_BLOCK`` characters."""
    groups: list[list[str]] = []
    size = 0
    for line in lines:
        if groups and size + 1 + len(line) <= SLACK_BLOCK:
            groups[-1].append(line)
            size += 1 + len(line)
        else:
            groups.append([line])
            size = len(line)
    return groups


def slack_payload(
    event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING
) -> dict[str, Any]:
    """``text`` (the notification and fallback: plain, escaped, ``mrkdwn: false``) and
    ``blocks``."""
    v = view(event, settings)
    return {"text": text(v, SLACK_PLAIN, SLACK_TEXT), "mrkdwn": False, "blocks": slack_blocks(v)}
