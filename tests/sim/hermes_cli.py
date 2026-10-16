"""A fake ``hermes`` executable: records every call and applies the kanban commands Kelpie
may run to the simulated board, the way Hermes' ``kanban_db`` does.

Kelpie runs the CLI with an allow-listed environment (no ``PYTHONPATH``), so the farm writes
a small launcher script with this repository's path baked in (:func:`install`).

Calls are appended as JSON lines to ``hermes-calls.jsonl`` next to the board named by
``$HERMES_KANBAN_DB``: the arguments, the names of the environment variables the command got
(never their values) and the exit code.

Supported commands (anything else exits 2 and is recorded):

- ``kanban block [--kind K] [--] <task> <reason>``: records ``BLOCKED: <reason>`` as a
  comment first (as Hermes does, so a lost race leaves a stray comment), then in one
  transaction: the task must be ``running`` and, with ``$HERMES_KANBAN_RUN_ID``, that run
  must be its current run (``expected_run_id``); the task becomes ``blocked`` with
  ``block_kind`` K, its claim is released, the run ends with outcome ``blocked`` and a
  ``blocked`` event carries the kind and the reason.
- ``kanban unblock <task>``: a ``blocked`` task goes back to ``ready``; an ``unblocked``
  event is recorded.

A file ``hermes-fail`` next to the board makes every command fail (exit 1), for scenarios
where Hermes refuses.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Sequence
from pathlib import Path

CALLS = "hermes-calls.jsonl"
FAIL = "hermes-fail"
BOARD_ENV = "HERMES_KANBAN_DB"
TASK_ENV = "HERMES_KANBAN_TASK"
RUN_ENV = "HERMES_KANBAN_RUN_ID"


class Refused(Exception):
    """Hermes refuses the command (exit 1)."""


def install(directory: Path, repo_root: Path) -> Path:
    """Write an executable ``hermes`` launcher into ``directory``; return its path."""
    path = directory / "hermes"
    path.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from tests.sim.hermes_cli import main\n"
        "raise SystemExit(main(sys.argv[1:]))\n",
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
    rest = list(args)
    if rest[:1] == ["--kind"]:
        kind, rest = rest[1], rest[2:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if len(rest) != 2:
        raise Refused("usage: hermes kanban block [--kind K] [--] <task> <reason>")
    task_id, reason = rest
    env_run = os.environ.get(RUN_ENV)
    expected = int(env_run) if env_run else None
    now = int(time.time())
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, "hermes-cli", f"BLOCKED: {reason}", now),
        )
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise Refused(f"no such task: {task_id}")
        if row["status"] != "running":
            raise Refused(f"task {task_id} is {row['status']}, not running")
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
            (task_id, int(time.time())),
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


def main(argv: Sequence[str]) -> int:
    try:
        code = run(argv)
    except Refused as exc:
        print(f"hermes: {exc}", file=sys.stderr)
        code = 1
    except sqlite3.Error as exc:
        print(f"hermes: database error: {exc}", file=sys.stderr)
        code = 1
    board = os.environ.get(BOARD_ENV)
    if board:
        record = {
            "argv": list(argv),
            "env": sorted(os.environ),
            "task": os.environ.get(TASK_ENV),
            "run": os.environ.get(RUN_ENV),
            "board": board,
            "code": code,
            "at": time.time(),
        }
        with (Path(board).parent / CALLS).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    return code


def calls(directory: Path) -> list[dict[str, object]]:
    """The recorded calls of the board in ``directory``, oldest first."""
    path = directory / CALLS
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
