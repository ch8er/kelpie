"""Ending a stopped run's worker (kelpie.adapters.hermes.worker) with real processes."""

from __future__ import annotations

import contextlib
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
    plan = targets(recorded(submitter.pid, agent.pid))
    assert plan.refused is None and {p.pid for p in plan.procs} == {submitter.pid, agent.pid}
    result = end(plan.procs, grace=5)
    assert (result.found, result.ended, result.killed, result.survivors) == (2, 2, False, [])
    assert result.seconds < 5
    assert submitter.wait(timeout=5) == -15 and agent.wait(timeout=5) == -15
    assert re.fullmatch(r"its worker ended \d+\.\ds after SIGTERM", result.describe())


def test_a_worker_ignoring_sigterm_gets_sigkill_after_the_grace(spawn: Spawn) -> None:
    stubborn = spawn(IGNORE_TERM, ready=True)
    result = end(targets(recorded(stubborn.pid)).procs, grace=0.3)
    assert result.killed and result.ended == 1 and result.seconds >= 0.3
    assert stubborn.wait(timeout=5) == -9
    assert "after SIGKILL" in result.describe()


def spawn_with_child(spawn: Spawn, pidfile: Path) -> tuple[subprocess.Popen[bytes], int]:
    """A process with a child in a session of its own (a tool command, or a gateway)."""
    parent = spawn(
        "import subprocess, sys, time;"
        f" c = subprocess.Popen([sys.executable, '-c', {SLEEP!r}], start_new_session=True);"
        f" open({str(pidfile)!r}, 'w').write(str(c.pid)); print('ready', flush=True);"
        " time.sleep(60)",
        ready=True,
    )
    return parent, int(pidfile.read_text())


def test_descendants_in_their_own_session_are_ended_too(spawn: Spawn, tmp_path: Path) -> None:
    agent, tool = spawn_with_child(spawn, tmp_path / "tool.pid")
    try:
        plan = targets(recorded(agent=agent.pid))
        assert {p.pid for p in plan.procs} == {agent.pid, tool}
        assert end(plan.procs, grace=5).ended == 2
        assert not alive(tool)
    finally:
        with contextlib.suppress(OSError):
            os.kill(tool, 9)


def test_the_submitters_children_are_not_the_runs(spawn: Spawn, tmp_path: Path) -> None:
    """A submitter starts the profile's gateway as its child when none runs; the gateway serves
    the profile's other runs too, so it is never ended with this one."""
    submitter, gateway = spawn_with_child(spawn, tmp_path / "gateway.pid")
    try:
        plan = targets(recorded(submitter.pid))
        assert [p.pid for p in plan.procs] == [submitter.pid]
        assert end(plan.procs, grace=5).ended == 1
        assert alive(gateway)
    finally:
        with contextlib.suppress(OSError):
            os.kill(gateway, 9)


def test_a_recycled_pid_is_gone_and_never_signalled(spawn: Spawn) -> None:
    stranger = spawn()
    epoch = instantiation_epoch()
    plan = targets(recorded(stranger.pid, prints={stranger.pid: f"{epoch}|1"}))
    assert (plan.procs, plan.refused) == ([], None)
    assert plan.outcome().describe() == (
        "no worker process of the run was still running on this host"
    )
    assert alive(stranger.pid)


def test_a_worker_from_before_a_reboot_is_gone(spawn: Spawn) -> None:
    child = spawn()
    p = read_proc(child.pid)
    assert p is not None
    plan = targets(recorded(child.pid, prints={child.pid: f"other-boot:1|{p.start}"}))
    assert (plan.procs, plan.refused) == ([], None)


def test_another_pid_namespace_is_refused_not_gone(spawn: Spawn) -> None:
    """Same kernel boot, another pid 1: Hermes runs in another container; its pid numbers
    mean nothing here, and its worker may well be running."""
    child = spawn()
    p = read_proc(child.pid)
    assert p is not None
    boot = instantiation_epoch().split(":")[0]
    plan = targets(recorded(child.pid, prints={child.pid: f"{boot}:999999999|{p.start}"}))
    assert plan.procs == [] and plan.refused is not None and "PID namespace" in plan.refused
    assert plan.outcome().describe().startswith("Kelpie did not end its worker (Hermes runs")
    assert alive(child.pid)


