"""Hermes ``stop``: the task is blocked through the Hermes CLI, never by Kelpie itself.

The CLI is a fake executable that does to a synthetic ``kanban.db`` what ``hermes kanban
block`` does (running -> blocked, run ended) and records its arguments and environment.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes import adapter as adapter_module
from kelpie.adapters.hermes.adapter import HermesError
from kelpie.adapters.hermes.park import load_key
from kelpie.adapters.hermes.stop import (
    MAX_REASON,
    HermesCliError,
    block_task,
    cli_env,
    has_stop_marker,
    run_id,
    stop_comment,
    verify_stop,
)
from kelpie.adapters.hermes.worker import fingerprint, instantiation_epoch, read_proc
from kelpie.core.actions import Action, ActionKind, ActionResult
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.ports import SupportsStop
from tests.adapters.kanban import Board
from tests.fakes import Emit

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)

FAKE_CLI = """\
#!{python}
import json, os, sqlite3, subprocess, sys, time

with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
try:  # the test sets the behaviour in a file: the CLI's environment is filtered
    mode = open({log!r} + ".mode").read().strip()
except OSError:
    mode = "block"
task = sys.argv[sys.argv.index("--") + 1]
# As hermes_cli/kanban.py _worker_run_id_for: the run is only pinned for the worker's own task.
raw = os.environ.get("HERMES_KANBAN_RUN_ID")
expected = int(raw) if os.environ.get("HERMES_KANBAN_TASK") == task and raw else None
conn = sqlite3.connect(os.environ["HERMES_KANBAN_DB"])
now = int(time.time())
if mode == "finish-then-fail":  # the worker completed first; Hermes refuses the block
    conn.execute("UPDATE tasks SET status='done'")
    conn.execute("UPDATE task_runs SET ended_at=1, outcome='completed'")
    conn.commit()
    mode = "fail"
if mode == "reclaimed":  # the task was re-claimed (a new run) before this command ran
    conn.execute("UPDATE task_runs SET ended_at=?, outcome='reclaimed' WHERE task_id=?"
                 " AND ended_at IS NULL", (now, task))
    cur = conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at)"
                       " VALUES (?, 'coder', 'running', ?)", (task, now))
    conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (cur.lastrowid, task))
    conn.commit()
if mode == "fail":
    print("cannot block t1 (token sk-secret-value)", file=sys.stderr)
    sys.exit(1)
if mode == "hang":
    child = subprocess.Popen(["sleep", "30"])
    with open({log!r} + ".pid", "w") as fh:
        fh.write(str(child.pid))
    time.sleep(30)
if mode == "noop":
    sys.exit(0)
sql = "UPDATE tasks SET status='blocked', block_kind='needs_input'" \
      " WHERE id=? AND status IN ('running', 'ready')"
params = [task]
if expected is not None:
    sql += " AND current_run_id=?"
    params.append(expected)
if conn.execute(sql, params).rowcount != 1:
    print("cannot block " + task, file=sys.stderr)
    sys.exit(1)
conn.execute("UPDATE task_runs SET status='blocked', outcome='blocked', ended_at=?"
             " WHERE task_id=? AND ended_at IS NULL", (now, task))
