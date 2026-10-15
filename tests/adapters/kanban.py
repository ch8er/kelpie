"""A synthetic Hermes kanban.db for tests: the columns Kelpie reads, nothing else.

The CREATE TABLE statements are trimmed copies of ``SCHEMA_SQL`` in Hermes'
``hermes_cli/kanban_db.py``; the rows are made up.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    assignee TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    completed_at INTEGER,
    current_run_id INTEGER,
    last_heartbeat_at INTEGER,
    session_id TEXT,
    block_kind TEXT,
    claim_expires INTEGER,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_failure_error TEXT
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL,
    child_id TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    run_id INTEGER,
    kind TEXT NOT NULL,
    payload TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    profile TEXT,
    status TEXT NOT NULL,
    last_heartbeat_at INTEGER,
    started_at INTEGER NOT NULL,
    ended_at INTEGER,
    outcome TEXT,
    summary TEXT,
    error TEXT
);
"""


def epoch(dt: datetime) -> int:
    return int(dt.timestamp())


class Board:
    """Write helper around a temporary kanban.db."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")  # as Hermes runs it
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def task(
        self,
        id: str,
        status: str,
        created: datetime,
        *,
        title: str = "Synthetic task",
        assignee: str | None = None,
        started: datetime | None = None,
        completed: datetime | None = None,
        block_kind: str | None = None,
        failures: int = 0,
        last_error: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, started_at,"
            " completed_at, block_kind, consecutive_failures, last_failure_error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                title,
                assignee,
                status,
                epoch(created),
                epoch(started) if started else None,
                epoch(completed) if completed else None,
                block_kind,
                failures,
                last_error,
            ),
        )
        self.conn.commit()

    def update(self, task_id: str, **columns: datetime | int | str | None) -> None:
        """Change task columns; datetimes are stored as epoch seconds."""
        values = [epoch(v) if isinstance(v, datetime) else v for v in columns.values()]
        assignments = ", ".join(f"{name} = ?" for name in columns)
        self.conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values, task_id))
        self.conn.commit()

    def event(
        self, task_id: str, kind: str, at: datetime, payload: dict[str, Any] | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, json.dumps(payload) if payload is not None else None, epoch(at)),
        )
        self.conn.commit()

    def link(self, parent: str, child: str) -> None:
        self.conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))
        self.conn.commit()

    def run(
        self,
        task_id: str,
        profile: str | None,
        started: datetime,
        *,
        status: str = "running",
        ended: datetime | None = None,
        heartbeat: datetime | None = None,
        outcome: str | None = None,
        error: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, last_heartbeat_at, started_at,"
            " ended_at, outcome, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                profile,
                status,
                epoch(heartbeat) if heartbeat else None,
                epoch(started),
                epoch(ended) if ended else None,
                outcome,
                error,
            ),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def close(self) -> None:
        self.conn.close()
