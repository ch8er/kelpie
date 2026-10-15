"""``kelpie report [--date YYYY-MM-DD] [--send]``: wiring between the app's settings and
:mod:`kelpie.report.run` (imported lazily by :mod:`kelpie.cli`)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime

from kelpie.app import Settings, build_adapters, merged_snapshot, utc_now
from kelpie.core.budgets import BudgetMode
from kelpie.core.config import Mode
from kelpie.metrics import Metrics
from kelpie.report.run import ReportSources, default_day, generate, send, write
from kelpie.rules import REGISTRY

log = logging.getLogger(__name__)


def sources(settings: Settings) -> ReportSources:
    engine = settings.engine
    modes = {name: Mode.OBSERVE if engine.dry_run else engine.mode_for(name) for name in REGISTRY}
    return ReportSources(
        db_path=settings.db_path,
        report=settings.report,
        prices=settings.budgets.prices,
        outputs=settings.traces.outputs,
        estimated=any(b.mode is BudgetMode.SUBSCRIPTION for b in settings.budgets.budgets),
        modes=modes,
    )


def run(
    settings: Settings,
    day_text: str | None,
    deliver: bool,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[str, int]:
    """The report as text for stdout and an exit code (2: bad input, 1: not delivered)."""
    try:
        day = date.fromisoformat(day_text) if day_text else default_day(clock(), settings.report)
    except ValueError:
        return f"kelpie: --date must be YYYY-MM-DD, got {day_text!r}\n", 2
    metrics = Metrics()
    board = merged_snapshot(build_adapters(settings), metrics, clock)()
    try:
        report, markdown = generate(sources(settings), day, board)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"kelpie: cannot read the database {settings.db_path}: {exc}\n", 1
    path = write(markdown, settings.report, day)
    if path is not None:
        log.info("report written to %s", path)
    if not deliver:
        return markdown, 0
    try:
        delivery = settings.alerts.build(metrics)
    except ValueError as exc:
        return markdown + f"\nkelpie: not sent: {exc}\n", 1
    if delivery is None:
        return markdown + "\nkelpie: not sent: no [[alerts.channels]] configured\n", 1
    try:
        send(report, settings.report, delivery)
    finally:
        delivery.close(timeout=10.0)
    if not sum(metrics.alerts_sent.values()):
        # failed, dropped, or no channel takes [report] severity (min_severity)
        return markdown + "\nkelpie: the summary was not delivered (see the log)\n", 1
    return markdown, 0
