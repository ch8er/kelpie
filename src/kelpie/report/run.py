"""``kelpie report``: build one day's report from storage and the board, write it as
Markdown, and optionally send its summary through the alert channels.

``kelpie serve`` runs it daily with ``[report] at`` (:mod:`kelpie.report.schedule`);
``kelpie report`` (cron, by hand) stays available. Both widen the board's lookback to what
the day needs (:func:`kelpie.report.cli.report_settings`); for days long past the hook
spool may have rotated, so run it daily.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

from kelpie.core.budgets import PriceTable
from kelpie.core.config import Mode
from kelpie.core.model import Reconciliation, Snapshot, Snooze
from kelpie.core.spend import SpendConfig
from kelpie.report.config import ReportSettings
from kelpie.report.daily import (
    DailyReport,
    ReportInputs,
    StuckCheck,
    build_report,
    day_bounds,
    render_markdown,
    summary_items,
    summary_title,
)
from kelpie.report.farm import UNKNOWN_FARM, FarmFacts
from kelpie.report.root_cause import Llm, with_root_cause
from kelpie.report.store import ReportStore
from kelpie.report.waits import BlockKind
from kelpie.traces.hooks import merge_hook_spans
from kelpie.traces.outputs import OutputSettings


class Sender(Protocol):
    """What the report needs of :class:`kelpie.alerts.Delivery`."""

    def send_summary(
        self,
        headline: str,
        items: Sequence[str],
        severity: str,
        channels: Sequence[str] | None = None,
        link: str | None = None,
        title: str | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ReportSources:
    """Configuration the report reads, taken from the app's settings."""

    db_path: Path | None
    report: ReportSettings
    prices: PriceTable
    outputs: OutputSettings
    estimated: bool  # a subscription-mode budget: dollars are estimates
    modes: Mapping[str, Mode]
    spend: SpendConfig | None = None  # [spend]: each provider's spend mode
    hold_labels: tuple[str, ...] = ()  # rule 6's hold labels (bottlenecks)
    block_kinds: tuple[BlockKind, ...] = ()  # [[block_conditions]], matched offline
    stuck_check: StuckCheck | None = None  # rule 3's, as configured (None: its defaults)
    act_blockers: Mapping[str, str] = field(default_factory=dict)  # rule -> what is missing
    act_gaps: Mapping[str, str] = field(default_factory=dict)  # rule -> where it only alerts
    farm: FarmFacts = UNKNOWN_FARM  # what the farm already has (from config)
    # Providers' bills against the estimate for UTC days ``since`` to ``until``
    # (``kelpie.app.read_reconciliations``); None: no cost reports configured.
    reconcile: Callable[[date, date], Sequence[Reconciliation] | str] | None = None


def default_day(now: datetime, settings: ReportSettings) -> date:
    """Yesterday in the report's time zone."""
    return now.astimezone(settings.tz).date() - timedelta(days=1)


def load_inputs(sources: ReportSources, day: date, board: Snapshot) -> ReportInputs:
    """Everything the day's report needs; raises OSError/sqlite3.Error/RuntimeError when the
    database cannot be read (a missing database is an empty one)."""
    tz = sources.report.tz
    start, end = day_bounds(day, tz)
    day_before, _ = day_bounds(day - timedelta(days=1), tz)  # 23 or 25h around DST
    # Earlier days: repeated output offenders, and the before-window of yesterday's fixes.
    # An hour of slack: a calendar day around a DST change is 23 or 25 hours.
    history = min(
        start - timedelta(days=max(sources.outputs.repeat_days - 1, 0)),
        day_before - sources.report.verify_window,
    ) - timedelta(hours=1)
    actions, states, spans, fixes, attribution = [], [], [], [], None
    snoozes: list[Snooze] = []
    if sources.db_path is not None and sources.db_path.exists():
        with ReportStore(sources.db_path) as store:
            actions = store.actions(history, end)
            states = store.states(history, end)
            spans = store.spans(history, end)
            fixes = store.fixes(day_before, end)
            attribution = store.attribution()
            snoozes = store.snoozes(start)
    # Hook spans (platforms without OTLP) live in the adapters, not in the store; the same
    # call reported both ways is kept once.
    merged = merge_hook_spans(
        [*spans, *(s for s in board.spans if history <= (s.end or s.start) < end)]
    ).spans
    return ReportInputs(
        reconciliations=_bills(sources, end),
        bill_day=_bill_day(sources, end),
        day=day,
        tz=tz,
        actions=actions,
        states=states,
        spans=merged,
        tasks=board.tasks,
        owners=board.owners,
        sessions=board.sessions,
        prices=sources.prices,
        spend=sources.spend,
        estimated=sources.estimated,
        outputs=sources.outputs,
        attribution=attribution,
        modes=sources.modes,
        hour_usd=sources.report.hour_usd,
        fixes=fixes,
        verify_window=sources.report.verify_window,
        review_roles=sources.report.review_roles,
        min_sample=sources.report.min_sample,
        section_config=sources.report.sections,
        conditions=board.conditions,
        hold_labels=sources.hold_labels,
        block_kinds=sources.block_kinds,
        dependencies=board.dependencies,
        stuck_check=sources.stuck_check,
        act_blockers=sources.act_blockers,
        act_gaps=sources.act_gaps,
        farm=sources.farm,
        snoozes=snoozes,
    )


def bill_days(end: datetime) -> tuple[date, date]:
    """The UTC day whose bills the report of a day ending at ``end`` shows: the one that
    ended 18 to 42 hours before (a bill settles hours after its day ends; the report runs
    just after ``end``). Consecutive reports show consecutive days: a report's day ends at
    local midnight, and the 18-hour edge falls on a UTC midnight only at offset +6, which
    no zone's current rules move across; a zone whose offset does cross it shows one day
    twice or skips one."""
    t = end.astimezone(UTC) - timedelta(hours=18)
    day = t.date() - timedelta(days=1)
    return day, day


def _bills(sources: ReportSources, end: datetime) -> Sequence[Reconciliation] | str:
    if sources.reconcile is None:
        return ()
    return sources.reconcile(*bill_days(end))


def _bill_day(sources: ReportSources, end: datetime) -> date | None:
    return bill_days(end)[0] if sources.reconcile is not None else None


def write(markdown: str, settings: ReportSettings, day: date) -> Path | None:
    """Write the report to ``[report] dir``; None when no directory is configured."""
    if settings.dir is None:
        return None
    settings.dir.mkdir(parents=True, exist_ok=True)
    path = settings.dir / f"kelpie-report-{day.isoformat()}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def send(report: DailyReport, settings: ReportSettings, sender: Sender) -> None:
    headline, items, link = summary_items(report, settings.url)
    sender.send_summary(
        headline, items, settings.severity, settings.channels, link, summary_title(report)
    )


def generate(
    sources: ReportSources, day: date, board: Snapshot, llm: Llm | None = None
) -> tuple[DailyReport, str]:
    """The day's report and its Markdown. With ``[report.llm]``, the Markdown also gets the
    LLM root-cause section (``llm``: a client for tests; default: the provider's API)."""
    report = build_report(load_inputs(sources, day, board))
    report = with_root_cause(report, sources.report.llm, llm)
    return report, render_markdown(report, sources.report.task_url)
