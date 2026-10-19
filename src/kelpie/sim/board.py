"""The simulated Hermes board: a ``kanban.db`` with the Hermes schema, written as Hermes
writes it (WAL mode, one transaction per change) and read back for assertions.

The tables are trimmed copies of ``SCHEMA_SQL`` in Hermes' ``hermes_cli/kanban_db.py``: the
columns Kelpie reads (:data:`KANBAN_SCHEMA`, also the schema of the adapter's unit tests),
``task_comments`` included (the Hermes CLI writes the reason of a block there; rule 1 reads
people's acceptance criteria from it). The rows are made up.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

KANBAN_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT,
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
    last_failure_error TEXT,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch',
    workspace_path TEXT,
    branch_name TEXT,
    created_by TEXT
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
    error TEXT,
    claim_lock TEXT,
    worker_pid INTEGER,
    worker_started_at INTEGER
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    author TEXT,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
"""

SCHEMA = KANBAN_SCHEMA


def epoch(dt: datetime) -> int:
    return int(dt.timestamp())


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    """A short-lived connection with an explicit transaction, committed on success."""
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class TaskRow:
    id: str
    status: str
    assignee: str | None
    block_kind: str | None
    current_run_id: int | None


@dataclass(frozen=True, slots=True)
class RunRow:
    id: int
    task_id: str
    status: str
    ended_at: int | None
    outcome: str | None