conn.commit()
"""


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


@pytest.fixture
def fake_cli(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "cli.jsonl"
    exe = tmp_path / "bin" / "hermes"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLI.format(python=sys.executable, log=str(log)))
    exe.chmod(0o755)
    return str(exe), log


def set_mode(log: Path, mode: str) -> None:
    Path(f"{log}.mode").write_text(mode)


def calls(log: Path) -> list[dict[str, object]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def running(board: Board, task: str = "t1") -> int:
    started = NOW - timedelta(minutes=20)
    board.task(task, "running", started - timedelta(hours=1), assignee="coder", started=started)
    rid = board.run(task, "coder", started, heartbeat=NOW)
    board.update(task, current_run_id=rid)
    return rid


def make(board: Board, cli: str, **kw: object) -> HermesAdapter:
    kw.setdefault("wait_key", board.path.parent / "wait.key")
    return HermesAdapter(db_path=board.path, spool=None, cli=cli, **kw)  # type: ignore[arg-type]


def task_state(board: Board, task: str = "t1") -> str:
    return board.conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0]


def test_implements_supports_stop(board: Board) -> None:
    assert isinstance(make(board, "hermes"), SupportsStop)


def test_stop_blocks_the_task_through_the_cli(board: Board, fake_cli: tuple[str, Path]) -> None:
    exe, log = fake_cli
    rid = running(board)
    make(board, exe).stop(f"hermes-run-{rid}", "same call 12 times")
    assert task_state(board) == "blocked"
    (call,) = calls(log)
    argv = call["argv"]
    assert argv[:6] == ["kanban", "block", "--kind", "needs_input", "--", "t1"]  # type: ignore[index]
    comment = argv[6]  # type: ignore[index]
    assert "same call 12 times" in comment
    assert "hermes kanban unblock t1" in comment  # how to resume
    env = call["env"]
    assert env["HERMES_KANBAN_DB"] == str(board.path)  # type: ignore[index]
    # Pinned to exactly this task and run (Hermes then guards on current_run_id).
    assert env["HERMES_KANBAN_TASK"] == "t1" and env["HERMES_KANBAN_RUN_ID"] == str(rid)  # type: ignore[index]


def test_stop_is_idempotent(board: Board, fake_cli: tuple[str, Path]) -> None:
    exe, log = fake_cli
    rid = running(board)
    adapter = make(board, exe)
    adapter.stop(f"hermes-run-{rid}", "loop")
    adapter.stop(f"hermes-run-{rid}", "loop")
    assert len(calls(log)) == 1
    assert task_state(board) == "blocked"


def test_finished_run_and_other_status_are_left_alone(
    board: Board, fake_cli: tuple[str, Path]
) -> None:
    exe, log = fake_cli
    board.task("done1", "done", NOW - timedelta(hours=3), completed=NOW)
    ended = board.run("done1", "coder", NOW - timedelta(hours=2), ended=NOW, outcome="completed")
    board.task("old", "running", NOW - timedelta(hours=3), started=NOW - timedelta(hours=2))
    old = board.run("old", "coder", NOW - timedelta(hours=2), ended=NOW, outcome="crashed")
    newer = board.run("old", "coder", NOW - timedelta(hours=1))
    board.update("old", current_run_id=newer)
    adapter = make(board, exe)
    adapter.stop(f"hermes-run-{ended}", "x")
    adapter.stop(f"hermes-run-{old}", "x")  # superseded run of a task that still runs
    assert calls(log) == []
    assert task_state(board, "old") == "running"


@pytest.mark.parametrize("session", ["hermes-run-abc", "claude-1", "hermes-run-", "hermes-run--4"])
def test_foreign_session_id_is_rejected(board: Board, session: str) -> None:
    with pytest.raises(HermesError, match="not a Hermes run session"):
        make(board, "hermes").stop(session, "x")


def test_unknown_run_is_rejected(board: Board) -> None:
    with pytest.raises(HermesError, match="unknown Hermes run"):
        make(board, "hermes").stop("hermes-run-999", "x")


def test_missing_cli_is_an_error_and_changes_nothing(board: Board, tmp_path: Path) -> None:
    rid = running(board)
    with pytest.raises(HermesError, match="Hermes CLI not found"):
        make(board, str(tmp_path / "nope")).stop(f"hermes-run-{rid}", "x")
    assert task_state(board) == "running"


def test_cli_failure_is_an_error(
    board: Board, fake_cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, log = fake_cli
    set_mode(log, "fail")
    rid = running(board)
    with pytest.raises(HermesError, match=r"failed \(exit 1\)"):
        make(board, exe).stop(f"hermes-run-{rid}", "x")
    assert task_state(board) == "running"


def test_cli_failure_after_the_run_ended_by_itself_is_a_noop(
    board: Board, fake_cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, log = fake_cli
    set_mode(log, "finish-then-fail")
    rid = running(board)
    make(board, exe).stop(f"hermes-run-{rid}", "x")  # no error: the run is over
    assert len(calls(log)) == 1 and task_state(board) == "done"


def test_task_still_running_after_cli_success_is_an_error(
    board: Board, fake_cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, log = fake_cli
    set_mode(log, "noop")
    rid = running(board)
    with pytest.raises(HermesError, match="still running"):
        make(board, exe).stop(f"hermes-run-{rid}", "x")


def pid_alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"  # a zombie is dead, only not reaped yet


def test_cli_timeout_kills_the_whole_process_group(
    fake_cli: tuple[str, Path], tmp_path: Path
) -> None:
    exe, log = fake_cli
    set_mode(log, "hang")
    pidfile = Path(f"{log}.pid")
    env = dict(os.environ)
    with pytest.raises(HermesCliError, match="timed out"):
        block_task(exe, tmp_path / "k.db", "t1", 1, "c", timeout=1.5, environ=env)
    grandchild = int(pidfile.read_text())  # started by the CLI, a dedicated test process
    for _ in range(50):
        if not pid_alive(grandchild):
            break
        time.sleep(0.1)
    assert not pid_alive(grandchild)


def test_env_is_an_allow_list_with_board_and_run_pinned(tmp_path: Path) -> None:
    base = {
        "PATH": "/bin",
        "HOME": "/home/k",
        "USER": "k",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "TMPDIR": "/tmp",
        "XDG_CONFIG_HOME": "/cfg",
        "VIRTUAL_ENV": "/venv",
        "HERMES_HOME": "/profile",
        "HERMES_PROFILE": "coder",
        "HERMES_KANBAN_TASK": "t9",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_DB": "/elsewhere/kanban.db",
        "HERMES_KANBAN_BOARD": "other",
        "HERMES_KANBAN_HOME": "/other-home",
        "HERMES_DELEGATED_CHILD_CONTEXT": "1",
        "GITHUB_TOKEN": "ghp_secret",
        "KELPIE_TRACES_TOKEN": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "SLACK_WEBHOOK_URL": "secret",
    }
    env = cli_env(base, tmp_path / "kanban.db", "t1", 5)
    assert env == {
        "PATH": "/bin",
        "HOME": "/home/k",
        "USER": "k",
        "LANG": "C.UTF-8",
        "LC_ALL": "C",
        "TMPDIR": "/tmp",
        "XDG_CONFIG_HOME": "/cfg",
        "VIRTUAL_ENV": "/venv",
        "HERMES_HOME": "/profile",
        "HERMES_PROFILE": "coder",
        "HERMES_KANBAN_DB": str(tmp_path / "kanban.db"),
        "HERMES_KANBAN_TASK": "t1",
        "HERMES_KANBAN_RUN_ID": "5",
    }


def test_kelpie_credentials_do_not_reach_the_cli(
    board: Board, fake_cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, log = fake_cli
    for name in ("GITHUB_TOKEN", "KELPIE_TRACES_TOKEN", "SLACK_BOT_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.setenv(name, "hunter2-secret")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "elsewhere")
    monkeypatch.setenv("HERMES_HOME", "/profile")
    rid = running(board)
    make(board, exe).stop(f"hermes-run-{rid}", "loop")
    (call,) = calls(log)
    env = call["env"]
    assert "hunter2-secret" not in json.dumps(env)
    assert "HERMES_KANBAN_BOARD" not in env and env["HERMES_HOME"] == "/profile"  # type: ignore[operator]


def test_run_re_claimed_before_the_command_is_not_blocked(
    board: Board, fake_cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes guards on current_run_id: a new run of the same task survives a stale stop."""
    exe, log = fake_cli
    set_mode(log, "reclaimed")
    rid = running(board)
    make(board, exe).stop(f"hermes-run-{rid}", "loop")  # old run is over: no error
    assert len(calls(log)) == 1
    assert task_state(board) == "running"
    row = board.conn.execute("SELECT current_run_id FROM tasks WHERE id='t1'").fetchone()
    assert row[0] != rid  # the new run is the one running, untouched


