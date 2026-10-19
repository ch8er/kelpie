"""Ending a stopped run's worker (kelpie.adapters.hermes.worker) with real processes."""

from __future__ import annotations

import os
import re
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from kelpie.adapters.hermes import worker
from kelpie.adapters.hermes.worker import (
    NO_WORKER,
    PROC,
    Ended,
    Proc,
    RunWorker,
    _signal,
    end,
    fingerprint,
    instantiation_epoch,
    read_proc,
    run_worker,
    targets,
)
from kelpie.sim.board import KANBAN_SCHEMA

pytestmark = pytest.mark.skipif(not (PROC / "self" / "stat").exists(), reason="needs /proc")

HOST = socket.gethostname() or "unknown"
SLEEP = "import time; time.sleep(60)"
IGNORE_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    " print('ready', flush=True); time.sleep(60)"
)


Spawn = Callable[..., "subprocess.Popen[bytes]"]


@pytest.fixture
def spawn() -> Iterator[Spawn]:
    started: list[subprocess.Popen[bytes]] = []

    def run(code: str = SLEEP, ready: bool = False) -> subprocess.Popen[bytes]:
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
        started.append(child)
        if ready:
            assert child.stdout is not None and child.stdout.readline() == b"ready\n"
        return child

    yield run
    for child in started:
        child.kill()
        child.wait(timeout=10)


def print_of(pid: int) -> str:
    p = read_proc(pid)
    assert p is not None
    return fingerprint(p, instantiation_epoch())


def alive(pid: int) -> bool:
    p = read_proc(pid)
    return p is not None and p.state not in {"Z", "X", "x"}


def recorded(
    submitter: int | None = None,
    agent: int | None = None,
    *,
    lock: str | None = f"{HOST}:4242",
    prints: dict[int, str | None] | None = None,
) -> RunWorker:
    prints = prints or {}

    def fp(pid: int | None) -> str | None:
        if pid is None:
            return None
        return prints[pid] if pid in prints else print_of(pid)

    return RunWorker(submitter, fp(submitter), agent, fp(agent), lock)


def test_the_recorded_worker_is_ended_with_sigterm(spawn: Spawn) -> None:
    submitter, agent = spawn(), spawn()
    procs, refused = targets(recorded(submitter.pid, agent.pid))
    assert refused is None and {p.pid for p in procs} == {submitter.pid, agent.pid}
    result = end(procs, grace=5)
    assert (result.found, result.ended, result.killed, result.survivors) == (2, 2, False, [])
    assert result.seconds < 5
    assert submitter.wait(timeout=5) == -15 and agent.wait(timeout=5) == -15
    assert re.fullmatch(r"its worker ended \d+\.\ds after SIGTERM", result.describe())


def test_a_worker_ignoring_sigterm_gets_sigkill_after_the_grace(spawn: Spawn) -> None:
    stubborn = spawn(IGNORE_TERM, ready=True)
    procs, _ = targets(recorded(stubborn.pid))
    result = end(procs, grace=0.3)
    assert result.killed and result.ended == 1 and result.seconds >= 0.3
    assert stubborn.wait(timeout=5) == -9
    assert "after SIGKILL" in result.describe()


def test_descendants_in_their_own_session_are_ended_too(spawn: Spawn, tmp_path: Path) -> None:
    pidfile = tmp_path / "tool.pid"
    agent = spawn(
        "import subprocess, sys, time;"
        f" c = subprocess.Popen([sys.executable, '-c', {SLEEP!r}], start_new_session=True);"
        f" open({str(pidfile)!r}, 'w').write(str(c.pid)); print('ready', flush=True);"
        " time.sleep(60)",
        ready=True,
    )
    tool = int(pidfile.read_text())
    try:
        procs, _ = targets(recorded(agent=agent.pid))
        assert {p.pid for p in procs} == {agent.pid, tool}
        assert end(procs, grace=5).ended == 2
        assert not alive(tool)
    finally:
        if alive(tool):
            os.kill(tool, 9)


def test_the_submitters_children_are_not_the_runs(spawn: Spawn, tmp_path: Path) -> None:
    """A submitter starts the profile's gateway as its child when none runs; the gateway serves
    the profile's other runs too, so it is never ended with this one."""
    pidfile = tmp_path / "gateway.pid"
    submitter = spawn(
        "import subprocess, sys, time;"
        f" c = subprocess.Popen([sys.executable, '-c', {SLEEP!r}], start_new_session=True);"
        f" open({str(pidfile)!r}, 'w').write(str(c.pid)); print('ready', flush=True);"
        " time.sleep(60)",
        ready=True,
    )
    gateway = int(pidfile.read_text())
    try:
        procs, _ = targets(recorded(submitter.pid))
        assert [p.pid for p in procs] == [submitter.pid]
        assert end(procs, grace=5).ended == 1
        assert alive(gateway)
    finally:
        os.kill(gateway, 9)


def test_a_recycled_pid_is_never_signalled(spawn: Spawn) -> None:
    stranger = spawn()
    epoch = instantiation_epoch()
    worker = recorded(stranger.pid, prints={stranger.pid: f"{epoch}|1"})  # another start time
    assert targets(worker) == ([], None)
    assert end([]).describe() == "no worker process of the run was still running on this host"
    assert alive(stranger.pid)