@pytest.mark.parametrize("value", [None, "unverified", "12345", "e|x"])
def test_without_a_checkable_fingerprint_nothing_is_signalled(
    spawn: Spawn, value: str | None
) -> None:
    child = spawn()
    plan = targets(recorded(child.pid, prints={child.pid: value}))
    assert plan.procs == [] and plan.refused == "Hermes recorded no fingerprint Kelpie can check"
    assert plan.outcome().describe() == (
        "Kelpie did not end its worker (Hermes recorded no fingerprint Kelpie can check)"
    )
    assert alive(child.pid)


def test_a_process_without_fingerprint_beside_a_verified_one_is_named(spawn: Spawn) -> None:
    submitter, agent = spawn(), spawn()
    plan = targets(recorded(submitter.pid, agent.pid, prints={agent.pid: "unverified"}))
    assert [p.pid for p in plan.procs] == [submitter.pid] and plan.unchecked == [agent.pid]
    result = end(plan.procs, grace=5)
    result.unchecked = plan.unchecked
    assert result.describe().endswith(
        f"; pid {agent.pid} has no fingerprint Kelpie can check and was left running"
    )
    assert alive(agent.pid)


@pytest.mark.parametrize("lock", [None, "other-host:4242", f"{HOST}x:1"])
def test_a_run_claimed_on_another_host_is_left_alone(spawn: Spawn, lock: str | None) -> None:
    child = spawn()
    assert targets(recorded(child.pid, lock=lock)).refused == "the run was claimed on another host"


def test_another_users_worker_is_refused_not_gone(spawn: Spawn) -> None:
    child = spawn()
    plan = targets(recorded(child.pid), uid=os.getuid() + 1)
    assert plan.procs == [] and plan.refused == (
        f"the worker runs as another user (uid {os.getuid()}) than Kelpie"
    )


def test_kelpie_inside_the_run_refuses_everything(spawn: Spawn) -> None:
    """Ending the submitter would make the agent exit, and the agent is Kelpie's ancestor."""
    submitter = spawn()
    plan = targets(recorded(submitter.pid, os.getppid()))
    assert plan.procs == [] and plan.refused == "Kelpie itself runs inside the run's worker"
    assert alive(submitter.pid)


def test_without_proc_nothing_is_signalled(spawn: Spawn, tmp_path: Path) -> None:
    child = spawn()
    plan = targets(recorded(child.pid), proc=tmp_path)
    assert plan.refused == "processes cannot be verified on this system"


def fake_proc(tmp_path: Path, mountinfo: str) -> Path:
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "stat").write_text("1 (x) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0\n")
    (proc / "self" / "mountinfo").write_text(mountinfo)
    return proc


def test_a_hidden_or_unreadable_process_is_refused_not_gone(tmp_path: Path) -> None:
    hidden = fake_proc(tmp_path, "22 1 0:21 / /proc rw - proc proc rw,hidepid=invisible\n")
    worker = RunWorker(4321, "b:1|5", None, None, f"{HOST}:1")
    assert targets(worker, proc=hidden).refused == (
        "Kelpie cannot see other users' processes (/proc is mounted with hidepid)"
    )
    (hidden / "self" / "mountinfo").write_text("22 1 0:21 / /proc rw - proc proc rw\n")
    assert targets(worker, proc=hidden).refused is None  # visible to all: really gone
    (hidden / "4321").mkdir()  # there, but its stat cannot be read (hidepid=noaccess)
    assert targets(worker, proc=hidden).refused == "Kelpie cannot read /proc/4321"


def test_an_impossible_pid_is_refused(tmp_path: Path) -> None:
    proc = fake_proc(tmp_path, "")
    assert targets(RunWorker(1, "b:1|5", None, None, f"{HOST}:1"), proc=proc).refused == (
        "Hermes recorded an impossible pid (1)"
    )


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
    assert result.describe() == f"its worker did not end even after SIGKILL (pid {child.pid})"


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
    assert targets(RunWorker(None, None, None, None, f"{HOST}:1")).refused == NO_WORKER
