"""Rule 10's real stop path on a multi-board config (issue 333): from a config file with two
``[[adapters.hermes.boards]]``, through the boards adapter the app builds and the engine,
to the fake Hermes CLI and the run's worker processes.

Both boards have a running run 1 with a live worker (run ids restart on every board). Only
the board in the stopped session's prefix may see a block, with its own ``HERMES_KANBAN_DB``
and a marker signed with its own key; the other board's task, run and worker are untouched.
"""

from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.adapters.hermes.park import load_key
from kelpie.adapters.hermes.stop import verify_stop
from kelpie.adapters.hermes.worker import host_table
from kelpie.app import build_adapters, load_rules, load_settings
from kelpie.core.actions import ActionKind
from kelpie.core.config import Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.ports import UnsupportedAction
from tests.adapters.kanban import Board
from tests.adapters.test_hermes_stop import FAKE_CLI

pytestmark = pytest.mark.skipif(
    host_table().unavailable() is not None, reason="needs /proc or libproc"
)

BOARDS = ("qr", "sign")
TASKS = {"qr": "t_q1", "sign": "t_s1"}


def sleeper() -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


@pytest.fixture
def farm(
    tmp_path: Path, tmp_db: Path
) -> Iterator[dict[str, tuple[Board, subprocess.Popen[bytes]]]]:
    """Two boards, each with a running run 1 and its recorded worker."""
    now = datetime.now(UTC)
    found: dict[str, tuple[Board, subprocess.Popen[bytes]]] = {}
    table = host_table()
    try:
        for name in BOARDS:
            (tmp_path / name).mkdir()
            board = Board(tmp_path / name / "kanban.db")
            task = TASKS[name]
            started = now - timedelta(minutes=20)
            board.task(task, "running", now - timedelta(hours=1), assignee="dev-1", started=started)
            rid = board.run(task, "dev-1", started, heartbeat=now)
            assert rid == 1
            board.update(task, current_run_id=rid)
            child = sleeper()
            found[name] = (board, child)
            p = table.read(child.pid)
            assert p is not None
            board.conn.execute(
                "UPDATE task_runs SET worker_pid = ?, worker_started_at = ?, claim_lock = ?"
                " WHERE id = ?",
                (
                    child.pid,
                    table.fingerprint(p, table.epoch()[0]),
                    f"{socket.gethostname()}:1",
                    rid,
                ),
            )
            board.conn.commit()
        yield found
    finally:
        for board, child in found.values():
            child.kill()
            child.wait(timeout=10)
            board.close()


def config(tmp_path: Path, spool: Path) -> Path:
    exe = tmp_path / "bin" / "hermes"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLI.format(python=sys.executable, log=str(tmp_path / "cli.jsonl")))
    exe.chmod(0o755)
    path = tmp_path / "kelpie.toml"
    boards = "".join(
        f'[[adapters.hermes.boards]]\nname = "{n}"\ndb_path = "{tmp_path / n / "kanban.db"}"\n'
        for n in BOARDS
    )
    path.write_text(
        '[engine]\ndry_run = false\n[engine.rules.loop_breaker]\nmode = "act"\n'
        f'[adapters.hermes]\ncli = "{exe}"\nspool = "{spool}"\ngit_state = false\n'
        f"state_home = false\nworker_grace = 5\n{boards}"
    )
    return path


def spool_line(task: str, n: int, at: datetime, args_hash: str) -> str:
    return json.dumps(
        {
            "type": "tool",
            "at": at.timestamp(),
            "task_id": task,
            "run_id": "1",
            "session_id": f"s-{task}",
            "tool": "terminal",
            "tool_call_id": f"{task}-{n}",
            "args_hash": args_hash,
            "outcome": "ok",
            "duration_ms": 10,
        }
    )


