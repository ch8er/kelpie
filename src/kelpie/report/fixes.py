"""Fix events: what changed on the farm and when, so the report can verify the effect.

Three sources write here (table ``report_fixes`` in Kelpie's database, created on first
write; the report reads it read-only and treats a missing table as no events):

- ``kelpie fix "<note>" [--rule R] [--command PREFIX] [--role ROLE] [--at TIME]``: a fix an
  operator made by hand (a merge hold lifted, a role's instructions changed);
- ``kelpie serve`` at startup, when the config file's content changed since the last start
  (``config``);
- nothing else writes: rule mode changes are read from the action ledger and filter
  introductions from traces when the report runs (:mod:`kelpie.report.verify`).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

MANUAL, CONFIG, MODE, FILTER = "manual", "config", "mode", "filter"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_fixes (
    id      INTEGER PRIMARY KEY,
    at      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    note    TEXT NOT NULL,
    rule    TEXT,
    command TEXT,
    role    TEXT,
    digest  TEXT
);
CREATE INDEX IF NOT EXISTS report_fixes_at ON report_fixes (at);
"""
MAX_NOTE = 500


@dataclass(frozen=True, slots=True)
class FixEvent:
    """A change whose effect the report verifies. ``rule``, ``command`` (a command prefix,
    see :mod:`kelpie.traces.command`) and ``role`` narrow what it is expected to change."""

    at: datetime
    kind: str
    note: str
    rule: str | None = None
    command: str | None = None
    role: str | None = None


def _ts(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("fix times must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


class FixStore:
    """Writer for ``kelpie fix`` and ``kelpie serve``; one short transaction per call."""

    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> FixStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add(self, event: FixEvent, digest: str | None = None) -> None:
        note = event.note.strip()[:MAX_NOTE]
        if not note:
            raise ValueError("a fix needs a note")
        with self._db:
            self._db.execute(
                "INSERT INTO report_fixes (at, kind, note, rule, command, role, digest)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_ts(event.at), event.kind, note, event.rule, event.command, event.role, digest),
            )

    def record_config(self, content: bytes, at: datetime) -> bool:
        """Add a ``config`` event when ``content`` differs from the last recorded config;
        the first config ever seen is the baseline, not a change. Returns True if added."""
        digest = hashlib.sha256(content).hexdigest()[:16]
        row = self._db.execute(
            "SELECT digest FROM report_fixes WHERE kind = ? ORDER BY at DESC, id DESC LIMIT 1",
            (CONFIG,),
        ).fetchone()
        if row is not None and row[0] == digest:
            return False
        note = "first config seen" if row is None else "config changed"
        self.add(FixEvent(at, CONFIG, note), digest)
        return row is not None


def read_fixes(db: sqlite3.Connection, start: datetime, end: datetime) -> list[FixEvent]:
    """Fix events in ``[start, end)`` from an open (read-only) connection; config
    baselines are left out."""
    try:
        rows = db.execute(
            "SELECT at, kind, note, rule, command, role FROM report_fixes"
            " WHERE at >= ? AND at < ? ORDER BY at, id",
            (_ts(start), _ts(end)),
        ).fetchall()
    except sqlite3.OperationalError:  # no fix was ever recorded: no table
        return []
    return [
        FixEvent(datetime.fromisoformat(r[0]), r[1], r[2], r[3], r[4], r[5])
        for r in rows
        if not (r[1] == CONFIG and r[2] == "first config seen")
    ]


def record_config_change(db_path: Path, config: Path, at: datetime) -> bool:
    """Called by ``kelpie serve`` at startup: a ``config`` event when the config file's
    content changed since the last start. Never raises (logs instead): a missing record
    must not stop the control loop."""
    try:
        content = config.read_bytes()
        with FixStore(db_path) as store:
            return store.record_config(content, at)
    except (OSError, sqlite3.Error) as exc:
        log.warning("cannot record a config change for the daily report: %s", exc)
        return False
