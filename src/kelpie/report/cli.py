"""``kelpie report [--date YYYY-MM-DD] [--send]``: wiring between the app's settings and
:mod:`kelpie.report.run` (imported lazily by :mod:`kelpie.cli`)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta

from kelpie.app import Settings, _hold_labels, build_adapters, merged_snapshot, utc_now
from kelpie.core.budgets import BudgetMode
from kelpie.core.config import Mode
from kelpie.metrics import Metrics
from kelpie.report.daily import day_bounds
from kelpie.report.fixes import MANUAL, FixEvent, FixStore
from kelpie.report.run import ReportSources, Sender, default_day, generate, send, write
from kelpie.report.schedule import ReportScheduler, RunLog
from kelpie.report.store import ReportStore
from kelpie.rules import REGISTRY

log = logging.getLogger(__name__)
FUTURE_SKEW = timedelta(minutes=5)  # clocks of the operator's machine and the farm
REPORT_SLACK = timedelta(hours=1)
MAX_LIST_DAYS = 3650


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
        hold_labels=tuple(_hold_labels(engine.rules.get("bottlenecks", {}))),
    )


def report_settings(settings: Settings, day: date, now: datetime) -> Settings:
    """``settings`` with the adapters looking back as far as the report of ``day`` needs:
    sessions and done tasks from the before-window of the day before's fixes on, plus an
    hour of slack (DST, a run a few minutes after midnight). Only the report reads this far
    back; ``kelpie serve``'s rules keep ``[adapters.hermes] lookback``."""
    hermes = settings.hermes
    if hermes is None:
        return settings
    day_before, _ = day_bounds(day - timedelta(days=1), settings.report.tz)
    needed = now - (day_before - settings.report.verify_window) + REPORT_SLACK
    if needed <= hermes.lookback:
        return settings
    return replace(settings, hermes=replace(hermes, lookback=needed))


def run(
    settings: Settings,
    day_text: str | None,
    deliver: bool,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[str, str, int]:
    """``(stdout, stderr, exit code)``: the report on stdout, problems on stderr (so
    ``kelpie report > day.md`` under cron keeps them apart); 2: bad input, 1: the database
    could not be read or the summary did not reach every channel."""
    now = clock()
    try:
        day = date.fromisoformat(day_text) if day_text else default_day(now, settings.report)
    except ValueError:
        return "", f"kelpie: --date must be YYYY-MM-DD, got {day_text!r}\n", 2
    metrics = Metrics()
    board = merged_snapshot(
        build_adapters(report_settings(settings, day, clock())), metrics, clock
    )()
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


def list_fixes(
    settings: Settings, days: int = 7, clock: Callable[[], datetime] = utc_now
) -> tuple[str, int]:
    """``kelpie fix --list``: the fixes recorded in the last ``days`` (manual and config;
    mode changes and filters are found when the report runs), in [report] timezone."""
    if not 0 < days <= MAX_LIST_DAYS:
        return f"kelpie: --days must be between 1 and {MAX_LIST_DAYS}\n", 2
    if settings.db_path is None:
        return "kelpie: fixes need a database ([storage] path is :memory:)\n", 2
    if not settings.db_path.exists():
        return "no fixes recorded (no database yet)\n", 0
    now = clock()
    try:
        with ReportStore(settings.db_path) as store:
            found = store.fixes(now - timedelta(days=days), now + FUTURE_SKEW)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"kelpie: cannot read the database {settings.db_path}: {exc}\n", 1
    if not found:
        return f"no fixes recorded in the last {days} day(s)\n", 0
    tz = settings.report.tz
    lines = []
    for f in found:
        scope = " ".join(
            x for x in (f.rule and f"rule={f.rule}", f.command and f"command={f.command!r}",
                        f.role and f"role={f.role}") if x
        )  # fmt: skip
        when = f.at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        note = " ".join(f.note.split())  # one line per fix
        lines.append(f"{when}  {f.kind:<6}  {note}" + (f"  [{scope}]" if scope else ""))
    return "\n".join(lines) + "\n", 0


def scheduled_run(
    settings: Settings, sender: Sender | None, clock: Callable[[], datetime] = utc_now
) -> Callable[[date], bool]:
    """The run of one day for :class:`~kelpie.report.schedule.ReportScheduler`: generate,
    write to ``[report] dir`` and hand the summary to ``sender`` (``kelpie serve``'s alert
    delivery; None: not sent). False when the database could not be read (retried)."""

    def run_day(day: date) -> bool:
        adapters = build_adapters(report_settings(settings, day, clock()))
        board = merged_snapshot(adapters, Metrics(), clock)()
        try:
            report, markdown = generate(sources(settings), day, board)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            log.error("daily report for %s: cannot read the database: %s", day, exc)
            return False
        path = write(markdown, settings.report, day)
        if path is not None:
            log.info("daily report for %s written to %s", day, path)
        if sender is not None:
            send(report, settings.report, sender)
        elif path is None:
            log.warning("daily report for %s went nowhere: set [report] dir or alerts", day)
        return True

    return run_day


def report_schedule(settings: Settings, sender: Sender | None) -> ReportScheduler | None:
    """The daily report for ``kelpie serve`` when ``[report] at`` is set (not started:
    ``serve`` starts it with the control loop and stops it before the alert delivery)."""
    report = settings.report
    if report.at is None:
        return None
    if settings.db_path is None:
        log.warning("[report] at with an in-memory database: the report has no history")
    if sender is None and report.dir is None:
        log.warning("[report] at without alert channels or [report] dir: reports go nowhere")
    run_day = scheduled_run(settings, sender)
    log.info("daily report scheduled at %s %s", report.at.isoformat("minutes"), report.timezone)
    return ReportScheduler(report, report.at, run_day, RunLog(settings.db_path))
