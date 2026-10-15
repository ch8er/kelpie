"""Hermes platform adapter: read side.

Describes a Hermes farm to Kelpie from two sources:

- the Kanban SQLite database (``~/.hermes/kanban.db``), opened strictly read-only, one
  short-lived connection per snapshot (Hermes writes to it concurrently, WAL mode);
- the hooks spool written by the Hermes plugin (:mod:`kelpie.adapters.hermes.plugin`),
  which supplies tool-call spans.

Actions (requeue, unblock, comment, WIP commit) are not implemented yet: run Kelpie in
``observe`` or ``suggest`` mode.

Status mapping (task ``status`` -> :class:`~kelpie.core.model.Status`)::

    triage, todo, scheduled -> BACKLOG   (``scheduled`` is a timed queue, not a Kelpie wait)
    ready                   -> READY
    running                 -> IN_PROGRESS
    blocked                 -> BLOCKED
    review                  -> REVIEW
    done                    -> DONE
    archived                -> (skipped)

``role`` is the assignee profile: Hermes routes work by profile, so a profile is both the
owner and its role. ``status_since`` is the time of the latest status-changing event of the
task (``task_events``), else ``started_at``/``completed_at``/``created_at``. A blocked task
carries a :class:`~kelpie.core.model.Block` built from its latest block event
(``"<kind>: <reason>"``, e.g. ``needs_input: which API version?``).

Sessions: one per ``task_runs`` row (``hermes-run-<id>``), for runs that are open or ended
within ``lookback``::

    run outcome                                    final_status
    completed                                      DONE
    review_requested                               REVIEW
    blocked                                        BLOCKED
    crashed, timed_out, spawn_failed, gave_up,
    reclaimed, changes_requested, scheduled,
    NULL (still open or ended without outcome)     None  (the session declared nothing)

``exit_reason`` is the outcome, followed by the run's error text when there is one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.core.model import (
    Block,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.core.ports import PlatformEvent

log = logging.getLogger(__name__)

DEFAULT_DB = Path("~/.hermes/kanban.db")
DEFAULT_SPOOL = Path("~/.hermes/kelpie/spool.jsonl")
DEFAULT_LOOKBACK = timedelta(hours=24)
BUSY_TIMEOUT_MS = 5000

NOT_IMPLEMENTED = "Hermes actions are not implemented yet; run Kelpie in observe or suggest mode"

STATUS_MAP: Mapping[str, Status] = {
    "triage": Status.BACKLOG,
    "todo": Status.BACKLOG,
    "scheduled": Status.BACKLOG,
    "ready": Status.READY,
    "running": Status.IN_PROGRESS,
    "blocked": Status.BLOCKED,
    "review": Status.REVIEW,
    "done": Status.DONE,
}

OUTCOME_STATUS: Mapping[str, Status] = {
    "completed": Status.DONE,
    "review_requested": Status.REVIEW,
    "blocked": Status.BLOCKED,
}

# Event kinds in ``task_events`` after which a task is in a (possibly) new status.
STATUS_EVENTS = (
    "created",
    "claimed",
    "promoted",
    "promoted_manual",
    "completed",
    "review_requested",
    "review_reopened",
    "changes_requested",
    "blocked",
    "dependency_wait",
    "block_loop_detected",
    "auto_blocked",
    "unblocked",
    "scheduled",
    "reclaimed",
    "crashed",
    "timed_out",
    "gave_up",
    "spawn_failed",
    "status",
)
BLOCK_EVENTS = ("blocked", "dependency_wait", "block_loop_detected", "auto_blocked")


class HermesError(RuntimeError):
    """The Hermes board cannot be read."""


def _utc(epoch: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(epoch), UTC) if epoch is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _in_clause(values: Sequence[str]) -> str:
    return ",".join("?" * len(values))


def _payload(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _block_reason(payload: Mapping[str, Any], block_kind: str | None) -> str:
    kind = payload.get("kind") or block_kind
    reason = payload.get("reason")
    kind_text = str(kind) if kind else ""
    reason_text = str(reason).strip() if reason else ""
    if kind_text and reason_text:
        return f"{kind_text}: {reason_text}"
    return kind_text or reason_text


@dataclass(slots=True)
class HermesAdapter:
    db_path: Path = DEFAULT_DB
    spool: Path | None = DEFAULT_SPOOL  # None: no hook data, no spans
    lookback: timedelta = DEFAULT_LOOKBACK
    name: str = "hermes"

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path).expanduser()
        if self.spool is not None:
            self.spool = Path(self.spool).expanduser()

    # --- reading -------------------------------------------------------------------------

    def check(self) -> str | None:
        """``None`` if the board can be read, else why not (for ``kelpie status``)."""
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchall()
        except (HermesError, sqlite3.Error) as exc:
            return str(exc)
        return None

    def _connect(self) -> _Connection:
        if not self.db_path.is_file():
            raise HermesError(f"Hermes kanban database not found: {self.db_path}")
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA query_only=1")
        except sqlite3.Error as exc:
            raise HermesError(f"cannot open Hermes kanban database {self.db_path}: {exc}") from exc
        return _Connection(conn)

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now - self.lookback
        try:
            with self._connect() as conn:
                tasks, owner_ids = self._tasks(conn)
                ids = {t.id for t in tasks}
                dependencies = self._dependencies(conn, ids)
                sessions, profiles = self._sessions(conn, ids, cutoff)
        except sqlite3.Error as exc:
            raise HermesError(f"cannot read Hermes kanban database {self.db_path}: {exc}") from exc
        owners = tuple(
            Owner(id=p, role=p, kind=OwnerKind.AGENT) for p in sorted(owner_ids | profiles)
        )
        return Snapshot(
            now=now,
            tasks=tasks,
            owners=owners,
            dependencies=dependencies,
            sessions=sessions,
            spans=self._spans(cutoff),
        )

    def _tasks(self, conn: _Connection) -> tuple[tuple[Task, ...], set[str]]:
        rows = conn.execute("SELECT * FROM tasks WHERE status != 'archived' ORDER BY id").fetchall()
        latest = self._latest_events(conn, STATUS_EVENTS)
        blocks = self._latest_events(conn, BLOCK_EVENTS)
        tasks: list[Task] = []
        owners: set[str] = set()
        for row in rows:
            status = STATUS_MAP.get(row["status"])
            if status is None:
                log.warning(
                    "hermes: task %s has unknown status %r; skipped", row["id"], row["status"]
                )
                continue
            created = _utc(row["created_at"]) or datetime.fromtimestamp(0, UTC)
            fallback = {
                Status.IN_PROGRESS: _utc(row["started_at"]),
                Status.DONE: _utc(row["completed_at"]),
            }.get(status)
            event = latest.get(row["id"])
            since = _utc(event["created_at"]) if event else None
            since = since or fallback or created
            assignee = (row["assignee"] or "").strip() or None
            if assignee:
                owners.add(assignee)
            tasks.append(
                Task(
                    id=row["id"],
                    title=row["title"],
                    status=status,
                    status_since=since,
                    role=assignee,
                    owner_id=assignee,
                    source=self.name,
                    block=self._block(row, status, since, blocks.get(row["id"])),
                )
            )
        return tuple(tasks), owners

    @staticmethod
    def _block(
        row: sqlite3.Row, status: Status, since: datetime, event: sqlite3.Row | None
    ) -> Block | None:
        if status is not Status.BLOCKED:
            return None
        kind = row["block_kind"] if "block_kind" in row.keys() else None  # noqa: SIM118
        at = _utc(event["created_at"]) if event else None
        if event is None or at is None or at < since:
            return Block(reason=kind or "", since=since)
        return Block(reason=_block_reason(_payload(event["payload"]), kind), since=at)

    @staticmethod
    def _latest_events(conn: _Connection, kinds: Sequence[str]) -> dict[str, sqlite3.Row]:
        rows = conn.execute(
            "SELECT task_id, kind, payload, created_at FROM ("
            " SELECT *, ROW_NUMBER() OVER ("
            "  PARTITION BY task_id ORDER BY created_at DESC, id DESC) AS rn"
            f" FROM task_events WHERE kind IN ({_in_clause(kinds)})) WHERE rn = 1",
            tuple(kinds),
        ).fetchall()
        return {r["task_id"]: r for r in rows}

    @staticmethod
    def _dependencies(conn: _Connection, ids: set[str]) -> tuple[Dependency, ...]:
        rows = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY child_id, parent_id"
        ).fetchall()
        # Archived parents count as satisfied in Hermes, so links to them are dropped.
        return tuple(
            Dependency(task_id=r["child_id"], depends_on=r["parent_id"])
            for r in rows
            if r["child_id"] in ids and r["parent_id"] in ids
        )

    @staticmethod
    def _sessions(
        conn: _Connection, ids: set[str], cutoff: datetime
    ) -> tuple[tuple[Session, ...], set[str]]:
        rows = conn.execute(
            "SELECT r.*, t.assignee AS assignee FROM task_runs r "
            "LEFT JOIN tasks t ON t.id = r.task_id "
            "WHERE r.ended_at IS NULL OR r.ended_at >= ? ORDER BY r.id",
            (int(cutoff.timestamp()),),
        ).fetchall()
        sessions: list[Session] = []
        profiles: set[str] = set()
        for r in rows:
            started = _utc(r["started_at"])
            if r["task_id"] not in ids or started is None:
                continue
            profile = (r["profile"] or "").strip()
            if profile:
                profiles.add(profile)
            outcome = r["outcome"]
            reason = " ".join(str(x).strip() for x in (outcome, r["error"]) if x and str(x).strip())
            sessions.append(
                Session(
                    id=f"hermes-run-{r['id']}",
                    task_id=r["task_id"],
                    owner_id=profile or (r["assignee"] or "").strip() or "unknown",
                    started_at=started,
                    ended_at=_utc(r["ended_at"]),
                    final_status=OUTCOME_STATUS.get(outcome) if outcome else None,
                    exit_reason=reason[:300] or None,
                    last_heartbeat=_utc(r["last_heartbeat_at"]),
                )
            )
        return tuple(sessions), profiles

    # --- hooks spool ---------------------------------------------------------------------

    def _spool_lines(self) -> Iterator[str]:
        if self.spool is None:
            return
        for path in (self.spool.with_name(self.spool.name + ".1"), self.spool):
            try:
                with path.open(encoding="utf-8", errors="replace") as f:
                    yield from f
            except OSError:
                continue

    def _spans(self, cutoff: datetime) -> tuple[Span, ...]:
        spans: list[Span] = []
        for line in self._spool_lines():
            span = _span_from_line(line, cutoff)
            if span is not None:
                spans.append(span)
        return tuple(spans)

    # --- PlatformAdapter -----------------------------------------------------------------

    def list_tasks(self) -> Sequence[Task]:
        return self.snapshot(datetime.now(UTC)).tasks

    def get_task(self, task_id: str) -> Task | None:
        return next((t for t in self.list_tasks() if t.id == task_id), None)

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return ()

    def requeue(self, task_id: str, reason: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def unblock(self, task_id: str, reason: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def comment(self, task_id: str, body: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def wip_commit(self, task_id: str, message: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)


def _span_from_line(line: str, cutoff: datetime) -> Span | None:
    """One spool line to a tool span; malformed or foreign lines give ``None``."""
    try:
        rec = json.loads(line)
        if not isinstance(rec, dict) or rec.get("type") != "tool":
            return None
        end = _utc(rec["at"])
        if end is None or end < cutoff:
            return None
        duration = rec.get("duration_ms")
        millis = duration if isinstance(duration, int | float) and duration >= 0 else 0
        run_id = rec.get("run_id")
        task_id = rec.get("task_id")
        tool = rec.get("tool")
        attributes: dict[str, Any] = {}
        if isinstance(rec.get("command"), str):
            attributes["command"] = rec["command"]
        digest = hashlib.sha256(line.strip().encode("utf-8", "replace")).hexdigest()[:16]
        return Span(
            trace_id=f"hermes-run-{run_id}" if run_id else f"hermes-task-{task_id or 'none'}",
            span_id=f"hermes-tool-{digest}",
            name=f"tool {tool}" if tool else "tool",
            kind=SpanKind.TOOL,
            start=end - timedelta(milliseconds=millis),
            end=end,
            task_id=str(task_id) if task_id else None,
            session_id=f"hermes-run-{run_id}" if run_id else None,
            tool=str(tool) if tool else None,
            args_hash=str(rec["args_hash"]) if rec.get("args_hash") else None,
            outcome=str(rec["outcome"]) if rec.get("outcome") else None,
            attributes=attributes,
        )
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


class _Connection:
    """``sqlite3.Connection`` that is closed (not just committed) by ``with``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)
