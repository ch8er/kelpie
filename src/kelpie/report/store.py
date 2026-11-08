"""Read what the daily report needs from Kelpie's SQLite database, without writing to it.

The control loop owns the database; the report opens it read-only (no migration, no lock
held beyond a read), like ``kelpie status``. A database older than the current schema
must be upgraded by ``kelpie serve`` first.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.core.incidents import is_outage_state
from kelpie.core.ledger import LedgerEntry
from kelpie.core.model import HostSleep, Snooze, Span
from kelpie.core.spans import Attribution
from kelpie.core.states import OpenState

# The ledger's and state store's own row mappings, so the two cannot drift apart.
from kelpie.core.storage import (
    SCHEMA_VERSION,
    ActionDay,
    SchemaOutdated,
    SqliteSpanStore,
    TokenRow,
    _entry,
    _state,
    read_action_days,
    read_host_sleeps,
    read_instruction_tokens,
    read_snoozes,
    read_transitions,
    read_watches,
)
from kelpie.core.transitions import StatusChange
from kelpie.report.fixes import FixEvent, read_fixes


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

    def action_days(self, start: datetime, end: datetime) -> list[ActionDay]:
        """The ledger's daily sums (#318) of the UTC days ``[start, end)`` touches: what
        is left of days whose raw rows retention pruned."""
        last = (end - timedelta(microseconds=1)).astimezone(UTC).date()
        return read_action_days(self._db, start.astimezone(UTC).date(), last)

    def states(self, start: datetime, end: datetime) -> list[StateRow]:
        """States open at ``end`` or closed after ``start``, that began before ``end``. An
        outage's row cannot tell when it began (rules 4 and 10 move its ``since`` forward and
        an escalation stores it; a check switched on re-opens it in place): outages load
        whatever their ``since``, and their alerts before ``end`` decide what the report
        shows of them (none: nothing)."""
        rows = self._db.execute(
            "SELECT * FROM states WHERE (closed_at IS NULL OR closed_at >= ?) ORDER BY since, id",
            (_ts(start),),
        ).fetchall()
        found = [StateRow(_state(r), _time(r["closed_at"])) for r in rows]
        return [r for r in found if r.state.since < end or is_outage_state(r.state.id)]

    def spans(self, start: datetime, end: datetime) -> list[Span]:
        """Stored spans whose last time falls in ``[start, end)``."""
        store = self._span_store()
        return [s for s in store.spans_since(start) if _last(s) < end]

    def fixes(self, start: datetime, end: datetime) -> list[FixEvent]:
        return read_fixes(self._db, start, end)

    def instruction_tokens(self, start: datetime, end: datetime) -> list[TokenRow]:
        """Tokens a day on instructions per role, sampled per scan ([instructions])."""
        return read_instruction_tokens(self._db, start, end)

    def host_sleeps(self, start: datetime, end: datetime) -> list[HostSleep]:
        """Stretches the farm's hosts slept that overlap ``[start, end)`` (issue #296)."""
        return read_host_sleeps(self._db, start, end)

    def snoozes(self, after: datetime) -> list[Snooze]:
        """Stored snoozes that ended after ``after`` (#176)."""
        return read_snoozes(self._db, after)

    def transitions(self, start: datetime, end: datetime) -> list[StatusChange]:
        """Task status changes Kelpie recorded (#157) in ``[start, end)``, plus each task's
        last one before ``start``."""
        return read_transitions(self._db, start, end)

    def watched(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        """When Kelpie's transition recorder watched the boards, overlapping the range."""
        return read_watches(self._db, start, end)

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
