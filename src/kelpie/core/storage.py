"""SQLite persistence: the action ledger, long-lived alert states, snoozes and trace spans.

The database runs in WAL mode so readers (``kelpie status``, the report) do not block the
control loop. The ledger store shares one connection across threads behind a lock. Each
insert commits on its own: the ledger writes a handful of rows per tick, and losing one
would let an action repeat.

Spans live in the same file but behind their own connection and lock
(:class:`SqliteSpanStore`), so OTLP ingest on HTTP threads never waits for the ledger lock
and the other way round. Span writes and deletes run in short batches, so the one SQLite
writer slot is never held for long.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple, cast

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression, to_json
from kelpie.core.model import ReportedCost, Snooze, SnoozeSource, Span, SpanKind, SpanTotals
from kelpie.core.snooze import EXPIRED_KEPT, MAX_BY, MAX_REASON, PRUNE_AFTER
from kelpie.core.spans import (
    MAX_LINK_DEPTH,
    Attribution,
    SpanChanges,
    TokenUsage,
    attribution_stale,
    counted_llm,
    has_usage,
    last_at,
)
from kelpie.core.states import OpenState

_V1 = """
CREATE TABLE actions (
    id          INTEGER PRIMARY KEY,
    at          TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    subject     TEXT NOT NULL,
    rule        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    mode        TEXT NOT NULL,
    outcome     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    task_id     TEXT,
    executor    TEXT,
    params      TEXT NOT NULL,
    suppressed  INTEGER NOT NULL DEFAULT 0,
    suppression TEXT
);
CREATE INDEX actions_at ON actions (at);
CREATE INDEX actions_fingerprint ON actions (fingerprint, at);
CREATE INDEX actions_subject ON actions (subject, at);
"""

_V2 = """
CREATE TABLE states (
    rowid_     INTEGER PRIMARY KEY,
    id         TEXT NOT NULL,
    rule       TEXT NOT NULL,
    task_id    TEXT,
    since      TEXT NOT NULL,
    opened_at  TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    level      INTEGER NOT NULL,
    severity   TEXT NOT NULL,
    summary    TEXT NOT NULL,
    key        TEXT,
    mode       TEXT NOT NULL,
    closed_at  TEXT
);
CREATE UNIQUE INDEX states_open ON states (id) WHERE closed_at IS NULL;
CREATE INDEX states_closed ON states (closed_at);
"""

_V3 = """
CREATE TABLE spans (
    rowid_        INTEGER PRIMARY KEY,
    trace_id      TEXT NOT NULL,
    span_id       TEXT NOT NULL,
    parent_id     TEXT,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    start         TEXT NOT NULL,
    end_at        TEXT,
    last_at       TEXT NOT NULL,
    task_id       TEXT,
    session_id    TEXT,
    model         TEXT,
    tool          TEXT,
    args_hash     TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0,
    outcome       TEXT,
    attributes    TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    version       INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX spans_id ON spans (trace_id, span_id);
CREATE INDEX spans_last ON spans (last_at);
CREATE INDEX spans_version ON spans (version);
CREATE INDEX spans_session ON spans (session_id, start);
CREATE INDEX spans_task ON spans (task_id, start);
"""

# Counted tokens (kelpie.core.spans.counted_llm), decided per trace when spans are stored:
# NULL until decided (rows of older databases are decided when the store opens). The
# partial covering indexes make budget usage index-only scans of counted rows. Attribution keeps
# session owners and task labels after adapters stop reporting them.
_V4 = """
ALTER TABLE spans ADD COLUMN counted INTEGER;
ALTER TABLE spans ADD COLUMN price_model TEXT;
CREATE INDEX spans_counted ON spans
    (last_at, session_id, task_id, price_model, input_tokens, output_tokens, cached_tokens)
    WHERE counted = 1;
CREATE INDEX spans_counted_by_subject ON spans
    (session_id, task_id, price_model, last_at, input_tokens, output_tokens, cached_tokens)
    WHERE counted = 1;
CREATE INDEX spans_undecided ON spans (trace_id) WHERE counted IS NULL;
CREATE TABLE span_sessions (
    session_id TEXT PRIMARY KEY,
    owner_id   TEXT NOT NULL,
    task_id    TEXT,
    seen_at    TEXT NOT NULL
);
CREATE INDEX span_sessions_seen ON span_sessions (seen_at);
CREATE TABLE span_task_labels (
    task_id TEXT PRIMARY KEY,
    labels  TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE INDEX span_task_labels_seen ON span_task_labels (seen_at);
"""

# What an executor reported it did, or why it failed (Dispatched.detail).
_V5 = """
ALTER TABLE actions ADD COLUMN detail TEXT;
"""

# Cache writes priced on their own (kelpie.core.prices): a column, filled for stored spans
# from the attribute OTLP ingest kept them in, and the covering indexes rebuilt with it.
# Frozen with migration 6 (a released migration never changes): not kelpie.traces.semconv's
# list, which may grow. A span store of a million spans takes some seconds to upgrade.
_V6_CACHE_WRITE_KEYS = (
    "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.usage.cache_write.input_tokens",
    "gen_ai.usage.cache_creation_input_tokens",
    "cache_creation_tokens",
)
_WRITTEN = ", ".join(f"json_extract(attributes, '$.\"{k}\"')" for k in _V6_CACHE_WRITE_KEYS)
_V6 = f"""
ALTER TABLE spans ADD COLUMN cache_write_tokens INTEGER NOT NULL DEFAULT 0;
UPDATE spans SET cache_write_tokens = MAX(0, MIN(input_tokens - MIN(cached_tokens, input_tokens),
    CAST(COALESCE({_WRITTEN}, 0) AS INTEGER)))
    WHERE kind = 'llm' AND (attributes LIKE '%cache_creation%' OR attributes LIKE '%cache_write%')
    AND json_valid(attributes);
DROP INDEX spans_counted;
DROP INDEX spans_counted_by_subject;
CREATE INDEX spans_counted ON spans
    (last_at, session_id, task_id, price_model, input_tokens, output_tokens, cached_tokens,
     cache_write_tokens)
    WHERE counted = 1;
CREATE INDEX spans_counted_by_subject ON spans
    (session_id, task_id, price_model, last_at, input_tokens, output_tokens, cached_tokens,
     cache_write_tokens)
    WHERE counted = 1;
"""

# Providers' daily cost reports (kelpie.connectors.cost_reports): the bill per provider,
# UTC day and model ('' for the rest of the day's figure), as last read.
_V7 = """
CREATE TABLE cost_reports (
    provider   TEXT NOT NULL,
    day        TEXT NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    usd        REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (provider, day, model)
);
"""

# Snoozes (``kelpie snooze``, chat replies): one per task, a new one replaces the old.
# Markers in block reasons are read from the board and never stored here.
_V8 = """
CREATE TABLE snoozes (
    task_id          TEXT PRIMARY KEY,
    until_at         TEXT NOT NULL,
    reason           TEXT,
    snoozed_by       TEXT,
    created_at       TEXT NOT NULL,
    source           TEXT NOT NULL,
    behind_at_snooze INTEGER
);
CREATE INDEX snoozes_until ON snoozes (until_at);
"""

# A snooze's latched growth (#176): when kelpie serve first saw more work behind its task
# than its baseline, and how much more. Written once, like the baseline. The baselines
# stored before were counted by ``kelpie snooze`` on its offline board, without the
# connectors' conditions: too low, they would latch false growth on the first tick, so
# they are dropped and serve counts them again at its first complete tick.
_V9 = """
ALTER TABLE snoozes ADD COLUMN grown_at TEXT;
ALTER TABLE snoozes ADD COLUMN grown_by INTEGER;
UPDATE snoozes SET behind_at_snooze = NULL WHERE source <> 'marker';
"""
SNOOZES_SINCE = 8  # the first schema with snoozes: read-only readers accept it (no growth)

# version -> script that upgrades from version - 1. Append, never edit a released one.
MIGRATIONS: dict[int, str] = {
    1: _V1, 2: _V2, 3: _V3, 4: _V4, 5: _V5, 6: _V6, 7: _V7, 8: _V8, 9: _V9,
}  # fmt: skip
SCHEMA_VERSION = max(MIGRATIONS)

_COLUMNS = (
    "at, fingerprint, subject, rule, kind, mode, outcome, reason, task_id, executor,"
    " params, suppressed, suppression, detail"
)


def _ts(at: datetime) -> str:
    """Fixed-width UTC timestamp, so string order equals time order."""
    if at.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def ledger_entry(row: sqlite3.Row) -> LedgerEntry:
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
        detail=row["detail"],
    )


def _open(path: Path | str, busy_timeout_ms: int) -> sqlite3.Connection:
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    db.execute("PRAGMA journal_mode = WAL")
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise RuntimeError(f"database schema {version} is newer than {SCHEMA_VERSION}")
    for target in range(version + 1, SCHEMA_VERSION + 1):
        # executescript commits any open transaction first, so wrap the script and the
        # version bump in one explicit transaction: a failed migration leaves no trace.
        try:
            db.executescript(
                f"BEGIN;\n{MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;"
            )
        except sqlite3.Error:
            if db.in_transaction:
                db.rollback()
            raise


class SqliteLedgerStore:
    def __init__(self, path: Path | str, busy_timeout_ms: int = 5000) -> None:
        self._lock = threading.Lock()
        self._db = _open(path, busy_timeout_ms)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _query(self, sql: str, args: tuple[object, ...]) -> list[LedgerEntry]:
        with self._lock:
            return [ledger_entry(r) for r in self._db.execute(sql, args).fetchall()]

    def _write(self, fn: Callable[[sqlite3.Connection], sqlite3.Cursor]) -> sqlite3.Cursor:
        with self._lock, self._db:
            return fn(self._db)

    def append(self, entry: LedgerEntry) -> None:
        self._write(lambda db: _insert_entry(db, entry))

    def by_fingerprint(self, fingerprint: str, after: datetime) -> Sequence[LedgerEntry]:
        sql = f"SELECT {_COLUMNS} FROM actions WHERE fingerprint = ? AND at > ? ORDER BY at, id"
        return self._query(sql, (fingerprint, _ts(after)))

    def by_subject(self, subject: str, after: datetime) -> Sequence[LedgerEntry]:
        sql = f"SELECT {_COLUMNS} FROM actions WHERE subject = ? AND at > ? ORDER BY at, id"
        return self._query(sql, (subject, _ts(after)))

    def bump_suppressed(self, fingerprint: str, mode: Mode, after: datetime) -> bool:
        sql = (
            "UPDATE actions SET suppressed = suppressed + 1 WHERE id = ("
            "SELECT id FROM actions WHERE fingerprint = ? AND mode = ? AND at > ?"
            " ORDER BY at DESC, id DESC LIMIT 1)"
        )
        args = (fingerprint, str(mode), _ts(after))
        return self._write(lambda db: db.execute(sql, args)).rowcount > 0

    def since(self, after: datetime) -> Sequence[LedgerEntry]:
        sql = f"SELECT {_COLUMNS} FROM actions WHERE at > ? ORDER BY at, id"
        return self._query(sql, (_ts(after),))

    # --- long-lived alert states (kelpie.core.states.StateStore) ------------------------

    _STATE_COLUMNS = (
        "id, rule, task_id, since, opened_at, last_seen, level, severity, summary, key, mode"
    )

    def open_states(self) -> Sequence[OpenState]:
        sql = f"SELECT {self._STATE_COLUMNS} FROM states WHERE closed_at IS NULL"
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY opened_at, rowid_").fetchall()
        return [open_state(r) for r in rows]

    def get_state(self, state_id: str) -> OpenState | None:
        sql = f"SELECT {self._STATE_COLUMNS} FROM states WHERE id = ? AND closed_at IS NULL"
        with self._lock:
            row = self._db.execute(sql, (state_id,)).fetchone()
        return None if row is None else open_state(row)

    def save(self, state: OpenState) -> None:
        values = (
            state.rule,
            state.task_id,
            _ts(state.since),
            _ts(state.opened_at),
            _ts(state.last_seen),
            state.level,
            str(state.severity),
            state.summary,
            state.key,
            str(state.mode),
        )

        def write(db: sqlite3.Connection) -> sqlite3.Cursor:
            cur = db.execute(
                "UPDATE states SET rule = ?, task_id = ?, since = ?, opened_at = ?,"
                " last_seen = ?, level = ?, severity = ?, summary = ?, key = ?, mode = ?"
                " WHERE id = ? AND closed_at IS NULL",
                (*values, state.id),
            )
            if cur.rowcount == 0:
                cur = db.execute(
                    "INSERT INTO states (rule, task_id, since, opened_at, last_seen, level,"
                    " severity, summary, key, mode, id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, state.id),
                )
            return cur

        self._write(write)

    def close_state(self, state_id: str, at: datetime) -> None:
        self._write(
            lambda db: db.execute(
                "UPDATE states SET closed_at = ? WHERE id = ? AND closed_at IS NULL",
                (_ts(at), state_id),
            )
        )

    # --- snoozes (kelpie snooze, chat) ---------------------------------------------------

    def put_snooze(self, snooze: Snooze) -> None:
        """Store ``snooze``, replacing the task's previous one. Markers are never stored."""
        self._write(lambda db: _insert_snooze(db, snooze))

    def put_chat_snooze(self, snooze: Snooze, entry: LedgerEntry) -> Snooze | None:
        """Store a snooze set from a chat (#176) and the ledger entry that records it, in one
        transaction: both or neither. Unless the task has a snooze from another source
        (``kelpie snooze``) that still holds at ``snooze.created_at`` (not expired, not
        grown) and ends later: then nothing is written and that snooze is returned."""
        with self._lock, self._db:
            row = self._db.execute(
                f"SELECT {_SNOOZE_COLUMNS} FROM snoozes WHERE task_id = ?", (snooze.task_id,)
            ).fetchone()
            if row is not None:
                held = snooze_row(row)
                if (
                    held.source != snooze.source
                    and held.grown_at is None
                    and held.until > snooze.created_at
                    and held.until > snooze.until
                ):
                    return held
            _insert_snooze(self._db, snooze)
            _insert_entry(self._db, entry)
        return None

    def set_snooze_baseline(self, snooze: Snooze, behind: int) -> bool:
        """Record how much work was behind a stored snooze's task when ``kelpie serve`` first
        saw it (``behind_at_snooze``), once: only on that same snooze (task and creation
        time) while it has none, so a snooze replaced meanwhile keeps its own. False when
        nothing was written."""
        sql = (
            "UPDATE snoozes SET behind_at_snooze = ? WHERE task_id = ? AND created_at = ?"
            " AND behind_at_snooze IS NULL"
        )
        args = (behind, snooze.task_id, _ts(snooze.created_at))
        return self._write(lambda db: db.execute(sql, args)).rowcount > 0

    def set_snooze_growth(self, snooze: Snooze, at: datetime, grown_by: int) -> bool:
        """Latch a stored snooze's growth (``grown_at``, ``grown_by``), once, on that same
        snooze (task and creation time): later counts never undo it. False when nothing was
        written."""
        sql = (
            "UPDATE snoozes SET grown_at = ?, grown_by = ? WHERE task_id = ? AND created_at = ?"
            " AND grown_at IS NULL"
        )
        args = (_ts(at), grown_by, snooze.task_id, _ts(snooze.created_at))
        return self._write(lambda db: db.execute(sql, args)).rowcount > 0

    def delete_snooze(self, task_id: str) -> bool:
        """Remove the task's snooze; False when it had none."""
        sql = "DELETE FROM snoozes WHERE task_id = ?"
        return self._write(lambda db: db.execute(sql, (task_id,))).rowcount > 0

    def snoozes(self, after: datetime | None = None) -> list[Snooze]:
        """Stored snoozes whose ``until`` is after ``after`` (None: all), soonest first."""
        with self._lock:
            return read_snoozes(self._db, after)

    def load_snoozes(self, now: datetime) -> dict[str, Snooze]:
        """What a snapshot holds: active snoozes and those expired within ``EXPIRED_KEPT``."""
        with self._lock:
            return load_snoozes(self._db, now)

    def prune_snoozes(self, now: datetime) -> int:
        """Delete snoozes that expired more than ``PRUNE_AFTER`` before ``now``."""
        sql = "DELETE FROM snoozes WHERE until_at < ?"
        return self._write(lambda db: db.execute(sql, (_ts(now - PRUNE_AFTER),))).rowcount


_SNOOZE_COLUMNS = (
    "task_id, until_at, reason, snoozed_by, created_at, source, behind_at_snooze, grown_at,"
    " grown_by"
)


def _insert_snooze(db: sqlite3.Connection, snooze: Snooze) -> sqlite3.Cursor:
    """Store ``snooze``, replacing the task's previous one. Markers are never stored."""
    if snooze.source == "marker":
        raise ValueError("snooze markers are read from the board, not stored")
    values = (
        snooze.task_id,
        _ts(snooze.until),
        None if snooze.reason is None else snooze.reason.strip()[:MAX_REASON] or None,
        None if snooze.by is None else snooze.by.strip()[:MAX_BY] or None,
        _ts(snooze.created_at),
        snooze.source,
        snooze.behind_at_snooze,
        None if snooze.grown_at is None else _ts(snooze.grown_at),
        snooze.grown_by,
    )
    return db.execute(
        f"INSERT OR REPLACE INTO snoozes ({_SNOOZE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        values,
    )


def _insert_entry(db: sqlite3.Connection, entry: LedgerEntry) -> sqlite3.Cursor:
    """Append ``entry`` to the action ledger."""
    values = (
        _ts(entry.at),
        entry.fingerprint,
        entry.subject,
        entry.rule,
        entry.kind,
        entry.mode,
        entry.outcome,
        entry.reason,
        entry.task_id,
        entry.executor,
        to_json(entry.params),
        entry.suppressed,
        entry.suppression,
        entry.detail,
    )
    sql = f"INSERT INTO actions ({_COLUMNS}) VALUES ({', '.join('?' * len(values))})"
    return db.execute(sql, values)


def snooze_row(r: sqlite3.Row) -> Snooze:
    # Schema 8, read only: as the v9 upgrade will make it (no offline baseline, no growth).
    if "grown_at" not in r.keys():  # noqa: SIM118 (a Row iterates its values, not its keys)
        return Snooze(
            task_id=r["task_id"],
            until=datetime.fromisoformat(r["until_at"]),
            reason=r["reason"],
            by=r["snoozed_by"],
            created_at=datetime.fromisoformat(r["created_at"]),
            source=cast(SnoozeSource, r["source"]),
        )
    return Snooze(
        task_id=r["task_id"],
        until=datetime.fromisoformat(r["until_at"]),
        reason=r["reason"],
        by=r["snoozed_by"],
        created_at=datetime.fromisoformat(r["created_at"]),
        source=cast(SnoozeSource, r["source"]),
        behind_at_snooze=r["behind_at_snooze"],
        grown_at=None if r["grown_at"] is None else datetime.fromisoformat(r["grown_at"]),
        grown_by=r["grown_by"],
    )


def read_snoozes(db: sqlite3.Connection, after: datetime | None = None) -> list[Snooze]:
    """Stored snoozes ending after ``after`` (None: all), soonest first; works on a
    read-only connection (:func:`open_readonly`, ``kelpie why``)."""
    have = {row[1] for row in db.execute("PRAGMA table_info(snoozes)")}
    columns = ", ".join(c.strip() for c in _SNOOZE_COLUMNS.split(",") if c.strip() in have)
    sql = f"SELECT {columns} FROM snoozes"
    args: tuple[object, ...] = ()
    if after is not None:
        sql += " WHERE until_at > ?"
        args = (_ts(after),)
    rows = db.execute(sql + " ORDER BY until_at, task_id", args).fetchall()
    return [snooze_row(r) for r in rows]


def load_snoozes(db: sqlite3.Connection, now: datetime) -> dict[str, Snooze]:
    """The stored snoozes a snapshot at ``now`` holds, by task id."""
    return {s.task_id: s for s in read_snoozes(db, now - EXPIRED_KEPT)}


def open_state(r: sqlite3.Row) -> OpenState:
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


# --- trace spans (kelpie.core.spans.SpanStore) --------------------------------------------

_SPAN_COLUMNS = (
    "trace_id, span_id, parent_id, name, kind, start, end_at, last_at, task_id, session_id,"
    " model, tool, args_hash, input_tokens, output_tokens, cached_tokens, outcome, attributes,"
    " received_at, version, counted, cache_write_tokens"
)


# Budget windows up to this long read counted rows by time; longer ones by subject.
SHORT_WINDOW = timedelta(hours=6)


def _usage_key(u: TokenUsage) -> tuple[str, str, str]:
    return (u.session_id or "", u.task_id or "", u.model or "")


class SchemaOutdated(RuntimeError):
    """A read-only open found a database an older Kelpie wrote; ``kelpie serve`` upgrades
    it."""


class _Node(NamedTuple):
    """The columns :func:`kelpie.core.spans.counted_llm` reads, plus the stored decision."""

    trace_id: str
    span_id: str
    parent_id: str | None
    kind: SpanKind
    model: str | None
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    rowid: int
    counted: int | None
    price_model: str | None


_NODE_COLUMNS = (
    "trace_id, span_id, parent_id, kind, model, input_tokens, output_tokens, cached_tokens,"
    " rowid_, counted, price_model"
)


# Rows per write or delete transaction: keeps the SQLite writer slot free for the ledger.
SPAN_BATCH = 500


def _span_row(span: Span, received: str, version: int) -> tuple[object, ...]:
    return (
        span.trace_id,
        span.span_id,
        span.parent_id,
        span.name,
        str(span.kind),
        _ts(span.start),
        None if span.end is None else _ts(span.end),
        _ts(last_at(span)),
        span.task_id,
        span.session_id,
        span.model,
        span.tool,
        span.args_hash,
        span.input_tokens,
        span.output_tokens,
        span.cached_tokens,
        span.outcome,
        json.dumps(span.attributes, sort_keys=True, separators=(",", ":"), default=str),
        received,
        version,
        # Only an LLM span with usage can count, and only its trace can tell: undecided
        # (NULL) until _decide; any other span is decided now and needs no update.
        None if has_usage(span) else 0,
        span.cache_write_tokens,
    )


def _span(r: sqlite3.Row) -> Span:
    attributes = json.loads(r["attributes"])
    return Span(
        trace_id=r["trace_id"],
        span_id=r["span_id"],
        parent_id=r["parent_id"],
        name=r["name"],
        kind=SpanKind(r["kind"]),
        start=datetime.fromisoformat(r["start"]),
        end=None if r["end_at"] is None else datetime.fromisoformat(r["end_at"]),
        task_id=r["task_id"],
        session_id=r["session_id"],
        model=r["model"],
        tool=r["tool"],
        args_hash=r["args_hash"],
        input_tokens=r["input_tokens"],
        output_tokens=r["output_tokens"],
        cached_tokens=r["cached_tokens"],
        outcome=r["outcome"],
        attributes=attributes if isinstance(attributes, dict) else {},
        cache_write_tokens=r["cache_write_tokens"],
    )


def open_readonly(
    path: Path | str, busy_timeout_ms: int, oldest: int | None = None
) -> sqlite3.Connection:
    """A connection that never writes, not even a migration (``kelpie status``). ``oldest``:
    an older schema it may also read (the snooze readers accept :data:`SNOOZES_SINCE`)."""
    db = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION and not (
        oldest is not None and oldest <= version < SCHEMA_VERSION
    ):
        db.close()
        if version < SCHEMA_VERSION:
            raise SchemaOutdated(
                f"database schema {version} is older than {SCHEMA_VERSION}; run kelpie serve"
                " once to upgrade it"
            )
        raise RuntimeError(f"database schema {version} is newer than {SCHEMA_VERSION}")
    return db


class SqliteSpanStore:
    """Spans in the Kelpie database, behind connections and locks of their own.

    Writes (ingest, links, retention) go through one connection in short batches; reads
    (snapshots, totals) through a second one. With WAL, a long read never blocks a write,
    and ingest never waits for a snapshot. Every insert or link update stamps the rows
    with a new ``version``, so :meth:`changes` returns only what changed since the last
    read; ``epoch`` grows with every delete, which tells a cache to reload.

    Which tokens count is decided per trace right after each insert and kept in the
    ``counted`` and ``price_model`` columns, so :meth:`usage_since` and
    :meth:`totals_since` are plain sums. ``readonly`` opens the file without writing
    anything (it must already have the current schema; :class:`SchemaOutdated` otherwise).
    """

    def __init__(
        self,
        path: Path | str,
        busy_timeout_ms: int = 5000,
        batch: int = SPAN_BATCH,
        *,
        readonly: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._closing = threading.Event()
        if readonly:
            self._db = self._reader = open_readonly(path, busy_timeout_ms)
        else:
            self._db = _open(path, busy_timeout_ms)
            self._reader = _open(path, busy_timeout_ms)
        self._reader.execute("PRAGMA query_only = 1")
        self._batch = max(1, batch)
        # Kept in step with every write under the lock, so count() needs no table scan.
        self._rows = int(self._db.execute("SELECT COUNT(*) FROM spans").fetchone()[0])
        self._version = int(
            self._db.execute("SELECT COALESCE(MAX(version), 0) FROM spans").fetchone()[0]
        )
        self.epoch = 0
        # What record_attribution wrote last, so a tick writes only what changed.
        self._sessions: dict[str, tuple[str, str | None, datetime]] | None = None
        self._labels: dict[str, tuple[tuple[str, ...], datetime]] | None = None
        if not readonly:
            self._decide_undecided()

    def close(self) -> None:
        self._closing.set()  # a running prune stops at its next batch
        with self._lock:
            self._db.close()
        with self._read_lock:
            self._reader.close()

    def append_spans(self, spans: Sequence[Span], received_at: datetime) -> int:
        received = _ts(received_at)
        sql = (
            f"INSERT OR IGNORE INTO spans ({_SPAN_COLUMNS})"
            f" VALUES ({', '.join('?' * len(_SPAN_COLUMNS.split(',')))})"
        )
        added = 0
        touched: set[str] = set()
        for i in range(0, len(spans), self._batch):
            chunk = spans[i : i + self._batch]
            with self._lock, self._db:  # one short transaction per batch
                version = self._version + 1
                n = self._db.executemany(sql, [_span_row(s, received, version) for s in chunk])
                self._version = version
                self._rows += n.rowcount
                added += n.rowcount
            if n.rowcount:
                touched.update(s.trace_id for s in chunk)
        self._decide(touched)
        return added

    def _decide_undecided(self) -> None:
        """Decide rows stored before counting existed (an upgraded database) or left
        undecided by an interrupted write."""
        rows = self._db.execute("SELECT DISTINCT trace_id FROM spans WHERE counted IS NULL")
        self._decide([r[0] for r in rows.fetchall()])

    def _decide(self, trace_ids: Iterable[str]) -> int:
        """Recompute which tokens count for whole traces (their spans may have arrived in
        any order) and store what changed. Returns the number of rows updated."""
        ids = sorted(set(trace_ids))
        updated = 0
        for i in range(0, len(ids), self._batch):
            chunk = ids[i : i + self._batch]
            marks = ", ".join("?" * len(chunk))
            sql = f"SELECT {_NODE_COLUMNS} FROM spans WHERE trace_id IN ({marks})"
            with self._lock:
                if self._closing.is_set():
                    return updated
                nodes = {
                    (r[0], r[1]): _Node(r[0], r[1], r[2], SpanKind(r[3]), *r[4:])
                    for r in self._db.execute(sql, chunk).fetchall()
                }
                counted = counted_llm(nodes, nodes.values())
                changes = [
                    (int(key in counted), counted.get(key), node.rowid)
                    for key, node in nodes.items()
                    if (node.counted, node.price_model) != (int(key in counted), counted.get(key))
                ]
                if changes:
                    with self._db:
                        self._db.executemany(
                            "UPDATE spans SET counted = ?, price_model = ? WHERE rowid_ = ?",
                            changes,
                        )
                updated += len(changes)
        return updated

    def _read(self, sql: str, args: Sequence[object]) -> list[sqlite3.Row]:
        with self._read_lock:
            return self._reader.execute(sql, tuple(args)).fetchall()

    def spans_since(self, after: datetime) -> Sequence[Span]:
        sql = f"SELECT {_SPAN_COLUMNS} FROM spans WHERE last_at >= ? ORDER BY start, rowid_"
        return [_span(r) for r in self._read(sql, (_ts(after),))]

    def tool_spans_since(self, after: datetime) -> Sequence[Span]:
        sql = (
            f"SELECT {_SPAN_COLUMNS} FROM spans INDEXED BY spans_last"
            " WHERE last_at >= ? AND kind = 'tool' ORDER BY start, rowid_"
        )
        return [_span(r) for r in self._read(sql, (_ts(after),))]

    def changes(self, after: datetime, since: int) -> SpanChanges:
        sql = (
            f"SELECT {_SPAN_COLUMNS} FROM spans WHERE version > ? AND last_at >= ?"
            " ORDER BY version, rowid_"
        )
        rows = self._read(sql, (since, _ts(after)))
        version = max((int(r["version"]) for r in rows), default=since)
        return SpanChanges(tuple(_span(r) for r in rows), version)

    def totals_since(self, after: datetime) -> Sequence[SpanTotals]:
        sql = (
            "SELECT session_id, MAX(task_id) AS task_id, COUNT(*) AS spans,"
            " SUM(counted = 1) AS llm_calls, SUM(kind = 'tool') AS tool_calls,"
            " SUM(kind = 'tool' AND outcome = 'error') AS tool_errors,"
            " SUM(CASE WHEN counted = 1 THEN input_tokens ELSE 0 END) AS input_tokens,"
            " SUM(CASE WHEN counted = 1 THEN output_tokens ELSE 0 END) AS output_tokens,"
            " SUM(CASE WHEN counted = 1 THEN cached_tokens ELSE 0 END) AS cached_tokens,"
            " MIN(start) AS first_start,"
            " MAX(last_at) AS last_end"
            " FROM spans INDEXED BY spans_last"
            " WHERE last_at >= ? AND session_id IS NOT NULL AND session_id != ''"
            " GROUP BY session_id ORDER BY session_id"
        )
        return [
            SpanTotals(
                session_id=r["session_id"],
                task_id=r["task_id"],
                spans=r["spans"],
                llm_calls=r["llm_calls"],
                tool_calls=r["tool_calls"],
                tool_errors=r["tool_errors"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                cached_tokens=r["cached_tokens"],
                first_start=datetime.fromisoformat(r["first_start"]),
                last_end=datetime.fromisoformat(r["last_end"]),
            )
            for r in self._read(sql, (_ts(after),))
        ]

    def usage_since(
        self, now: datetime, windows: Sequence[timedelta]
    ) -> list[tuple[TokenUsage, ...]]:
        """One plain indexed aggregate per window over counted rows only. A short window
        is a range scan on ``spans_counted`` (time first); a long one streams through
        ``spans_counted_by_subject`` (already in group order, so no sort of every row)."""
        out: list[tuple[TokenUsage, ...]] = []
        for window in windows:
            index = "spans_counted" if window <= SHORT_WINDOW else "spans_counted_by_subject"
            sql = (
                "SELECT session_id, task_id, price_model, COUNT(*), SUM(input_tokens),"
                " SUM(output_tokens), SUM(cached_tokens), SUM(cache_write_tokens)"
                f" FROM spans INDEXED BY {index}"
                " WHERE counted = 1 AND last_at >= ? GROUP BY session_id, task_id, price_model"
            )
            rows = [TokenUsage(*r) for r in self._read(sql, (_ts(now - window),))]
            out.append(tuple(sorted(rows, key=_usage_key)))
        return out

    def usage_between(self, start: datetime, end: datetime) -> tuple[TokenUsage, ...]:
        """A range scan on ``spans_counted`` (time first)."""
        sql = (
            "SELECT session_id, task_id, price_model, COUNT(*), SUM(input_tokens),"
            " SUM(output_tokens), SUM(cached_tokens), SUM(cache_write_tokens)"
            " FROM spans INDEXED BY spans_counted"
            " WHERE counted = 1 AND last_at >= ? AND last_at < ?"
            " GROUP BY session_id, task_id, price_model"
        )
        rows = [TokenUsage(*r) for r in self._read(sql, (_ts(start), _ts(end)))]
        return tuple(sorted(rows, key=_usage_key))

    def record_attribution(
        self,
        sessions: Iterable[tuple[str, str, str | None]],
        labels: Mapping[str, tuple[str, ...]],
        at: datetime,
    ) -> int:
        with self._lock:
            known_sessions, known_labels = self._known_attribution()
            session_rows = []
            for session_id, owner, task in sessions:
                old = known_sessions.get(session_id)
                if old is None or old[:2] != (owner, task) or attribution_stale(old[2], at):
                    known_sessions[session_id] = (owner, task, at)
                    session_rows.append((session_id, owner, task, _ts(at)))
            label_rows = []
            for task_id, names in labels.items():
                prev = known_labels.get(task_id)
                if prev is None or prev[0] != names or attribution_stale(prev[1], at):
                    known_labels[task_id] = (names, at)
                    label_rows.append((task_id, json.dumps(list(names)), _ts(at)))
            if session_rows or label_rows:
                with self._db:
                    self._db.executemany(
                        "INSERT INTO span_sessions (session_id, owner_id, task_id, seen_at)"
                        " VALUES (?, ?, ?, ?) ON CONFLICT (session_id) DO UPDATE SET"
                        " owner_id = excluded.owner_id, task_id = excluded.task_id,"
                        " seen_at = excluded.seen_at",
                        session_rows,
                    )
                    self._db.executemany(
                        "INSERT INTO span_task_labels (task_id, labels, seen_at)"
                        " VALUES (?, ?, ?) ON CONFLICT (task_id) DO UPDATE SET"
                        " labels = excluded.labels, seen_at = excluded.seen_at",
                        label_rows,
                    )
            return len(session_rows) + len(label_rows)

    def _known_attribution(
        self,
    ) -> tuple[
        dict[str, tuple[str, str | None, datetime]], dict[str, tuple[tuple[str, ...], datetime]]
    ]:
        """Loaded once, under the write lock."""
        if self._sessions is None or self._labels is None:
            self._sessions = {
                r[0]: (r[1], r[2], datetime.fromisoformat(r[3]))
                for r in self._db.execute(
                    "SELECT session_id, owner_id, task_id, seen_at FROM span_sessions"
                )
            }
            self._labels = {
                r[0]: (tuple(_labels(r[1])), datetime.fromisoformat(r[2]))
                for r in self._db.execute("SELECT task_id, labels, seen_at FROM span_task_labels")
            }
        return self._sessions, self._labels

    def attribution(self) -> Attribution:
        sessions = self._read("SELECT session_id, owner_id, task_id FROM span_sessions", ())
        labels = self._read("SELECT task_id, labels FROM span_task_labels", ())
        return Attribution(
            owners={r[0]: r[1] for r in sessions},
            session_tasks={r[0]: r[2] for r in sessions if r[2]},
            labels={r[0]: tuple(_labels(r[1])) for r in labels},
        )

    def prune(self, before: datetime, max_rows: int) -> int:
        """Batched deletes, each its own short transaction; the lock is released between
        batches so ingest interleaves with a long prune, and closing the store stops it."""
        deleted = 0
        old = (
            "DELETE FROM spans WHERE rowid_ IN (SELECT rowid_ FROM spans WHERE last_at < ? LIMIT ?)"
        )
        while n := self._delete(old, (_ts(before), self._batch)):
            deleted += n
        excess = self.count() - max(0, max_rows)
        oldest = (
            "DELETE FROM spans WHERE rowid_ IN"
            " (SELECT rowid_ FROM spans ORDER BY last_at, rowid_ LIMIT ?)"
        )
        while excess > 0 and (n := self._delete(oldest, (min(excess, self._batch),))):
            deleted += n
            excess -= n
        with self._lock:
            if not self._closing.is_set():
                with self._db:
                    for table in ("span_sessions", "span_task_labels"):
                        self._db.execute(f"DELETE FROM {table} WHERE seen_at < ?", (_ts(before),))
                self._sessions = self._labels = None  # reload on the next write
        return deleted

    def _delete(self, sql: str, args: tuple[object, ...]) -> int:
        with self._lock:
            if self._closing.is_set():
                return 0
            with self._db:
                n = self._db.execute(sql, args).rowcount
            self._rows -= n
            if n:
                self.epoch += 1  # after the commit: a reload must not see the rows again
            return n

    def oldest_at(self) -> datetime | None:
        rows = self._read("SELECT MIN(last_at) FROM spans INDEXED BY spans_last", ())
        return datetime.fromisoformat(rows[0][0]) if rows and rows[0][0] is not None else None

    def count(self) -> int:
        return self._rows  # an int read is atomic; never waits for a write batch

    def inherit_links(self, trace_ids: Collection[str]) -> int:
        """One UPDATE per tree level and batch of traces (correlated subqueries rather than
        ``UPDATE ... FROM``, which older SQLite builds lack). Updated rows get a new
        version, so snapshot caches pick up their links."""
        ids = sorted(set(trace_ids))
        updated = 0
        parent = "p.trace_id = spans.trace_id AND p.span_id = spans.parent_id"
        for i in range(0, len(ids), self._batch):
            chunk = ids[i : i + self._batch]
            sql = (
                "UPDATE spans SET version = ?,"
                f" task_id = COALESCE(task_id, (SELECT p.task_id FROM spans p WHERE {parent})),"
                " session_id = COALESCE(session_id,"
                f" (SELECT p.session_id FROM spans p WHERE {parent}))"
                f" WHERE trace_id IN ({', '.join('?' * len(chunk))})"
                " AND parent_id IS NOT NULL AND EXISTS (SELECT 1 FROM spans p WHERE"
                f" {parent} AND ((spans.task_id IS NULL AND p.task_id IS NOT NULL)"
                " OR (spans.session_id IS NULL AND p.session_id IS NOT NULL)))"
            )
            for _ in range(MAX_LINK_DEPTH):
                if not (n := self._update(sql, tuple(chunk))):
                    break
                updated += n
        return updated

    def _update(self, sql: str, args: tuple[object, ...]) -> int:
        with self._lock, self._db:
            version = self._version + 1
            n = self._db.execute(sql, (version, *args)).rowcount
            if n:
                self._version = version
            return n


def _labels(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


# Earlier private names, still imported by readers of the database (the report).
_entry, _state, _open_readonly = ledger_entry, open_state, open_readonly


# --- providers' cost reports (kelpie.connectors.cost_reports) -----------------------------

COST_REPORT_RETENTION = timedelta(days=90)


class SqliteCostReports:
    """The daily figures of providers' cost reports, as last read: a new reading of a
    provider replaces its rows for the days it covers; rows older than
    :data:`COST_REPORT_RETENTION` are dropped. ``readonly`` as in :class:`SqliteSpanStore`."""

    def __init__(
        self, path: Path | str, busy_timeout_ms: int = 5000, *, readonly: bool = False
    ) -> None:
        self._lock = threading.Lock()
        self._db = (
            open_readonly(path, busy_timeout_ms) if readonly else _open(path, busy_timeout_ms)
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def replace(self, costs: Sequence[ReportedCost], fetched_at: datetime) -> None:
        """Store ``costs`` (any providers): per provider, its rows for the days ``costs``
        cover are replaced."""
        days: dict[str, set[str]] = {}
        for c in costs:
            days.setdefault(c.provider, set()).add(c.day.isoformat())
        cutoff = (fetched_at - COST_REPORT_RETENTION).date().isoformat()
        with self._lock, self._db:
            for provider, covered in days.items():
                self._db.execute(
                    "DELETE FROM cost_reports WHERE provider = ? AND day >= ? AND day <= ?",
                    (provider, min(covered), max(covered)),
                )
            self._db.executemany(
                "INSERT OR REPLACE INTO cost_reports (provider, day, model, usd, fetched_at)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (c.provider, c.day.isoformat(), c.model or "", c.usd, _ts(fetched_at))
                    for c in costs
                ],
            )
            self._db.execute("DELETE FROM cost_reports WHERE day < ?", (cutoff,))

    def read(self, since: date, until: date | None = None) -> tuple[ReportedCost, ...]:
        """The stored figures of the days from ``since`` to ``until`` (inclusive)."""
        sql = "SELECT provider, day, model, usd FROM cost_reports WHERE day >= ?"
        args: list[object] = [since.isoformat()]
        if until is not None:
            sql += " AND day <= ?"
            args.append(until.isoformat())
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY provider, day, model", args).fetchall()
        return tuple(
            ReportedCost(r["provider"], date.fromisoformat(r["day"]), r["usd"], r["model"] or None)
            for r in rows
        )