def test_comment_is_one_clean_bounded_line() -> None:
    text = stop_comment("t1", "line one\nline two\x00\x1b[31m" + "x" * 2000)
    assert "\n" not in text and "\x00" not in text and "\x1b" not in text
    assert text.startswith("Stopped by Kelpie: line one line two")
    assert "..." in text and len(text) < MAX_REASON + 200
    assert "no reason given" in stop_comment("t1", "  \n ")
    assert stop_comment("t1", "Runaway run.").startswith("Stopped by Kelpie: Runaway run. The")


def test_run_id_parsing() -> None:
    assert run_id("hermes-run-12") == 12
    assert run_id("hermes-run-0012") == 12
    for bad in ("", "hermes-run-", "hermes-run-1a", "hermes-run-١٢", "x-hermes-run-1"):
        assert run_id(bad) is None


def test_cli_error_output_is_bounded(
    fake_cli: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, log = fake_cli
    set_mode(log, "fail")
    with pytest.raises(HermesCliError) as info:
        block_task(exe, tmp_path / "k.db", "t1", 1, "c")
    assert len(str(info.value)) < 500


def test_engine_dispatches_stop_to_hermes(board: Board, fake_cli: tuple[str, Path]) -> None:
    exe, log = fake_cli
    rid = running(board)
    adapter = make(board, exe)
    action = Action(
        ActionKind.STOP,
        "loop_breaker",
        "same call 12 times",
        executor="hermes",
        params={"session_id": f"hermes-run-{rid}"},
    )

    engine = Engine(
        [Emit([action], name="loop_breaker")],
        adapters=[adapter],
        config=EngineConfig(dry_run=False, modes={"loop_breaker": Mode.ACT}),
    )
    results = engine.tick(lambda: adapter.snapshot(NOW))
    assert [r.outcome for r in results] == [Outcome.EXECUTED]
    assert task_state(board) == "blocked"
    assert len(calls(log)) == 1


def sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def record_worker(board: Board, rid: int, pid: int) -> None:
    """The run's submitter as Hermes records it: pid, start fingerprint, this host's claim."""
    p = read_proc(pid)
    assert p is not None
    board.conn.execute(
        "UPDATE task_runs SET worker_pid = ?, worker_started_at = ?, claim_lock = ? WHERE id = ?",
        (pid, fingerprint(p, instantiation_epoch()), f"{socket.gethostname()}:1", rid),
    )
    board.conn.commit()


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_stop_ends_the_runs_worker(board: Board, fake_cli: tuple[str, Path]) -> None:
    exe, _ = fake_cli
    rid = running(board)
    child = sleeper()
    try:
        record_worker(board, rid, child.pid)
        done = make(board, exe, worker_grace=5.0).stop(f"hermes-run-{rid}", "loop")
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert isinstance(done, str) and "blocked for a human" in done
        assert re.search(r"its worker ended \d+\.\ds after SIGTERM$", done)
    finally:
        child.kill()
        child.wait(timeout=10)


def test_with_end_worker_off_the_worker_is_left_to_hermes(
    board: Board, fake_cli: tuple[str, Path]
) -> None:
    exe, _ = fake_cli
    rid = running(board)
    child = sleeper()
    try:
        record_worker(board, rid, child.pid)
        done = make(board, exe, end_worker=False).stop(f"hermes-run-{rid}", "loop")
        assert child.poll() is None
        os.kill(child.pid, 0)
        assert isinstance(done, str) and done.endswith("(end_worker = false)")
    finally:
        child.kill()
        child.wait(timeout=10)


def test_stop_says_what_it_did_or_that_there_was_nothing_to_do(
    board: Board, fake_cli: tuple[str, Path]
) -> None:
    exe, _ = fake_cli
    rid = running(board)  # Hermes recorded no worker process for this run
    adapter = make(board, exe)
    done = adapter.stop(f"hermes-run-{rid}", "loop")
    assert done == (
        "task t1 is blocked for a human (hermes kanban unblock t1); Kelpie did not end its "
        "worker (the board records no worker process for the run)"
    )
    assert adapter.stop(f"hermes-run-{rid}", "loop") is ActionResult.NOTHING_TO_DO  # over now


def test_a_failure_ending_the_worker_does_not_fail_the_stop(
    board: Board, fake_cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block already happened: the stop is done, and says the worker was not ended."""
    exe, _ = fake_cli
    rid = running(board)
    board.conn.execute(
        "UPDATE task_runs SET worker_pid = 99999999, worker_started_at = 'e|1',"
        " claim_lock = ? WHERE id = ?",
        (f"{socket.gethostname()}:1", rid),
    )
    board.conn.commit()

    def broken(*args: object, **kw: object) -> object:
        raise PermissionError("/proc")

    monkeypatch.setattr(adapter_module, "worker_targets", broken)
    done = make(board, exe).stop(f"hermes-run-{rid}", "loop")
    assert task_state(board) == "blocked"
    assert isinstance(done, str) and done.endswith(
        "Kelpie did not end its worker (ending it failed: PermissionError)"
    )


def test_an_unreadable_or_older_board_says_so(
    board: Board, fake_cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, _ = fake_cli
    adapter = make(board, exe)
    monkeypatch.setattr(adapter_module, "run_worker", lambda conn, rid: None)
    assert adapter._end_worker(1) == (
        "Kelpie did not end its worker (this Hermes records no worker processes on the board)"
    )

    def unreadable(conn: object, rid: int) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(adapter_module, "run_worker", unreadable)
    assert adapter._end_worker(1) == (
        "Kelpie did not end its worker (Kelpie could not read the run's worker from the board)"
    )


def test_a_refused_stop_has_a_reason_safe_to_show(board: Board, fake_cli: tuple[str, Path]) -> None:
    exe, log = fake_cli
    set_mode(log, "fail")  # the CLI prints a token on stderr
    rid = running(board)
    with pytest.raises(HermesError) as info:
        make(board, exe).stop(f"hermes-run-{rid}", "x")
    assert "sk-secret-value" in str(info.value)  # the log keeps the detail...
    assert info.value.public_reason == "hermes kanban block failed (exit 1)"  # ...chat does not
    with pytest.raises(HermesError) as unknown:
        make(board, exe).stop("hermes-run-999", "x")
    assert unknown.value.public_reason == "the run is not on the board"


def test_the_stop_is_signed(board: Board, fake_cli: tuple[str, Path]) -> None:
    """Issue 141: the block carries Kelpie's marker, so it reads back as Kelpie's own stop."""
    exe, log = fake_cli
    rid = running(board)
    make(board, exe).stop(f"hermes-run-{rid}", "same call 12 times")
    comment = str(calls(log)[0]["argv"][6])  # type: ignore[index]
    key = load_key(board.path.parent / "wait.key", create=False)
    assert key is not None and has_stop_marker(comment)
    assert verify_stop(comment, "t1", key, datetime.now(UTC), timedelta(seconds=10))


def test_without_a_usable_key_the_run_is_still_stopped(
    board: Board, fake_cli: tuple[str, Path], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    exe, log = fake_cli
    rid = running(board)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    with caplog.at_level("WARNING"):
        make(board, exe, wait_key=blocker / "wait.key").stop(f"hermes-run-{rid}", "loop")
    assert task_state(board) == "blocked"
    assert not has_stop_marker(str(calls(log)[0]["argv"][6]))  # type: ignore[index]
    assert "stopping without a signed marker" in caplog.text