def test_another_boot_or_namespace_is_never_signalled(spawn: Spawn) -> None:
    child = spawn()
    p = read_proc(child.pid)
    assert p is not None
    worker = recorded(child.pid, prints={child.pid: f"other-boot:1|{p.start}"})
    assert targets(worker) == ([], None)


@pytest.mark.parametrize("value", [None, "unverified", "12345"])
def test_without_a_checkable_fingerprint_nothing_is_signalled(
    spawn: Spawn, value: str | None
) -> None:
    child = spawn()
    procs, refused = targets(recorded(child.pid, prints={child.pid: value}))
    assert procs == [] and refused == "Hermes recorded no fingerprint Kelpie can check"
    assert "left to Hermes' dispatcher" in Ended(refused=refused).describe()
    assert alive(child.pid)


@pytest.mark.parametrize("lock", [None, "other-host:4242", f"{HOST}x:1"])
def test_a_run_claimed_on_another_host_is_left_alone(spawn: Spawn, lock: str | None) -> None:
    child = spawn()
    assert targets(recorded(child.pid, lock=lock)) == ([], "the run was claimed on another host")


def test_another_users_process_is_left_alone(spawn: Spawn) -> None:
    child = spawn()
    assert targets(recorded(child.pid), uid=os.getuid() + 1) == ([], None)


def test_kelpie_and_its_ancestors_are_never_targets() -> None:
    me, parent = os.getpid(), os.getppid()
    assert targets(recorded(me, parent)) == ([], None)


def test_without_proc_nothing_is_signalled(spawn: Spawn, tmp_path: Path) -> None:
    child = spawn()
    worker = recorded(child.pid)
    assert targets(worker, proc=tmp_path) == ([], "processes cannot be verified on this system")


def test_a_process_that_changed_is_not_signalled(spawn: Spawn) -> None:
    child = spawn()
    p = read_proc(child.pid)
    assert p is not None
    _signal(Proc(p.pid, p.ppid, p.state, p.start + 1, p.uid), 9, PROC)
    assert alive(child.pid)


def test_a_worker_that_does_not_die_is_reported(
    spawn: Spawn, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = spawn()
    p = read_proc(child.pid)
    assert p is not None
    monkeypatch.setattr(worker, "_signal", lambda *a: None)  # the signals do not land
    ticks = iter(range(100))
    result = end([p], grace=1, clock=lambda: next(ticks), sleep=lambda s: None)
    assert (result.killed, result.ended, result.survivors) == (True, 0, [child.pid])
    assert result.describe() == (
        f"its worker did not end even after SIGKILL (pid {child.pid}); Hermes' dispatcher reaps it"
    )


def test_read_proc_handles_odd_command_names(tmp_path: Path) -> None:
    stat = "1234 (a) b (c) S 77 1 1 0 -1 0 0 0 0 0 0 0 0 0 20 0 1 0 98765 0 0\n"
    (tmp_path / "1234").mkdir()
    (tmp_path / "1234" / "stat").write_text(stat)
    p = read_proc(1234, tmp_path)
    assert p is not None and (p.ppid, p.state, p.start) == (77, "S", 98765)
    (tmp_path / "1234" / "stat").write_text("garbage")
    assert read_proc(1234, tmp_path) is None
    assert read_proc(99, tmp_path) is None


# --- what the board records ---------------------------------------------------------------


def board(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "kanban.db")
    conn.executescript(KANBAN_SCHEMA)
    conn.execute(
        "INSERT INTO task_runs (id, task_id, status, started_at, claim_lock, worker_pid,"
        " worker_started_at) VALUES (7, 't1', 'blocked', 1, 'h:1', 100, 'e|5')"
    )
    return conn


def bound(conn: sqlite3.Connection, payload: str, run_id: int = 7) -> None:
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
        " VALUES ('t1', ?, 'worker_bound', ?, 1)",
        (run_id, payload),
    )


def test_run_worker_reads_the_submitter_and_the_bound_agent(tmp_path: Path) -> None:
    conn = board(tmp_path)
    assert run_worker(conn, 7) == RunWorker(100, "e|5", None, None, "h:1")
    bound(conn, '{"pid": 200, "claim_lock": "h:1", "started_at": "e|6"}')
    bound(conn, '{"pid": 300, "claim_lock": "h:1", "started_at": "e|7"}', run_id=8)
    assert run_worker(conn, 7) == RunWorker(100, "e|5", 200, "e|6", "h:1")
    bound(conn, '{"pid": 201, "claim_lock": "other:1", "started_at": "e|9"}')  # not this claim
    assert run_worker(conn, 7) == RunWorker(100, "e|5", None, None, "h:1")
    bound(conn, "not json")
    assert run_worker(conn, 7) == RunWorker(100, "e|5", None, None, "h:1")
    assert run_worker(conn, 99) is None


def test_an_older_board_without_worker_columns_records_nothing(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    assert run_worker(conn, 1) is None


def test_a_run_without_recorded_processes_is_left_to_hermes() -> None:
    assert targets(RunWorker(None, None, None, None, f"{HOST}:1")) == ([], NO_WORKER)
