"""Read what the daily report needs from Kelpie's SQLite database, without writing to it.

The control loop owns the database; the report opens it read-only (no migration, no lock
held beyond a read), like ``kelpie status``. A database older than the current schema
must be upgraded by ``kelpie serve`` first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kelpie.core.incidents import is_outage_state
from kelpie.core.ledger import LedgerEntry
from kelpie.core.model import Snooze, Span
from kelpie.core.spans import Attribution
from kelpie.core.states import OpenState

# The ledger's and state store's own row mappings, so the two cannot drift apart.
from kelpie.core.storage import (
    SCHEMA_VERSION,
    SchemaOutdated,
    SqliteSpanStore,
    _entry,
    _state,
    read_snoozes,
)
from kelpie.report.fixes import FixEvent, read_fixes


@dataclass(frozen=True, slots=True)
class StateRow:
    """A long-lived alert state; ``closed_at`` is None while it is still open."""

    state: OpenState
    closed_at: datetime | None = None


def began(state: OpenState) -> datetime:
    """When a state began, as far as its row shows: its ``since``; an outage's, the earlier
    of that and when it was opened (its ``since`` may have moved forward since)."""
    return min(state.since, state.opened_at) if is_outage_state(state.id) else state.since


def _ts(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="microseconds")


class ReportStore:
    """Ledger entries, alert states and spans for a time range, read-only."""

    def __init__(self, path: Path | str, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(path)
        uri = self._path.resolve().as_uri() + "?mode=ro"  # escapes ?, # and % in the path
        self._db = sqlite3.connect(uri, uri=True)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            self._db.close()
            if version < SCHEMA_VERSION:
                raise SchemaOutdated(
                    f"database schema {version} is older than {SCHEMA_VERSION}; run kelpie "
                    "serve once to upgrade it"
                )
            raise RuntimeError(f"database schema {version} is newer than {SCHEMA_VERSION}")
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
        """States open at ``end`` or closed after ``start``, that began before ``end``. An
        outage's ``since`` may have moved past ``end`` since (rules 4 and 10 move it forward
        as early blocks or failures age out, and an escalation stores it): an outage began
        before ``end`` if it was opened before it too (:func:`began`)."""
        rows = self._db.execute(
            "SELECT * FROM states WHERE (closed_at IS NULL OR closed_at >= ?) ORDER BY since, id",
            (_ts(start),),
        ).fetchall()
        found = [StateRow(_state(r), _time(r["closed_at"])) for r in rows]
        return [r for r in found if began(r.state) < end]

    def spans(self, start: datetime, end: datetime) -> list[Span]:
        """Stored spans whose last time falls in ``[start, end)``."""
        store = self._span_store()
        return [s for s in store.spans_since(start) if _last(s) < end]

    def fixes(self, start: datetime, end: datetime) -> list[FixEvent]:
        return read_fixes(self._db, start, end)

    def snoozes(self, after: datetime) -> list[Snooze]:
        """Stored snoozes that ended after ``after`` (#176)."""
        return read_snoozes(self._db, after)

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
