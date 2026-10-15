"""SQLite persistence: the action ledger, long-lived alert states and trace spans.

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
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, datetime
from pathlib import Path

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression, to_json
from kelpie.core.model import Span, SpanKind, SpanTotals
from kelpie.core.spans import MAX_LINK_DEPTH, SpanChanges, last_at
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

# version -> script that upgrades from version - 1. Append, never edit a released one.
MIGRATIONS: dict[int, str] = {1: _V1, 2: _V2, 3: _V3}
SCHEMA_VERSION = max(MIGRATIONS)

_COLUMNS = (
    "at, fingerprint, subject, rule, kind, mode, outcome, reason, task_id, executor,"
    " params, suppressed, suppression"
)


def _ts(at: datetime) -> str:
    """Fixed-width UTC timestamp, so string order equals time order."""
    if at.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


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
            return [_entry(r) for r in self._db.execute(sql, args).fetchall()]

    def _write(self, fn: Callable[[sqlite3.Connection], sqlite3.Cursor]) -> sqlite3.Cursor:
        with self._lock, self._db:
            return fn(self._db)

    def append(self, entry: LedgerEntry) -> None:
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
        )
        sql = f"INSERT INTO actions ({_COLUMNS}) VALUES ({', '.join('?' * len(values))})"
        self._write(lambda db: db.execute(sql, values))

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
        return [_state(r) for r in rows]

    def get_state(self, state_id: str) -> OpenState | None:
        sql = f"SELECT {self._STATE_COLUMNS} FROM states WHERE id = ? AND closed_at IS NULL"
        with self._lock:
            row = self._db.execute(sql, (state_id,)).fetchone()
        return None if row is None else _state(row)

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


# --- trace spans (kelpie.core.spans.SpanStore) --------------------------------------------

_SPAN_COLUMNS = (
    "trace_id, span_id, parent_id, name, kind, start, end_at, last_at, task_id, session_id,"
    " model, tool, args_hash, input_tokens, output_tokens, cached_tokens, outcome, attributes,"
    " received_at, version"
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
    )


class SqliteSpanStore:
    """Spans in the Kelpie database, behind connections and locks of their own.

    Writes (ingest, links, retention) go through one connection in short batches; reads
    (snapshots, totals) through a second one. With WAL, a long read never blocks a write,
    and ingest never waits for a snapshot. Every insert or link update stamps the rows
    with a new ``version``, so :meth:`changes` returns only what changed since the last
    read; ``epoch`` grows with every delete, which tells a cache to reload.
    """

    def __init__(
        self, path: Path | str, busy_timeout_ms: int = 5000, batch: int = SPAN_BATCH
    ) -> None:
        self._lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._closing = threading.Event()
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
        for i in range(0, len(spans), self._batch):
            chunk = spans[i : i + self._batch]
            with self._lock, self._db:  # one short transaction per batch
                version = self._version + 1
                n = self._db.executemany(sql, [_span_row(s, received, version) for s in chunk])
                self._version = version
                self._rows += n.rowcount
                added += n.rowcount
        return added

    def _read(self, sql: str, args: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._read_lock:
            return self._reader.execute(sql, args).fetchall()

    def spans_since(self, after: datetime) -> Sequence[Span]:
        sql = f"SELECT {_SPAN_COLUMNS} FROM spans WHERE last_at >= ? ORDER BY start, rowid_"
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
            " SUM(kind = 'llm') AS llm_calls, SUM(kind = 'tool') AS tool_calls,"
            " SUM(kind = 'tool' AND outcome = 'error') AS tool_errors,"
            " SUM(CASE WHEN kind = 'llm' THEN input_tokens ELSE 0 END) AS input_tokens,"
            " SUM(CASE WHEN kind = 'llm' THEN output_tokens ELSE 0 END) AS output_tokens,"
            " SUM(CASE WHEN kind = 'llm' THEN cached_tokens ELSE 0 END) AS cached_tokens,"
            " MIN(start) AS first_start,"
            " MAX(last_at) AS last_end"
            " FROM spans WHERE last_at >= ? AND session_id IS NOT NULL AND session_id != ''"
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
        return deleted

    def _delete(self, sql: str, args: tuple[object, ...]) -> int:
        with self._lock:
            if self._closing.is_set():
                return 0
            with self._db:
                n = self._db.execute(sql, args).rowcount
                self._rows -= n
                if n:
                    self.epoch += 1
                return n

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
