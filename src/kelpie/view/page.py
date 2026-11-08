"""The farm view as one read-only HTML page, made for a phone (``GET /`` of ``[view]``).

Server-rendered, no script, no external resource: every value is HTML-escaped and the
reply's Content-Security-Policy allows inline styles only. It reloads itself every minute.
Order: today's cost of chaos and spend so far, then the open tasks per board by attention
(needs you, stuck or blocked, running long, the rest), then open alerts. There are no
buttons or forms: acting stays with the board, the chat and the CLI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from html import escape

from kelpie.core.farmview import Attention, AttentionItem, FarmView

REFRESH_SECONDS = 60
REST_SHOWN = 20  # tasks of "the rest" shown per board; the others are counted

TITLES = {
    Attention.NEEDS_YOU: "Needs you",
    Attention.STUCK: "Stuck or blocked",
    Attention.RUNNING_LONG: "Running long",
    Attention.REST: "The rest",
}

MANIFEST = json.dumps(
    {
        "name": "Kelpie farm view",
        "short_name": "Kelpie",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#1f5f5b",
    }
)

_STYLE = """
:root { color-scheme: light dark; --muted: #666; --line: #ddd; --accent: #1f5f5b;
  --warn: #9a5b00; --bad: #a11; }
@media (prefers-color-scheme: dark) { :root { --muted: #aaa; --line: #333; --accent: #7cc;
  --warn: #e3a64a; --bad: #f77; } }
body { font: 16px/1.4 system-ui, sans-serif; margin: 0 auto; max-width: 46rem;
  padding: 0 16px 2rem; }
h1 { font-size: 1.25rem; margin: 1rem 0 .25rem; }
h2 { font-size: 1.05rem; margin: 1.25rem 0 .25rem; border-bottom: 1px solid var(--line); }
h3 { font-size: .95rem; margin: .75rem 0 .25rem; color: var(--accent); }
.muted, .meta { color: var(--muted); font-size: .85rem; }
.figures { display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; margin: .5rem 0; }
.figure b { display: block; font-size: 1.3rem; }
ul { list-style: none; padding: 0; margin: 0; }
li { padding: .4rem 0; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.needs_you h3 { color: var(--bad); } .stuck h3 { color: var(--warn); }
.why { display: block; color: var(--muted); font-size: .85rem; }
"""


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _age(delta: timedelta) -> str:
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 24 * 60:
        return f"{minutes // 60}h{minutes % 60:02d}m"
    return f"{minutes // 1440}d{minutes % 1440 // 60}h"


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def _item(i: AttentionItem, now: datetime) -> str:
    owner = f" · {_e(i.owner)}" if i.owner else ""
    why = f'<span class="why">{_e(i.why)}</span>' if i.why else ""
    return (
        f"<li><b>{_e(i.task_id)}</b> {_e(i.title)}"
        f'<span class="meta"> · {_e(i.status)} {_age(now - i.since)}{owner}</span>{why}</li>'
    )


def _board(items: list[AttentionItem], now: datetime) -> list[str]:
    out: list[str] = []
    for attention in Attention:
        group = [i for i in items if i.attention == attention]
        if not group:
            continue
        shown = group if attention is not Attention.REST else group[:REST_SHOWN]
        out.append(
            f'<section class="{attention.value}"><h3>{TITLES[attention]} ({len(group)})</h3>'
        )
        out.append("<ul>" + "".join(_item(i, now) for i in shown) + "</ul>")
        if len(group) > len(shown):
            out.append(f'<p class="muted">… and {len(group) - len(shown)} more</p>')
        out.append("</section>")
    return out


def _today(view: FarmView) -> list[str]:
    t = view.today
    if t is None:
        return ['<p class="muted">Today\'s figures are not computed yet.</p>']
    plan = (
        f'<div class="figure"><b>{t.plan_tokens:,}</b>subscription tokens</div>'
        if t.plan_tokens
        else ""
    )
    return [
        '<div class="figures">',
        f'<div class="figure"><b>{t.chaos_hours:.1f} h</b>cost of chaos</div>',
        f'<div class="figure"><b>{_usd(t.chaos_usd)}</b>chaos in dollars</div>',
        f'<div class="figure"><b>{_usd(t.spend_usd)}</b>spend so far</div>',
        plan,
        "</div>",
        f'<p class="meta">{_e(t.day)}, as of {_e(t.at.strftime("%H:%M %Z"))}</p>',
    ]


def render_page(view: FarmView) -> str:
    now = view.now
    body = [
        "<h1>Kelpie farm view</h1>",
        f'<p class="meta">{view.tasks} tasks · {view.alerts_total} open alerts · '
        f"read-only · {_e(now.strftime('%Y-%m-%d %H:%M %Z'))}</p>",
        "<h2>Today</h2>",
        *_today(view),
    ]
    boards: list[str | None] = [*view.boards] if view.boards else [None]
    if view.boards and any(i.board is None for i in view.items):
        boards.append(None)
    for board in boards:
        items = [i for i in view.items if i.board == board]
        title = f"Board {_e(board)}" if board else ("Tasks" if not view.boards else "No board")
        body.append(f"<h2>{title}</h2>")
        empty = "No open tasks." if view.items_total == len(view.items) else "None listed."
        body += _board(items, now) or [f'<p class="muted">{empty}</p>']
    if view.items_total > len(view.items):
        body.append(f'<p class="muted">{view.items_total - len(view.items)} more not listed</p>')
    body.append(f"<h2>Open alerts ({view.alerts_total})</h2>")
    if view.alerts:
        body.append(
            "<ul>"
            + "".join(
                f"<li><b>{_e(a.severity)}</b> {_e(a.rule)}"
                + (f" · {_e(a.task_id)}" if a.task_id else "")
                + f'<span class="meta"> · open {_age(now - a.opened_at)}</span>'
                + f'<span class="why">{_e(a.summary)}</span></li>'
                for a in view.alerts
            )
            + "</ul>"
        )
    else:
        body.append('<p class="muted">None.</p>')
    body += [f'<p class="muted">Note: {_e(p)}</p>' for p in view.problems]
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}">'
        '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">'
        "<title>Kelpie farm view</title>"
        f"<style>{_STYLE}</style></head><body>" + "\n".join(body) + "</body></html>\n"
    )
