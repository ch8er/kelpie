"""The daily report inside ``kelpie serve``: ``[report] at = "00:15"`` runs yesterday's report
once a day at that local time (``[report] timezone``), writes it to ``[report] dir`` and sends
its summary through the alert channels ``kelpie serve`` already uses.

- **Once per day.** Each completed run is recorded in ``report_runs`` (Kelpie's database,
  created on first write), so a restart does not send the same day twice. Without a
  database (``:memory:``) runs are remembered in memory.
- **Catch-up.** If ``kelpie serve`` was down at ``at``, yesterday's report runs as soon as
  it is back the same day; days before that are not caught up (run ``kelpie report --date``).
- **Failures.** A run that cannot read the database is retried every ``RETRY_AFTER``, at
  most ``MAX_ATTEMPTS`` times a day; everything is logged, nothing raises into ``serve``. A
  run log that cannot be read skips the tick rather than risk a second send.
- **Done** means generated, written and handed to the alert delivery; a channel that
  fails later is retried and counted by the delivery layer, not by the schedule. A run
  interrupted by shutdown is not recorded and runs again after the restart.
- **One serve per database**, as everywhere in Kelpie: two processes could both send.

``kelpie report`` stays as it is; cron is optional. A day run by hand is not recorded, so
the schedule still sends it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time as clock_time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from kelpie.report.config import ReportSettings

log = logging.getLogger(__name__)

CHECK_EVERY = timedelta(seconds=60)
RETRY_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 3
_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_runs (
    day     TEXT PRIMARY KEY,
    done_at TEXT NOT NULL
);
"""

RunDay = Callable[[date], bool]  # True: the day's report was generated (and handed over)


class RunLog:
    """Which days the schedule already reported."""

    def __init__(self, db_path: Path | None) -> None:
        self._path = db_path
        self._memory: set[date] = set()

    def done(self, day: date) -> bool | None:
        """True or False; None when the log cannot be read (skip: never risk a second send)."""
        if day in self._memory:
            return True
        if self._path is None or not self._path.exists():
            return False
        try:
            with closing(sqlite3.connect(self._path)) as db, db:
                db.executescript(_SCHEMA)
                row = db.execute(
                    "SELECT 1 FROM report_runs WHERE day = ?", (day.isoformat(),)
                ).fetchone()
        except (sqlite3.Error, OSError) as exc:
            log.warning("cannot read report runs: %s", exc)
            return None
        if row is not None:
            self._memory.add(day)  # no need to ask the database again today
        return row is not None

    def mark(self, day: date, at: datetime) -> None:
        self._memory.add(day)
        if self._path is None:
            return
        try:
            with closing(sqlite3.connect(self._path)) as db, db:
                db.executescript(_SCHEMA)
                db.execute(
                    "INSERT OR REPLACE INTO report_runs (day, done_at) VALUES (?, ?)",
                    (day.isoformat(), at.astimezone(UTC).isoformat(timespec="seconds")),
                )
        except (sqlite3.Error, OSError) as exc:  # remembered in memory: no resend in this process
            log.warning("cannot record the report run of %s: %s", day, exc)


@dataclass(slots=True)
class ReportScheduler:
    settings: ReportSettings
    at: time
    run_day: RunDay
    runs: RunLog
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    monotonic: Callable[[], float] = clock_time.monotonic
    _attempts: dict[date, tuple[int, float]] = field(default_factory=dict)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def due(self, now: datetime) -> date | None:
        """Yesterday, once the local time is past ``at`` and it has not been reported."""
        local = now.astimezone(self.settings.tz)
        if local.time() < self.at:
            return None
        day = local.date() - timedelta(days=1)
        if self.runs.done(day) is not False:  # done, or unknown: do not risk a second send
            return None
        attempts, last = self._attempts.get(day, (0, None))
        if attempts >= MAX_ATTEMPTS:
            return None
        if last is not None and self.monotonic() - last < RETRY_AFTER.total_seconds():
            return None
        return day

    def tick(self) -> date | None:
        """Run the report if one is due; returns the day it ran, if any."""
        now = self.clock()
        day = self.due(now)
        if day is None:
            return None
        attempts = self._attempts.get(day, (0, None))[0] + 1
        self._attempts[day] = (attempts, self.monotonic())
        try:
            ok = self.run_day(day)
        except Exception:
            log.exception("scheduled report for %s failed", day)
            ok = False
        if ok and self._stop.is_set():
            # shutting down: the summary may not have left before delivery closed
            log.warning("daily report for %s interrupted by shutdown; it runs again", day)
            return None
        if ok:
            self.runs.mark(day, now)
            log.info("daily report for %s done", day)
        elif attempts >= MAX_ATTEMPTS:
            log.error("daily report for %s failed %d times; giving up for today", day, attempts)
        return day if ok else None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="kelpie-report", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let one bad tick end the schedule
                log.exception("daily report schedule failed; trying again")
            self._stop.wait(CHECK_EVERY.total_seconds())
