"""A fake ``hermes`` executable: records every call and applies the kanban commands Kelpie
may run to the simulated board, the way Hermes' ``kanban_db`` does.

Kelpie runs the CLI with an allow-listed environment (no ``PYTHONPATH``), so the farm writes
a small launcher script with the interpreter, the path of the ``kelpie`` package and the call
log baked in (:func:`install`).

Every call is appended as a JSON line to the call log (``hermes-calls.jsonl`` next to the
board), whatever its environment and even if the command crashes: the arguments, the names
of the environment variables the command got (never their values), the board it was pointed
at and the exit code.

Supported commands (anything else exits 2 and is recorded):

- ``kanban block <task> <reason> [--kind K]`` (options anywhere, ``--`` ends them): records
  ``BLOCKED: <reason>`` as a comment first (as Hermes does, so a lost race leaves a stray
  comment), then in one transaction: the task must be ``running`` or ``ready``; when
  ``$HERMES_KANBAN_TASK`` names this task, ``$HERMES_KANBAN_RUN_ID`` pins the run (Hermes'
  ``_worker_run_id_for`` passes it as ``expected_run_id``), so the block fails if that run is
  no longer the task's current run; the task becomes ``blocked`` with ``block_kind`` K, its
  claim is released, the run ends with outcome ``blocked`` and a ``blocked`` event carries
  the kind and the reason.
- ``kanban unblock <task>``: a ``blocked`` task goes back to ``ready``; an ``unblocked``
  event is recorded.

A file ``hermes-fail`` next to the board makes every command fail (exit 1), for scenarios
where Hermes refuses. With a clock file (:func:`install`), times written to the board come
from it (epoch seconds) instead of the wall clock: the demo runs on a virtual clock.
"""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

CALLS = "hermes-calls.jsonl"
BLOCK_KINDS = ("needs_input", "capability", "transient", "dependency")
FAIL = "hermes-fail"
BOARD_ENV = "HERMES_KANBAN_DB"
TASK_ENV = "HERMES_KANBAN_TASK"
RUN_ENV = "HERMES_KANBAN_RUN_ID"
PACKAGE_ROOT = Path(__file__).resolve().parents[2]  # the directory holding kelpie/


class Refused(Exception):
    """Hermes refuses the command (exit 1)."""


def _author() -> str:
    """Who Hermes signs a block's comment with (``hermes_cli.profiles.current_profile_name``):
    ``$HERMES_PROFILE_NAME``, ``$HERMES_PROFILE``, else the profile of ``HERMES_HOME``."""
    for name in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        if value := os.environ.get(name, "").strip():
            return value
    return "default"


