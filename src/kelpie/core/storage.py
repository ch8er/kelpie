"""SQLite persistence. Today: the action ledger. Snapshots and spans follow.

The database runs in WAL mode so readers (``kelpie status``, the report) do not block the
control loop. One connection is shared across threads behind a lock. Each insert commits
on its own: the ledger writes a handful of rows per tick, and losing one would let an
action repeat.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from kelpie.core.actions import ActionKind
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression, to_json

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

# version -> script that upgrades from version - 1. Append, never edit a released one.
MIGRATIONS: dict[int, str] = {1: _V1}
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


class SqliteLedgerStore:
    def __init__(self, path: Path | str, busy_timeout_ms: int = 5000) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than {SCHEMA_VERSION}")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            # executescript commits any open transaction first, so wrap the script and the
            # version bump in one explicit transaction: a failed migration leaves no trace.
            try:
                self._db.executescript(
                    f"BEGIN;\n{MIGRATIONS[target]}\nPRAGMA user_version = {target};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self._db.in_transaction:
                    self._db.rollback()
                raise

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
