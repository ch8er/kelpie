"""Read what the daily report needs from Kelpie's SQLite database, without writing to it.

The control loop owns the database; the report opens it read-only (no migration, no lock
held beyond a read), like ``kelpie status``. A database older than the current schema
must be upgraded by ``kelpie serve`` first.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression
from kelpie.core.model import Span
from kelpie.core.spans import Attribution
from kelpie.core.states import OpenState
from kelpie.core.storage import SqliteSpanStore


@dataclass(frozen=True, slots=True)
class StateRow:
    """A long-lived alert state; ``closed_at`` is None while it is still open."""

    state: OpenState
    closed_at: datetime | None = None


def _ts(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="microseconds")


class ReportStore:
    """Ledger entries, alert states and spans for a time range, read-only."""

    def __init__(self, path: Path | str, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(path)
        self._db = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._spans: SqliteSpanStore | None = None

    def close(self) -> None:
        self._db.close()
        if self._spans is not None:
            self._spans.close()

    def __enter__(self) -> ReportStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def actions(self, start: datetime, end: datetime) -> list[LedgerEntry]:
        rows = self._db.execute(
            "SELECT * FROM actions WHERE at >= ? AND at < ? ORDER BY at, id",
            (_ts(start), _ts(end)),
        ).fetchall()
        return [_entry(r) for r in rows]

    def states(self, start: datetime, end: datetime) -> list[StateRow]:
        """States open at ``end`` or closed after ``start``, that began before ``end``."""
        rows = self._db.execute(
            "SELECT * FROM states WHERE since < ? AND (closed_at IS NULL OR closed_at >= ?)"
            " ORDER BY since, id",
            (_ts(end), _ts(start)),
        ).fetchall()
        return [StateRow(_state(r), _time(r["closed_at"])) for r in rows]

    def spans(self, start: datetime, end: datetime) -> list[Span]:
        """Stored spans whose last time falls in ``[start, end)``."""
        store = self._span_store()
        return [s for s in store.spans_since(start) if _last(s) < end]

    def attribution(self) -> Attribution:
        return self._span_store().attribution()

    def _span_store(self) -> SqliteSpanStore:
        if self._spans is None:
            self._spans = SqliteSpanStore(self._path, readonly=True)
        return self._spans


def _last(span: Span) -> datetime:
    return span.end if span.end is not None and span.end > span.start else span.start


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        at=datetime.fromisoformat(row["at"]),
        fingerprint=row["fingerprint"],
        subject=row["subject"],
        rule=row["rule"],
        kind=ActionKind(row["kind"]),
        mode=Mode(row["mode"]),
        outcome=Outcome(row["outcome"]),
        reason=row["reason"],
        task_id=row["task_id"],
        executor=row["executor"],
        params=json.loads(row["params"]),
        suppressed=row["suppressed"],
        suppression=Suppression(row["suppression"]) if row["suppression"] else None,
    )


def _state(r: sqlite3.Row) -> OpenState:
    return OpenState(
        id=r["id"],
        rule=r["rule"],
        task_id=r["task_id"],
        since=datetime.fromisoformat(r["since"]),
        opened_at=datetime.fromisoformat(r["opened_at"]),
        last_seen=datetime.fromisoformat(r["last_seen"]),
        level=r["level"],
        severity=Severity(r["severity"]),
        summary=r["summary"],
        key=r["key"],
        mode=Mode(r["mode"]),
    )
