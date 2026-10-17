"""A synthetic OpenClaw Workboard database (``workboard.sqlite``) for tests.

The tables Kelpie reads are created with the DDL of OpenClaw 2026.10.4's Workboard plugin
(``sqlite-store-schema.ts``, schema version 3), so a column Kelpie reads that OpenClaw does
not have fails here first. Times are epoch milliseconds, as Workboard stores them.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE workboard_cards (
  id TEXT PRIMARY KEY, board_id TEXT NOT NULL, title TEXT NOT NULL, notes TEXT,
  status TEXT NOT NULL, priority TEXT NOT NULL, agent_id TEXT, session_key TEXT, run_id TEXT,
  task_id TEXT, source_url TEXT, position REAL NOT NULL, created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER, execution_id TEXT,
  execution_kind TEXT, execution_engine TEXT, execution_mode TEXT, execution_status TEXT,
  execution_model TEXT, execution_session_key TEXT, execution_run_id TEXT,
  execution_started_at INTEGER, execution_updated_at INTEGER, automation_json TEXT,
  claim_json TEXT, template_id TEXT, archived_at INTEGER, stale_json TEXT,
  lifecycle_status_source_updated_at INTEGER, failure_count INTEGER
) STRICT;
CREATE TABLE workboard_card_labels (
  card_id TEXT NOT NULL REFERENCES workboard_cards(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY(card_id, ordinal)
) STRICT;
CREATE TABLE workboard_card_events (
  id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES workboard_cards(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL, from_status TEXT,
  to_status TEXT, session_key TEXT, run_id TEXT
) STRICT;
CREATE TABLE workboard_card_attempts (
  id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES workboard_cards(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, status TEXT NOT NULL, started_at INTEGER NOT NULL,
  ended_at INTEGER, engine TEXT, mode TEXT, model TEXT, session_key TEXT, run_id TEXT,
  error TEXT
) STRICT;
CREATE TABLE workboard_card_comments (
  id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES workboard_cards(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL,
  updated_at INTEGER
) STRICT;
CREATE TABLE workboard_card_links (
  id TEXT PRIMARY KEY, card_id TEXT NOT NULL REFERENCES workboard_cards(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL, type TEXT NOT NULL, target_card_id TEXT, title TEXT, url TEXT,
  created_at INTEGER NOT NULL
) STRICT;
"""


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


class Workboard:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)
            db.execute("PRAGMA journal_mode=WAL")  # as OpenClaw opens it

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def _ordinal(self, db: sqlite3.Connection, table: str, card_id: str) -> int:
        row = db.execute(f"SELECT COUNT(*) FROM {table} WHERE card_id = ?", (card_id,)).fetchone()
        return int(row[0])

    def card(
        self,
        card_id: str,
        status: str,
        at: datetime,
        *,
        title: str = "",
        agent: str | None = None,
        session_key: str | None = None,
        run_id: str | None = None,
        claim: dict[str, Any] | None = None,
        notes: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        archived_at: datetime | None = None,
        board: str = "default",
        event: bool = True,
    ) -> str:
        """A card created at ``at`` in ``status`` (with its ``created`` event)."""
        with self._db() as db:
            db.execute(
                "INSERT INTO workboard_cards (id, board_id, title, notes, status, priority, "
                "agent_id, session_key, run_id, position, created_at, updated_at, started_at, "
                "completed_at, claim_json, archived_at) "
                "VALUES (?, ?, ?, ?, ?, 'normal', ?, ?, ?, 1000, ?, ?, ?, ?, ?, ?)",
                (
                    card_id,
                    board,
                    title or card_id,
                    notes,
                    status,
                    agent,
                    session_key,
                    run_id,
                    ms(at),
                    ms(at),
                    ms(started_at) if started_at else None,
                    ms(completed_at) if completed_at else None,
                    json.dumps(claim) if claim is not None else None,
                    ms(archived_at) if archived_at else None,
                ),
            )
        if event:
            self.event(card_id, "created", at, to_status=status)
        return card_id

    def move(self, card_id: str, status: str, at: datetime) -> None:
        """What a Workboard move does: the status and a ``moved`` event."""
        with self._db() as db:
            (old,) = db.execute(
                "SELECT status FROM workboard_cards WHERE id = ?", (card_id,)
            ).fetchone()
            db.execute(
                "UPDATE workboard_cards SET status = ?, updated_at = ? WHERE id = ?",
                (status, ms(at), card_id),
            )
        self.event(card_id, "moved", at, from_status=old, to_status=status)

    def event(
        self,
        card_id: str,
        kind: str,
        at: datetime,
        *,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO workboard_card_events (id, card_id, ordinal, kind, at, from_status, "
                "to_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    card_id,
                    self._ordinal(db, "workboard_card_events", card_id),
                    kind,
                    ms(at),
                    from_status,
                    to_status,
                ),
            )

    def label(self, card_id: str, *labels: str) -> None:
        with self._db() as db:
            for label in labels:
                db.execute(
                    "INSERT INTO workboard_card_labels VALUES (?, ?, ?)",
                    (card_id, self._ordinal(db, "workboard_card_labels", card_id), label),
                )

    def link(self, card_id: str, kind: str, target: str, at: datetime) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO workboard_card_links (id, card_id, ordinal, type, target_card_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    card_id,
                    self._ordinal(db, "workboard_card_links", card_id),
                    kind,
                    target,
                    ms(at),
                ),
            )

    def attempt(
        self,
        card_id: str,
        status: str,
        started: datetime,
        ended: datetime | None = None,
        error: str | None = None,
    ) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO workboard_card_attempts (id, card_id, ordinal, status, started_at, "
                "ended_at, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    card_id,
                    self._ordinal(db, "workboard_card_attempts", card_id),
                    status,
                    ms(started),
                    ms(ended) if ended else None,
                    error,
                ),
            )

    def comment(self, card_id: str, body: str, at: datetime) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO workboard_card_comments (id, card_id, ordinal, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    card_id,
                    self._ordinal(db, "workboard_card_comments", card_id),
                    body,
                    ms(at),
                ),
            )

    def get(self, card_id: str) -> sqlite3.Row:
        db = self._db()
        db.row_factory = sqlite3.Row
        try:
            return db.execute("SELECT * FROM workboard_cards WHERE id = ?", (card_id,)).fetchone()
        finally:
            db.close()
