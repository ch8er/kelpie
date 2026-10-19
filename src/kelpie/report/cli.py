"""``kelpie report [--date YYYY-MM-DD] [--send]``: wiring between the app's settings and
:mod:`kelpie.report.run` (imported lazily by :mod:`kelpie.cli`)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta

from kelpie.app import (
    Settings,
    _rule_config,
    act_blockers,
    build_adapters,
    connector_steps,
    hold_labels,
    merged_snapshot,
    read_reconciliations,
    utc_now,
)
from kelpie.core.budgets import BudgetMode
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.model import Snapshot
from kelpie.metrics import Metrics
from kelpie.report.daily import StuckCheck, day_bounds
from kelpie.report.farm import FarmFacts
from kelpie.report.fixes import MANUAL, FixEvent, FixStore
from kelpie.report.run import ReportSources, Sender, default_day, generate, send, write
from kelpie.report.schedule import ReportScheduler, RunLog
from kelpie.report.store import ReportStore
from kelpie.report.waits import BlockKind
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
        # [prices] over the default table; each provider's spend mode decides dollars or
        # tokens against its plan's limit
        prices=settings.budgets.prices,
        spend=settings.spend,
        outputs=settings.traces.outputs,
        estimated=any(b.mode is BudgetMode.SUBSCRIPTION for b in settings.budgets.budgets),
        modes=modes,
        hold_labels=tuple(hold_labels(engine.rules.get("bottlenecks", {}))),
        block_kinds=tuple(
            BlockKind(b.pattern, b.condition_kind, b.repo)
            for b in settings.block_conditions
            if "block" in b.applies_to
        ),
        stuck_check=_stuck_check(engine),
        # What keeps a rule from acting here: never recommend a config kelpie serve refuses.
        act_blockers={
            rule: b.reason for rule, b in act_blockers(settings, build_adapters(settings)).items()
        },
        farm=farm_facts(settings),
        reconcile=lambda since, until: read_reconciliations(settings, since, until),
    )


def farm_facts(settings: Settings) -> FarmFacts:
    """What the farm already has, from its config: the report never advises it."""
    rules = settings.engine.rules
    needs_you = rules.get("needs_you", {})
    approvers = needs_you.get("approvers")
    bottlenecks = rules.get("bottlenecks", {})
    holds = [h for h in bottlenecks.get("holds", []) if isinstance(h, Mapping)]
    held = {h.get("label") for h in holds}
    labels = bottlenecks.get("hold_labels", [])
    bare = {x for x in labels if isinstance(x, str)} - held if isinstance(labels, list) else set()
    return FarmFacts(
        approvers=bool(needs_you.get("default_approver")),
        approver_roles=frozenset(approvers) if isinstance(approvers, Mapping) else frozenset(),
        # holds entries expire (their own or the 4h default); bare labels only with hold_expiry
        hold_expiry=bool(holds or bare) and (not bare or "hold_expiry" in bottlenecks),
        hold_owners=bool(holds or bare) and not bare and all(h.get("owner") for h in holds),
        block_conditions=any("block" in p.applies_to for p in settings.block_conditions),
        budgets=bool(settings.budgets.budgets),
        dry_run=settings.engine.dry_run,
    )


def _stuck_check(engine: EngineConfig) -> StuckCheck | None:
    """Rule 3's stuck-before-work test, built from its config as ``kelpie serve`` builds it
    (hold labels inherited from rule 6 included); None (its defaults) if the config is
    invalid, which ``kelpie serve`` refuses at startup."""
    try:
        rule = REGISTRY["orphan_status"](_rule_config("orphan_status", engine))
    except (TypeError, ValueError):
        return None
    return getattr(rule, "stuck_before_work", None)


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


def report_board(
    settings: Settings, day: date, clock: Callable[[], datetime], metrics: Metrics
) -> Snapshot:
    """The board the report of ``day`` reads: the adapters, looking back as far as the day
    needs, with the connector steps that read nothing but the board (``task_status``, human
    tasks from block reasons), so the report knows what more waiting tasks wait for. Like
    ``kelpie why`` without ``--refresh``, the report calls no network service of its own:
    a block reason ``[[block_conditions]]`` matches counts as a CI or merge wait without
    a PR lookup (:class:`kelpie.report.waits.BlockKind`)."""
    enrich, _ = connector_steps(settings, metrics, clock, network=False)
    adapters = build_adapters(report_settings(settings, day, clock()))
    return merged_snapshot(adapters, metrics, clock, enrich=enrich)()


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
    board = report_board(settings, day, clock, metrics)
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
        board = report_board(settings, day, clock, Metrics())
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