def test_rule_10_stops_only_the_run_on_the_board_in_the_prefix(
    farm: dict[str, tuple[Board, subprocess.Popen[bytes]]], tmp_path: Path, tmp_db: Path
) -> None:
    now = datetime.now(UTC)
    spool = tmp_path / "spool.jsonl"
    lines = [  # sign's run 1 loops on one call; qr's run 1 does varied work
        spool_line("t_s1", n, now - timedelta(seconds=60 - n), "0123456789abcdef")
        for n in range(12)
    ] + [spool_line("t_q1", n, now - timedelta(seconds=60 - n), f"{n:016x}") for n in range(12)]
    spool.write_text("\n".join(lines) + "\n")
    settings = load_settings(config(tmp_path, spool))
    (adapter,) = build_adapters(settings)
    assert isinstance(adapter, HermesBoards)

    engine = Engine(load_rules(settings.engine).rules, adapters=[adapter], config=settings.engine)
    assert settings.engine.mode_for("loop_breaker") is Mode.ACT
    results = engine.tick(lambda: adapter.snapshot(datetime.now(UTC)))
    (stop,) = [r for r in results if r.action.kind is ActionKind.STOP]
    assert stop.outcome is Outcome.EXECUTED, stop.detail
    assert stop.action.params["session_id"] == "sign:hermes-run-1"
    assert "task t_s1 is blocked for a human" in str(stop.detail)
    assert "after SIGTERM" in str(stop.detail)

    # One Hermes command, on sign's database, pinned to sign's task and run.
    (call,) = [json.loads(x) for x in (tmp_path / "cli.jsonl").read_text().splitlines()]
    sign, sign_worker = farm["sign"]
    qr, qr_worker = farm["qr"]
    assert call["argv"][:6] == ["kanban", "block", "--kind", "needs_input", "--", "t_s1"]
    env = call["env"]
    assert env["HERMES_KANBAN_DB"] == str(sign.path)
    assert (env["HERMES_KANBAN_TASK"], env["HERMES_KANBAN_RUN_ID"]) == ("t_s1", "1")
    # Signed with sign's own key (next to the ledger), which qr's key does not verify.
    keys = {n: load_key(tmp_db.parent / f"hermes-wait-{n}.key", create=False) for n in BOARDS}
    assert keys["sign"] is not None and keys["sign"] != keys["qr"]
    comment = str(call["argv"][6])
    assert verify_stop(comment, "t_s1", keys["sign"], datetime.now(UTC), timedelta(seconds=30))
    if keys["qr"] is not None:
        assert not verify_stop(comment, "t_s1", keys["qr"], datetime.now(UTC), timedelta(minutes=1))

    # sign: blocked, run ended, worker ended. qr: running, run open, worker alive.
    assert sign_worker.wait(timeout=10) == -signal.SIGTERM
    row = sign.conn.execute("SELECT status, block_kind FROM tasks WHERE id='t_s1'").fetchone()
    assert tuple(row) == ("blocked", "needs_input")
    assert qr_worker.poll() is None
    row = qr.conn.execute("SELECT status, current_run_id FROM tasks WHERE id='t_q1'").fetchone()
    assert tuple(row) == ("running", 1)
    ended = qr.conn.execute("SELECT ended_at FROM task_runs WHERE id = 1").fetchone()[0]
    assert ended is None

    # The next tick: the snapshot reads sign's block back as Kelpie's own stop; nothing more.
    again = engine.tick(lambda: adapter.snapshot(datetime.now(UTC)))
    assert not [r for r in again if r.action.kind is ActionKind.STOP]
    assert len((tmp_path / "cli.jsonl").read_text().splitlines()) == 1
    assert qr_worker.poll() is None


def test_a_stop_of_an_unknown_board_or_a_bare_run_id_reaches_no_board(
    farm: dict[str, tuple[Board, subprocess.Popen[bytes]]], tmp_path: Path
) -> None:
    settings = load_settings(config(tmp_path, tmp_path / "spool.jsonl"))
    (adapter,) = build_adapters(settings)
    for session in ("hermes-run-1", "other:hermes-run-1", "qr:", "qrx:hermes-run-1"):
        with pytest.raises(UnsupportedAction):
            adapter.stop(session, "loop")  # type: ignore[union-attr]
    assert not (tmp_path / "cli.jsonl").exists()
    assert isinstance(adapter, HermesBoards)
    adapter.cli_environ = {"PATH": "/bin"}  # the self-test's throwaway home: every board's
    assert [b.cli_environ for b in adapter.boards] == [{"PATH": "/bin"}] * len(BOARDS)
    for board, worker in farm.values():
        assert worker.poll() is None
        assert board.conn.execute("SELECT status FROM tasks").fetchone()[0] == "running"
