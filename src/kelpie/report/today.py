"""Today's cost of chaos and spend so far, for the read-only farm view ([view]).

The same report the daily run builds (:func:`kelpie.report.daily.build_report`), for the day
in progress, over the control loop's last snapshot: no LLM (the root-cause section is never
asked for), nothing written, nothing sent. ``kelpie serve`` computes it on the view's own
thread every few minutes, never on a request.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from kelpie.app import Settings
from kelpie.core.farmview import Today
from kelpie.core.model import Snapshot
from kelpie.report.cli import sources
from kelpie.report.daily import build_report
from kelpie.report.run import load_inputs


def today_figures(settings: Settings) -> Callable[[Snapshot, datetime], Today]:
    """A function of (snapshot, now) → the day's figures so far; it raises what reading the
    database raises (OSError, sqlite3.Error, RuntimeError)."""
    src = sources(settings)

    def compute(board: Snapshot, now: datetime) -> Today:
        day = now.astimezone(settings.report.tz).date()
        report = build_report(load_inputs(src, day, board))
        return Today(
            at=now,
            day=day.isoformat(),
            chaos_hours=report.chaos_hours,
            chaos_usd=report.chaos_usd,
            spend_usd=report.spend.usd,
            tokens=report.spend.tokens,
            plan_tokens=report.spend.plan_tokens,
        )

    return compute
