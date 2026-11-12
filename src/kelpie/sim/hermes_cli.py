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
- ``kanban unblock [--reason R] [--] <task>``: refused with ``$HERMES_KANBAN_TASK`` set
  (orchestrator-only); with a reason, ``UNBLOCK: <reason>`` is recorded as a comment first;
  a ``blocked`` or ``scheduled`` task goes back to ``ready`` (Hermes resumes a task blocked
  in review to ``review``, and lands it in ``todo`` while a parent is open; not simulated);
  an ``unblocked`` event is recorded.
- ``kanban assign [--] <task> <profile>``: refused while the task is ``running`` with a
  claimed run; sets the (lowercased) assignee, records an ``assigned`` event. ``none``,
  ``-`` and ``null`` unassign.
- ``kanban comment [--author A] [--] <task> <text...>``: records the comment.
- ``kanban create [--body B] [--assignee P] [--idempotency-key K] [--json] [--] <title>``:
  a new ``ready`` task (``t_`` and a counter), assigned to P (lowercased), with a
  ``created`` event carrying the key; a key used before prints that task again. Prints
  ``{"id": ...}`` (Hermes' real ``--json`` keys are not documented; the adapter reads
  ``id`` or ``task_id``).

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
    ``$HERMES_PROFILE_NAME``, ``$HERMES_PROFILE``, else the profile of ``HERMES_HOME`` (here
    always ``default``)."""
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


def _options(args: Sequence[str], names: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """``--name V``, ``--name=V`` options anywhere (``--`` ends them) and the positionals."""
    found: dict[str, str] = {}
    positional: list[str] = []
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            positional += rest
            break
        name, eq, value = arg.partition("=")
        if name in names:
            if not eq:
                if not rest:
                    raise Refused(f"{name} needs a value")
                value = rest.pop(0)
            found[name] = value
        elif arg.startswith("--"):
            raise Refused(f"unknown option {arg}")
        else:
            positional.append(arg)
    return found, positional


def _unblock(args: Sequence[str]) -> None:
    if os.environ.get(TASK_ENV):
        raise Refused("kanban unblock is orchestrator-only")
    found, positional = _options(args, ("--reason",))
    if len(positional) != 1:
        raise Refused("usage: hermes kanban unblock [--reason R] <task>")
    (task_id,) = positional
    reason = found.get("--reason", "").strip()
    now = int(_now())
    conn = _db()
    try:
        if reason:
            conn.execute(
                "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
                (task_id, _author(), f"UNBLOCK: {reason}", now),
            )
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise Refused(f"no such task: {task_id}")
        if row["status"] not in ("blocked", "scheduled"):
            raise Refused(f"task {task_id} is {row['status']}, not blocked or scheduled")
        conn.execute(
            "UPDATE tasks SET status = 'ready', current_run_id = NULL WHERE id = ?", (task_id,)
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at)"
            " VALUES (?, 'unblocked', NULL, ?)",
            (task_id, now),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _assign(args: Sequence[str]) -> None:
    _, positional = _options(args, ())
    if len(positional) != 2:
        raise Refused("usage: hermes kanban assign <task> <profile>")
    task_id, raw = positional
    profile = None if raw.lower() in ("none", "-", "null") else raw.lower()
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, current_run_id, assignee FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise Refused(f"no such task: {task_id}")
        if row["status"] == "running" and row["current_run_id"] is not None:
            raise Refused(f"cannot reassign {task_id}: currently running (claimed)")
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (profile, task_id))
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at)"
            " VALUES (?, 'assigned', ?, ?)",
            (task_id, json.dumps({"assignee": profile, "from": row["assignee"]}), int(_now())),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _comment(args: Sequence[str]) -> None:
    found, positional = _options(args, ("--author",))
    if len(positional) < 2:
        raise Refused("usage: hermes kanban comment <task> <text...>")
    task_id, *words = positional
    conn = _db()
    try:
        if conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone() is None:
            raise Refused(f"no such task: {task_id}")
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)",
            (task_id, found.get("--author") or _author(), " ".join(words), int(_now())),
        )
    finally:
        conn.close()


def _create(args: Sequence[str]) -> None:
    flags = [a for a in args if a != "--json"]
    found, positional = _options(flags, ("--body", "--assignee", "--idempotency-key"))
    if len(positional) != 1 or not positional[0].strip():
        raise Refused("usage: hermes kanban create <title> [--body B] [--assignee P]")
    key = found.get("--idempotency-key")
    assignee = (found.get("--assignee") or "").lower() or None
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if key:
            for row in conn.execute(
                "SELECT task_id, payload FROM task_events WHERE kind = 'created'"
            ).fetchall():
                payload = json.loads(row["payload"] or "{}")
                if isinstance(payload, dict) and payload.get("idempotency_key") == key:
                    conn.execute("COMMIT")
                    print(json.dumps({"id": row["task_id"], "existing": True}))
                    return
        count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        task_id = f"t_{count + 1:04x}"
        while conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone():
            count += 1
            task_id = f"t_{count + 1:04x}"
        now = int(_now())
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at, created_by)"
            " VALUES (?, ?, ?, ?, 'ready', ?, ?)",
            (task_id, positional[0], found.get("--body"), assignee, now, _author()),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at)"
            " VALUES (?, 'created', ?, ?)",
            (task_id, json.dumps({"idempotency_key": key, "assignee": assignee}), now),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print(json.dumps({"id": task_id, "status": "ready"}))


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
        case ["kanban", "assign", *rest]:
            _assign(rest)
        case ["kanban", "comment", *rest]:
            _comment(rest)
        case ["kanban", "create", *rest]:
            _create(rest)
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
