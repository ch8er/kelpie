"""``kelpie snooze``, ``kelpie unsnooze`` and ``kelpie snoozes``: acknowledge a deliberate wait.

A person who knows a task waits on purpose (a decision deferred to a release, a manual pass
planned for a date) snoozes it until a date. The snooze is stored in Kelpie's database, one
per task (a new one replaces the old), and ``kelpie serve`` puts it into every snapshot;
rules decide what a snooze changes. ``until:`` markers in block reasons are read from the
board instead and are not listed here (``kelpie why <task>`` shows both).

The task need not be on the board, which may be offline: when the local adapters can be
read cheaply (not a hub, no issue tracker read over the network), an unknown task is
warned about and snoozed anyway, and the number of tasks waiting behind a known one is kept
with the snooze. Exit codes: 0; 1 when ``unsnooze`` finds no snooze for the task or the
database cannot be written or read; 2 for a config error, a bad ``--until``, no database
yet or ``[storage] path = ":memory:"``. ``kelpie snoozes`` only reads (it never upgrades the
database).
"""

from __future__ import annotations

import getpass
import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from kelpie.app import Settings, board_only, build_adapters, utc_now
from kelpie.core.model import Snapshot, Snooze
from kelpie.core.ports import SnapshotSource
from kelpie.core.snooze import format_when, is_date, parse_until, snooze_json, until_problem
from kelpie.core.storage import SchemaOutdated, SqliteLedgerStore, open_readonly, read_snoozes
from kelpie.rules._chains import BlockerGraph

SCHEMA_VERSION = 1  # of the --json object (fields are only ever added within a version)
PREDATES = "no snoozes (the database predates snoozes; kelpie serve upgrades it)"


def default_by() -> str | None:
    """The OS user running the command, or None when it cannot be told."""
    try:
        return getpass.getuser() or None
    except Exception:  # no user name in the environment or the password database
        return None


def local_board(settings: Settings, now: datetime) -> Snapshot | str:
    """The boards of the local adapters that read no network (the issue trackers, GitHub
    issues and Linear, are left out), each read board-only, or why there is none: a hub
    (the boards are the agents'), no such adapter, or one that fails."""
    if settings.hub is not None:
        return "the boards are the agents' ([hub])"
    try:
        adapters = build_adapters(replace(settings, github_issues=None, linear=None))
    except Exception as exc:
        return f"adapters not built: {exc}"
    sources = [board_only(a) for a in adapters]
    sources = [a for a in sources if isinstance(a, SnapshotSource)]
    if not sources:
        return "no local adapter"
    board = Snapshot(now=now)
    for adapter in sources:
        try:
            part = adapter.snapshot(now)
        except Exception as exc:
            return f"board not readable: {exc}"
        board = replace(
            board,
            tasks=board.tasks + part.tasks,
            owners=board.owners + part.owners,
            dependencies=board.dependencies + part.dependencies,
            sessions=board.sessions + part.sessions,
        )
    return board


def _board(settings: Settings, now: datetime) -> Snapshot | None:
    """The whole board, when it can be read cheaply; None when the task may be on a board
    this cannot see (an issue tracker, read over the network) or none could be read."""
    if any(t is not None and t.enabled for t in (settings.github_issues, settings.linear)):
        return None
    board = local_board(settings, now)
    return None if isinstance(board, str) else board


def _database(settings: Settings) -> Path | str:
    """The database snoozes are written to, or why there is none to write to."""
    if settings.db_path is None:
        return "kelpie: snoozes need a database ([storage] path is :memory:)\n"
    if not settings.db_path.exists():
        return f"kelpie: no database at {settings.db_path} yet; run kelpie serve first\n"
    return settings.db_path