def install(directory: Path, log: Path, clock: Path | None = None) -> Path:
    """Write an executable ``hermes`` launcher into ``directory`` that records its calls in
    ``log``; return its path. It runs this interpreter and imports this ``kelpie`` package,
    installed or not. With ``clock``, the board's times are read from that file."""
    path = directory / "hermes"
    # Appended, not prepended: site-packages must not shadow the standard library. A shell
    # wrapper, not a shebang: an interpreter path may hold spaces or be too long for one.
    clock_arg = repr(str(clock)) if clock is not None else "None"
    code = (
        "import sys; "
        f"sys.path.append({str(PACKAGE_ROOT)!r}); "
        "from kelpie.sim.hermes_cli import main; "
        f"raise SystemExit(main(sys.argv[1:], {str(log)!r}, {clock_arg}))"
    )
    path.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -c {shlex.quote(code)} "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _db() -> sqlite3.Connection:
    board = os.environ.get(BOARD_ENV)
    if not board:
        raise Refused(f"${BOARD_ENV} is not set")
    conn = sqlite3.connect(board, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def _block(args: Sequence[str]) -> None:
    kind = "needs_input"
    positional: list[str] = []
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            positional += rest
            break
        if arg == "--kind":
            if not rest:
                raise Refused("--kind needs a value")
            kind = rest.pop(0)
        elif arg.startswith("--kind="):
            kind = arg.split("=", 1)[1]
        else:
            positional.append(arg)
    if kind not in BLOCK_KINDS:
        raise Refused(f"unknown block kind {kind!r}")
    if len(positional) != 2:
        raise Refused("usage: hermes kanban block <task> <reason> [--kind K]")
    task_id, reason = positional
    # As Hermes' _worker_run_id_for: the run is pinned only for the worker's own task.
    env_run = os.environ.get(RUN_ENV)
    pinned = os.environ.get(TASK_ENV) == task_id and bool(env_run)
    if pinned and not str(env_run).isdigit():
        raise Refused(f"${RUN_ENV} is not a run id")
    expected = int(str(env_run)) if pinned else None
    now = int(_now())
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, _author(), f"BLOCKED: {reason}", now),
        )
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise Refused(f"no such task: {task_id}")
        if row["status"] not in ("running", "ready"):
            raise Refused(f"task {task_id} is {row['status']}, not running or ready")
        if expected is not None and row["current_run_id"] != expected:
            raise Refused(f"task {task_id} is not on run {expected}")
        rid = row["current_run_id"]
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = ?, current_run_id = NULL,"
            " claim_expires = NULL WHERE id = ?",
            (kind, task_id),
        )
        if rid is not None:
            conn.execute(
                "UPDATE task_runs SET status = 'blocked', ended_at = ?, outcome = 'blocked'"
                " WHERE id = ? AND ended_at IS NULL",
                (now, rid),
            )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
            " VALUES (?, ?, 'blocked', ?, ?)",
            (task_id, rid, json.dumps({"kind": kind, "reason": reason}), now),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _unblock(args: Sequence[str]) -> None:
    if len(args) != 1:
        raise Refused("usage: hermes kanban unblock <task>")
    (task_id,) = args
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise Refused(f"no such task: {task_id}")
        if row["status"] != "blocked":
            raise Refused(f"task {task_id} is {row['status']}, not blocked")
        conn.execute(
            "UPDATE tasks SET status = 'ready', block_kind = NULL WHERE id = ?", (task_id,)
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at)"
            " VALUES (?, 'unblocked', NULL, ?)",
            (task_id, int(_now())),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def run(argv: Sequence[str]) -> int:
    board = os.environ.get(BOARD_ENV)
    if board and (Path(board).parent / FAIL).exists():
        print("hermes: simulated failure", file=sys.stderr)
        return 1
    match list(argv):
        case ["kanban", "block", *rest]:
            _block(rest)
        case ["kanban", "unblock", *rest]:
            _unblock(rest)
        case _:
            print(f"hermes: unsupported command: {' '.join(argv)}", file=sys.stderr)
            return 2
    return 0


_clock: Path | None = None  # set by main()


def _now() -> float:
    """The board's clock: the clock file when there is one, else the wall clock."""
    if _clock is not None:
        return float(_clock.read_text(encoding="utf-8").strip())
    return time.time()


def _log_time() -> float:
    """When the call happened, for the call log: a broken clock file must not lose the call
    (the command itself already failed on it and is recorded with its exit code)."""
    try:
        return _now()
    except (OSError, ValueError):
        return time.time()


def main(argv: Sequence[str], log: str | None = None, clock: str | None = None) -> int:
    global _clock
    _clock = Path(clock) if clock is not None else None
    error = None
    try:
        code = run(argv)
    except Refused as exc:
        print(f"hermes: {exc}", file=sys.stderr)
        code = 1
    except sqlite3.Error as exc:
        print(f"hermes: database error: {exc}", file=sys.stderr)
        code = 1
    except Exception as exc:  # a crash is recorded too: the call happened
        print(f"hermes: crashed: {exc!r}", file=sys.stderr)
        code, error = 3, repr(exc)
    if log is not None:
        record = {
            "argv": list(argv),
            "env": sorted(os.environ),
            "task": os.environ.get(TASK_ENV),
            "run": os.environ.get(RUN_ENV),
            "board": os.environ.get(BOARD_ENV),
            "code": code,
            "error": error,
            "at": _log_time(),
        }
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return code


def calls(path: Path) -> list[dict[str, object]]:
    """The calls recorded in the log at ``path``, oldest first."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
