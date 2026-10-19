"""A fake ``hermes`` for ``kelpie selftest stop``: just enough of the CLI, the dispatcher and a
worker to run the self-test without Hermes.

``dispatch`` starts what Hermes would: a submitter (recorded as ``task_runs.worker_pid`` with
Hermes' fingerprint) and an agent (recorded in a ``worker_bound`` event) that asks the model
in the profile's ``config.yaml`` for a tool call, starts it as a child of its own the first
time (a tool command), writes each call to ``$KELPIE_HERMES_SPOOL`` as Kelpie's plugin would
and heartbeats. ``kanban block`` is the simulator's (:mod:`kelpie.sim.hermes_cli`).

A file ``mode`` next to the executable (its directory is ``BIN_DIR``) changes the behaviour:
``nonspawnable`` (dispatch skips the task), ``crash`` (the run ends crashed before an agent
takes it), ``author`` (blocks are signed ``default``, as a Hermes that ignores
``HERMES_PROFILE_NAME``), ``deaf`` (the worker and its tool command ignore SIGTERM).
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from kelpie.adapters.hermes.worker import fingerprint, instantiation_epoch, read_proc
from kelpie.sim import hermes_cli
from kelpie.sim.board import KANBAN_SCHEMA

HOME = Path(os.environ.get("HERMES_HOME", "/nonexistent"))
# The launcher passes the directory of the executable (runpy init_globals), where the mode is.
BIN_DIR = str(globals().get("BIN_DIR") or Path(__file__).parent)
MODE_FILE = Path(BIN_DIR) / "mode"
DEAF = "import signal; signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"


def mode() -> str:
    try:
        return MODE_FILE.read_text().strip()
    except OSError:
        return ""


def db_of(board: str) -> Path:
    return HOME / "kanban" / "boards" / board / "kanban.db"


def connect(board: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_of(board), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def spawn(code: str) -> subprocess.Popen[bytes]:
    prefix = DEAF if mode() == "deaf" else ""
    return subprocess.Popen(
        [sys.executable, "-c", prefix + code],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=dict(os.environ),
    )


def print_of(pid: int) -> str:
    for _ in range(100):
        if (p := read_proc(pid)) is not None:
            return fingerprint(p, instantiation_epoch())
        time.sleep(0.01)
    raise SystemExit(f"no /proc entry for {pid}")


def dispatch(board: str) -> int:
    conn = connect(board)
    row = conn.execute("SELECT id, assignee FROM tasks WHERE status = 'ready'").fetchone()
    out: dict[str, list[object]] = {"spawned": [], "skipped_nonspawnable": []}
    if row is None:
        print(json.dumps(out))
        return 0
    if mode() == "nonspawnable":
        out["skipped_nonspawnable"].append(row["id"])
        print(json.dumps(out))
        return 0
    task, profile = row["id"], row["assignee"]
    now = int(time.time())
    lock = f"{socket.gethostname()}:{os.getpid()}"
    rid = conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, last_heartbeat_at,"
        " claim_lock) VALUES (?, ?, 'running', ?, ?, ?)",
        (task, profile, now, now, lock),
    ).lastrowid
    conn.execute(
        "UPDATE tasks SET status = 'running', current_run_id = ?, started_at = ?,"
        " last_heartbeat_at = ? WHERE id = ?",
        (rid, now, now, task),
    )
    if mode() == "crash":  # the worker died before an agent took the run
        conn.execute("UPDATE task_runs SET status = 'crashed', error = 'boom' WHERE id = ?", (rid,))
        out["spawned"].append({"task_id": task, "assignee": profile})
        print(json.dumps(out))
        return 0
    submitter = spawn("import time; time.sleep(600)")
    conn.execute(
        "UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
        (submitter.pid, print_of(submitter.pid), rid),
    )
    config = (HOME / "profiles" / profile / "config.yaml").read_text()
    base_url = re.search(r"base_url: (\S+)", config)
    assert base_url is not None
    agent = spawn(
        f"import sys; sys.argv = ['agent', {board!r}, {task!r}, '{rid}', {base_url[1]!r}]\n"
        f"import runpy; runpy.run_path({__file__!r}, {{'BIN_DIR': {BIN_DIR!r}}}, 'agent')"
    )
    payload = {"pid": agent.pid, "claim_lock": lock, "started_at": print_of(agent.pid)}
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
        " VALUES (?, ?, 'worker_bound', ?, ?)",
        (task, rid, json.dumps(payload), now),
    )
    out["spawned"].append({"task_id": task, "assignee": profile})
    print(json.dumps(out))
    return 0


def agent(board: str, task: str, rid: int, base_url: str) -> None:
    conn = connect(board)
    spool = Path(os.environ["KELPIE_HERMES_SPOOL"])
    tool: subprocess.Popen[bytes] | None = None
    for n in range(10_000):
        body = json.dumps({"model": "m", "messages": [], "tools": [{"type": "function"}]})
        request = urllib.request.Request(
            f"{base_url}/chat/completions", body.encode(), {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            call = json.load(response)["choices"][0]["message"]["tool_calls"][0]
        if tool is None:
            tool = spawn("import time; time.sleep(600)")
        now = time.time()
        line = {
            "type": "tool",
            "at": now,
            "task_id": task,
            "run_id": str(rid),
            "session_id": "fake-session",
            "tool": call["function"]["name"],
            "tool_call_id": f"call_{n}",
            "args_hash": "0123456789abcdef",
            "command": "sleep *** *** ***",
            "outcome": "ok",
            "duration_ms": 10,
        }
        with spool.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (int(now), task))
        conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (int(now), rid))
        time.sleep(0.1)


def main(argv: list[str]) -> int:
    if argv == ["--version"]:
        print("Hermes Agent fake (selftest)")
        return 0
    if argv[:2] == ["profile", "create"]:
        (HOME / "profiles" / argv[-1]).mkdir(parents=True)
        return 0
    if argv[:2] == ["gateway", "stop"]:
        return 0
    if argv[:3] == ["kanban", "boards", "create"]:
        db = db_of(argv[3])
        db.parent.mkdir(parents=True)
        sqlite3.connect(db).executescript(KANBAN_SCHEMA)
        return 0
    if argv[:2] == ["kanban", "--board"]:
        board, verb, rest = argv[2], argv[3], argv[4:]
        if verb == "create":
            conn = connect(board)
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at)"
                " VALUES ('t_1', ?, ?, 'ready', ?)",
                (rest[0], option(rest, "--assignee"), int(time.time())),
            )
            print(json.dumps({"id": "t_1", "status": "ready"}, indent=2))
            return 0
        if verb == "dispatch":
            return dispatch(board)
    if argv[:2] == ["kanban", "block"]:
        if mode() == "author":
            os.environ["HERMES_PROFILE_NAME"] = "default"
        return hermes_cli.main(argv)
    print(f"fake hermes: unsupported {argv}", file=sys.stderr)
    return 2


if __name__ == "agent":
    agent(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4])
elif __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