def snooze(
    settings: Settings,
    task_id: str,
    until: str,
    reason: str | None = None,
    by: str | None = None,
    clock: Callable[[], datetime] | None = None,
    board: Callable[[Settings, datetime], Snapshot | None] = _board,
) -> tuple[str, str, int]:
    """``kelpie snooze``: stdout, stderr and the exit code."""
    task_id = task_id.strip()
    if not task_id:
        return "", "kelpie: the task id is empty\n", 2
    now = (clock or utc_now)()
    tz = settings.snooze.tz
    try:
        at = parse_until(until, now, tz)
    except ValueError as exc:
        return "", f"kelpie: {exc}\n", 2
    day = is_date(until)
    if (problem := until_problem(at, now, settings.snooze, through_day=day)) is not None:
        shown = f"{until.strip()} (through that day)" if day else format_when(at, tz)
        return "", f"kelpie: --until {shown} {problem}\n", 2
    if isinstance(db := _database(settings), str):
        return "", db, 2
    err = ""
    behind: int | None = None
    snapshot = board(settings, now)
    if snapshot is not None:
        if snapshot.task(task_id) is None:
            err = f"kelpie: warning: {task_id} is not on the board; snoozed anyway\n"
        else:
            behind = len(BlockerGraph(snapshot).behind(task_id))
    record = Snooze(
        task_id=task_id,
        until=at,
        reason=(reason.strip() or None) if reason else None,
        by=(by if by is not None else default_by()),
        created_at=now,
        source="cli",
        behind_at_snooze=behind,
    )
    try:
        store = SqliteLedgerStore(db)
        try:
            replaced = any(s.task_id == task_id for s in store.snoozes())
            store.put_snooze(record)
            store.prune_snoozes(now)
            saved = next((s for s in store.snoozes() if s.task_id == task_id), None)
        finally:
            store.close()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return "", f"{err}kelpie: cannot write to {db}: {exc}\n", 1
    if saved is None:  # another command removed it between the write and the read
        err += f"kelpie: warning: the snooze of {task_id} was removed again meanwhile\n"
        saved = record
    verb = "snooze replaced" if replaced else "snoozed"
    return f"{verb}: {_line(saved, now, settings)}\n", err, 0


def unsnooze(
    settings: Settings, task_id: str, clock: Callable[[], datetime] | None = None
) -> tuple[str, str, int]:
    """``kelpie unsnooze``: stdout, stderr and the exit code (1: the task had no snooze)."""
    if isinstance(db := _database(settings), str):
        return "", db, 2
    try:
        store = SqliteLedgerStore(db)
        try:
            removed = store.delete_snooze(task_id.strip())
            store.prune_snoozes((clock or utc_now)())
        finally:
            store.close()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return "", f"kelpie: cannot write to {db}: {exc}\n", 1
    if not removed:
        return "", f"kelpie: {task_id} has no snooze\n", 1
    return f"unsnoozed {task_id}\n", "", 0


def list_snoozes(
    settings: Settings,
    as_json: bool = False,
    include_expired: bool = False,
    clock: Callable[[], datetime] | None = None,
) -> tuple[str, str, int]:
    """``kelpie snoozes``: the active snoozes, soonest first (``include_expired``: also
    those that expired and are not pruned yet). Read-only."""
    now = (clock or utc_now)()
    if settings.db_path is None:
        return "", "kelpie: snoozes need a database ([storage] path is :memory:)\n", 2
    found: list[Snooze] = []
    note: str | None = None
    if settings.db_path.exists():
        try:
            conn = open_readonly(settings.db_path, 5000)
        except SchemaOutdated:
            note = PREDATES
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            return "", f"kelpie: cannot read the database {settings.db_path}: {exc}\n", 1
        else:
            try:
                found = read_snoozes(conn, None if include_expired else now)
            except sqlite3.Error as exc:
                return "", f"kelpie: cannot read the database {settings.db_path}: {exc}\n", 1
            finally:
                conn.close()
    tz = settings.snooze.tz
    if as_json:
        body: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "snoozes": [snooze_json(s, now, tz) for s in found],
        }
        if note:
            body["note"] = note
        return json.dumps(body, indent=2) + "\n", "", 0
    if note:
        return note + "\n", "", 0
    if not found:
        return ("no snoozes\n" if include_expired else "no active snoozes\n"), "", 0
    return "".join(f"{_line(s, now, settings)}\n" for s in found), "", 0


def _line(s: Snooze, now: datetime, settings: Settings) -> str:
    """``T-101 until 2026-10-26 00:00 UTC (the reason; by alice; 2 task(s) behind it)``."""
    state = "until" if s.active(now) else "expired"
    extra = [" ".join(s.reason.split())] if s.reason else []
    if s.by:
        extra.append(f"by {s.by}")
    if s.source != "cli":
        extra.append(f"from {s.source}")
    if s.behind_at_snooze:
        extra.append(f"{s.behind_at_snooze} task(s) behind it")
    detail = f" ({'; '.join(extra)})" if extra else ""
    return f"{s.task_id} {state} {format_when(s.until, settings.snooze.tz)}{detail}"
