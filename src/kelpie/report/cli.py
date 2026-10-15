"""``kelpie report [--date YYYY-MM-DD] [--send]``: wiring between the app's settings and
:mod:`kelpie.report.run` (imported lazily by :mod:`kelpie.cli`)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import date, datetime, timedelta

from kelpie.app import Settings, build_adapters, merged_snapshot, utc_now
from kelpie.core.budgets import BudgetMode
from kelpie.core.config import Mode
from kelpie.metrics import Metrics
from kelpie.report.fixes import MANUAL, FixEvent, FixStore
from kelpie.report.run import ReportSources, default_day, generate, send, write
from kelpie.rules import REGISTRY

log = logging.getLogger(__name__)
FUTURE_SKEW = timedelta(minutes=5)  # clocks of the operator's machine and the farm


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
) -> tuple[str, str, int]:
    """``(stdout, stderr, exit code)``: the report on stdout, problems on stderr (so
    ``kelpie report > day.md`` under cron keeps them apart); 2: bad input, 1: the database
    could not be read or the summary did not reach every channel."""
    try:
        day = date.fromisoformat(day_text) if day_text else default_day(clock(), settings.report)
    except ValueError:
        return "", f"kelpie: --date must be YYYY-MM-DD, got {day_text!r}\n", 2
    metrics = Metrics()
    board = merged_snapshot(build_adapters(settings), metrics, clock)()
    try:
        report, markdown = generate(sources(settings), day, board)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return "", f"kelpie: cannot read the database {settings.db_path}: {exc}\n", 1
    path = write(markdown, settings.report, day)
    if path is not None:
        log.info("report written to %s", path)
    if not deliver:
        return markdown, "", 0
    try:
        delivery = settings.alerts.build(metrics)
    except ValueError as exc:
        return markdown, f"kelpie: not sent: {exc}\n", 1
    if delivery is None:
        return markdown, "kelpie: not sent: no [[alerts.channels]] configured\n", 1
    try:
        send(report, settings.report, delivery)
    finally:
        delivery.close(timeout=10.0)
    if metrics.alerts_failed or metrics.alerts_dropped or not metrics.alerts_sent:
        # failed, dropped, or no channel takes [report] severity (min_severity)
        return markdown, "kelpie: the summary did not reach every channel (see the log)\n", 1
    return markdown, "", 0


def note_fix(
    settings: Settings,
    note: str,
    rule: str | None = None,
    command: str | None = None,
    role: str | None = None,
    when: str | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[str, int]:
    """``kelpie fix``: record a manual fix for the daily report to verify."""
    if settings.db_path is None:
        return "kelpie: fixes need a database ([storage] path is :memory:)\n", 2
    if not settings.db_path.exists():
        return f"kelpie: no database at {settings.db_path} yet; run kelpie serve first\n", 2
    now = clock()
    try:
        at = datetime.fromisoformat(when) if when else now
    except ValueError:
        return f"kelpie: --at must be an ISO time, got {when!r}\n", 2
    if at.tzinfo is None:
        at = at.replace(tzinfo=settings.report.tz)  # as typed, in [report] timezone
    if at > now + FUTURE_SKEW:
        return f"kelpie: --at {at.isoformat(timespec='minutes')} is in the future\n", 2
    if rule is not None and rule not in REGISTRY:
        return f"kelpie: unknown rule {rule!r}\n", 2
    event = FixEvent(at, MANUAL, note, rule, command, role)
    try:
        with FixStore(settings.db_path) as store:
            store.add(event)
    except ValueError as exc:
        return f"kelpie: {exc}\n", 2
    except (OSError, sqlite3.Error) as exc:
        return f"kelpie: cannot write to {settings.db_path}: {exc}\n", 1
    return f"fix noted at {at.isoformat(timespec='minutes')}: {event.note.strip()}\n", 0