class Board:
    """Writes and reads a Hermes ``kanban.db``. Every method opens its own connection, so
    the board can be changed while Kelpie (or the fake CLI) reads it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # as Hermes runs it
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    # --- writing ---------------------------------------------------------------------

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
        workspace_kind: str = "scratch",
        workspace_path: str | None = None,
        branch: str | None = None,
    ) -> None:
        with connect(self.path) as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at, started_at,"
                " completed_at, block_kind, consecutive_failures, last_failure_error,"
                " workspace_kind, workspace_path, branch_name)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    workspace_kind,
                    workspace_path,
                    branch,
                ),
            )

    def update(self, task_id: str, **columns: datetime | int | str | None) -> None:
        """Change task columns; datetimes are stored as epoch seconds."""
        values = [epoch(v) if isinstance(v, datetime) else v for v in columns.values()]
        assignments = ", ".join(f"{name} = ?" for name in columns)
        with connect(self.path) as conn:
            conn.execute(f"UPDATE tasks SET {assignments} WHERE id = ?", (*values, task_id))

    def event(
        self,
        task_id: str,
        kind: str,
        at: datetime,
        payload: dict[str, Any] | None = None,
        run_id: int | None = None,
    ) -> None:
        with connect(self.path) as conn:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    task_id,
                    run_id,
                    kind,
                    json.dumps(payload) if payload is not None else None,
                    epoch(at),
                ),
            )

    def link(self, parent: str, child: str) -> None:
        with connect(self.path) as conn:
            conn.execute("INSERT INTO task_links VALUES (?, ?)", (parent, child))

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
        summary: str | None = None,
    ) -> int:
        with connect(self.path) as conn:
            cur = conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, last_heartbeat_at, started_at,"
                " ended_at, outcome, summary, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    profile,
                    status,
                    epoch(heartbeat) if heartbeat else None,
                    epoch(started),
                    epoch(ended) if ended else None,
                    outcome,
                    summary,
                    error,
                ),
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    def claim(
        self,
        task_id: str,
        profile: str,
        started: datetime,
        *,
        heartbeat: datetime | None = None,
        claim_expires: datetime | None = None,
    ) -> int:
        """A worker claims a task: a ``claimed`` event, an open run, the task ``running``."""
        self.event(task_id, "claimed", started)
        rid = self.run(task_id, profile, started, heartbeat=heartbeat)
        self.update(
            task_id,
            status="running",
            started_at=started,
            current_run_id=rid,
            last_heartbeat_at=heartbeat,
            claim_expires=claim_expires,
        )
        return rid

    def bind_worker(
        self,
        run_id: int,
        at: datetime,
        *,
        claim_lock: str,
        submitter: tuple[int, str | None] | None = None,
        agent: tuple[int, str | None] | None = None,
    ) -> None:
        """Record a run's worker processes as Hermes does: the spawned submitter's
        ``(pid, fingerprint)`` on the run row, the agent's in a ``worker_bound`` event."""
        pid, started = submitter if submitter is not None else (None, None)
        with connect(self.path) as conn:
            conn.execute(
                "UPDATE task_runs SET claim_lock = ?, worker_pid = ?, worker_started_at = ?"
                " WHERE id = ?",
                (claim_lock, pid, started, run_id),
            )
            task_id = conn.execute(
                "SELECT task_id FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()[0]
        if agent is not None:
            payload = {"pid": agent[0], "claim_lock": claim_lock, "started_at": agent[1]}
            self.event(task_id, "worker_bound", at, payload, run_id=run_id)

    def end_run(self, run_id: int, at: datetime, outcome: str | None = None) -> None:
        """A run ends (``outcome`` None: the worker went away without declaring one)."""
        with connect(self.path) as conn:
            conn.execute(
                "UPDATE task_runs SET status = ?, ended_at = ?, outcome = ? WHERE id = ?",
                (outcome or "ended", epoch(at), outcome, run_id),
            )

    def block(
        self,
        task_id: str,
        reason: str,
        at: datetime,
        *,
        kind: str | None = None,
    ) -> None:
        """The worker running ``task_id`` blocks it, as Hermes records it: the run ends
        ``blocked``, the task is ``blocked`` without a claim, a ``blocked`` event carries
        the kind (if typed) and the reason."""
        rid = self.get(task_id).current_run_id
        assert rid is not None, f"{task_id} has no current run"
        self.end_run(rid, at, "blocked")
        self.update(
            task_id, status="blocked", current_run_id=None, claim_expires=None, block_kind=kind
        )
        payload: dict[str, Any] = {"reason": reason}
        if kind:
            payload["kind"] = kind
        self.event(task_id, "blocked", at, payload, run_id=rid)

    def hand_off(self, task_id: str, at: datetime, outcome: str = "review_requested") -> None:
        """The worker hands the task over to review: its run ends with ``outcome``."""
        rid = self.get(task_id).current_run_id
        assert rid is not None, f"{task_id} has no current run"
        self.end_run(rid, at, outcome)
        self.update(task_id, status="review", current_run_id=None, claim_expires=None)
        self.event(task_id, outcome, at, run_id=rid)

    # --- reading ---------------------------------------------------------------------

    def _read(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def get(self, task_id: str) -> TaskRow:
        rows = self._read(
            "SELECT id, status, assignee, block_kind, current_run_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        if not rows:
            raise KeyError(task_id)
        r = rows[0]
        return TaskRow(r["id"], r["status"], r["assignee"], r["block_kind"], r["current_run_id"])

    def statuses(self) -> dict[str, str]:
        return {r["id"]: r["status"] for r in self._read("SELECT id, status FROM tasks")}

    def get_run(self, run_id: int) -> RunRow:
        r = self._read(
            "SELECT id, task_id, status, ended_at, outcome FROM task_runs WHERE id = ?", (run_id,)
        )[0]
        return RunRow(r["id"], r["task_id"], r["status"], r["ended_at"], r["outcome"])

    def events(self, task_id: str) -> list[tuple[str, dict[str, Any]]]:
        rows = self._read(
            "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )
        return [(r["kind"], json.loads(r["payload"]) if r["payload"] else {}) for r in rows]

    def comments(self, task_id: str) -> list[str]:
        rows = self._read(
            "SELECT body FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
        )
        return [r["body"] for r in rows]

    def comment_authors(self, task_id: str) -> list[str]:
        rows = self._read(
            "SELECT author FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
        )
        return [r["author"] for r in rows]
