"""The macOS process table (libproc) behind ending a stopped run's worker, with a fake libproc
everywhere, and against real processes on macOS (CI's macos-latest job)."""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kelpie.adapters.hermes import worker
from kelpie.adapters.hermes.proctable import (
    PROC_PIDVNODEPATHINFO,
    SZOMB,
    VNODEPATHINFO_SIZE,
    Libproc,
    Proc,
    ProcBsdInfo,
    Procfs,
    birth,
    host_table,
    procargs_strings,
)
from kelpie.adapters.hermes.worker import RunWorker, end, targets

HOST = socket.gethostname() or "unknown"
LOCK = f"{HOST}:1"
ME = os.getuid()


@dataclass
class FakeProc:
    ppid: int
    start: tuple[int, int]  # tv_sec, tv_usec
    uid: int = ME
    status: int = 2  # SRUN


@dataclass
class FakeLib:
    """``proc_pidinfo``/``proc_listallpids`` as libproc behaves: fills the caller's buffers,
    returns the bytes or pids written, sets errno."""

    procs: dict[int, FakeProc] = field(default_factory=dict)
    denied: set[int] = field(default_factory=set)
    grow: int = 0  # pids appearing between proc_listallpids' two calls, that many times
    last_errno: int = 0
    reads: int = 0

    cwds: dict[int, bytes] = field(default_factory=dict)
    args: dict[int, bytes] = field(default_factory=dict)

    def procargs(self, pid: int) -> bytes | None:
        return self.args.get(pid)

    def proc_pidinfo(self, pid: int, flavor: int, arg: int, buf: object, size: int) -> int:
        self.reads += 1
        if flavor == PROC_PIDVNODEPATHINFO:
            assert size == VNODEPATHINFO_SIZE == 2352
            if pid not in self.cwds:
                self.last_errno = errno.ESRCH
                return 0
            path = self.cwds[pid]
            ctypes.memmove(ctypes.addressof(buf) + 152, path + b"\0", len(path) + 1)  # type: ignore[call-overload]
            return size
        assert flavor == 3 and size == ctypes.sizeof(ProcBsdInfo) == 136
        if pid in self.denied:
            self.last_errno = errno.EPERM
            return 0
        p = self.procs.get(pid)
        if p is None:
            self.last_errno = errno.ESRCH
            return 0
        info = buf._obj  # type: ignore[attr-defined]  # ctypes.byref(...)
        info.pbi_pid, info.pbi_ppid, info.pbi_uid = pid, p.ppid, p.uid
        info.pbi_status = p.status
        info.pbi_start_tvsec, info.pbi_start_tvusec = p.start
        return size

    def proc_listallpids(self, buf: object, size: int) -> int:
        pids = sorted(self.procs)
        if buf is None:
            return len(pids)
        if self.grow:
            self.grow -= 1
            return size // ctypes.sizeof(ctypes.c_int)  # filled up: the table grew
        arr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int))
        for i, pid in enumerate(pids):
            arr[i] = pid
        return len(pids)

    def errno(self) -> int:
        return self.last_errno

    def set_errno(self, value: int) -> None:
        self.last_errno = value

    def ppid(self, pid: int) -> int | None:
        """sysctl KERN_PROC_PID: every process, also those proc_pidinfo refuses."""
        p = self.procs.get(pid)
        return None if p is None else p.ppid


T0 = (1792372019, 283603)  # a start: 2026-10-19T...


def fake_farm() -> FakeLib:
    """pid 30 a submitter, pid 40 its run's agent under a gateway (20), 50 and 60 the agent's
    tool command and its child, 70 another run's agent under the same gateway."""
    return FakeLib(
        {
            1: FakeProc(0, (1792000000, 0), uid=0),
            20: FakeProc(1, (1792372000, 1)),
            30: FakeProc(1, (1792372010, 2)),
            40: FakeProc(20, (1792372011, 3)),
            50: FakeProc(40, (1792372012, 4)),
            60: FakeProc(50, (1792372013, 5)),
            70: FakeProc(20, (1792372014, 6)),
        }
    )


def stamp(lib: FakeLib, pid: int) -> str:
    """The fingerprint a macOS Hermes records: no epoch, the start in centiseconds."""
    return f"|{birth(*lib.procs[pid].start)}"


def test_birth_is_hermes_centiseconds() -> None:
    """psutil: ``tv_sec + tv_usec / 1000000.0``; Hermes: ``round(that * 100)``."""
    sec, usec = T0
    assert birth(sec, usec) == round((sec + usec / 1000000.0) * 100) == 179237201928
    assert birth(sec, 999999) == round((sec + 0.999999) * 100)


def test_read_and_list() -> None:
    lib = fake_farm()
    lib.procs[60].status = SZOMB
    table = Libproc(lib)
    assert table.unavailable() is None and table.pinned is False
    assert table.read(40) == Proc(40, 20, "R", birth(1792372011, 3), ME)
    assert table.read(60) is not None and table.read(60).state == "Z"  # type: ignore[union-attr]
    assert table.read(99) is None and table.read(0) is None
    assert sorted(table.pids()) == [1, 20, 30, 40, 50, 60, 70]
    lib.grow = 1
    assert sorted(table.pids()) == [1, 20, 30, 40, 50, 60, 70]  # retried once it grew
    lib.grow = 5
    assert list(table.pids()) == []  # kept growing: nothing rather than a partial list


def test_missing_tells_gone_from_unreadable() -> None:
    lib = fake_farm()
    lib.denied.add(80)
    table = Libproc(lib)
    assert table.missing(99) is None  # ESRCH: gone
    assert table.missing(80) == "Kelpie cannot read process 80 (EPERM)"


def test_short_read_and_huge_pid_are_not_gone() -> None:
    lib = fake_farm()
    table = Libproc(lib)
    real = lib.proc_pidinfo
    lib.proc_pidinfo = lambda pid, flavor, arg, buf, size: 8  # type: ignore[method-assign,assignment]
    assert table.read(40) is None
    assert table.missing(40) == "Kelpie cannot read process 40 (EIO)"
    lib.proc_pidinfo = real  # type: ignore[method-assign]
    # A board pid ctypes would truncate to another process's is no process at all.
    big = 2**32 + 40
    assert table.read(big) is None and table.missing(big) is None and table.parent(big) is None


def test_a_process_readable_again_is_not_called_gone() -> None:
    lib = fake_farm()
    table = Libproc(lib)
    assert table.missing(40) == "process 40 changed while Kelpie read it"


def test_parent_of_another_users_process() -> None:
    """libproc refuses another user's process (EPERM), sysctl still gives its parent: Kelpie's
    own ancestors are known past a root-owned ``login`` or ``sudo``."""
    lib = fake_farm()
    lib.procs[90] = FakeProc(40, (1792372020, 0), uid=0)  # sudo, under the run's agent
    lib.procs[91] = FakeProc(90, (1792372021, 0))  # kelpie, under sudo
    lib.denied.add(90)
    table = Libproc(lib)
    assert table.read(90) is None and table.parent(90) == 40
    assert worker.ancestors(91, table) == {91, 90, 40, 20, 1}


def test_the_runs_worker_and_the_agents_tree() -> None:
    lib = fake_farm()
    worker_ = RunWorker(30, stamp(lib, 30), 40, stamp(lib, 40), LOCK)
    plan = targets(worker_, Libproc(lib), uid=ME)
    assert plan.refused is None
    # The submitter, the agent and the agent's tree; never the gateway or the other run.
    assert sorted(p.pid for p in plan.procs) == [30, 40, 50, 60]


def test_a_linux_fingerprint_or_none_is_unchecked() -> None:
    lib = fake_farm()
    for recorded in ("boot:1|179237201100", "unverified", None, "|soon"):
        plan = targets(RunWorker(None, None, 40, recorded, LOCK), Libproc(lib), uid=ME)
        assert plan.refused == "Hermes recorded no fingerprint Kelpie can check"


def test_another_start_is_not_called_gone() -> None:
    """On macOS a different start may be a reused pid or a clock step in an older Hermes'
    record: Kelpie cannot tell, so it neither signals nor reports the worker gone."""
    lib = fake_farm()
    plan = targets(RunWorker(None, None, 40, "|1", LOCK), Libproc(lib), uid=ME)
    assert plan.procs == []
    assert plan.refused == (
        "its start time differs from Hermes' record (a reused pid, or a clock step)"
    )


def test_another_user_and_unreadable_and_gone() -> None:
    lib = fake_farm()
    lib.procs[40].uid = ME + 1
    plan = targets(RunWorker(None, None, 40, stamp(lib, 40), LOCK), Libproc(lib), uid=ME)
    assert plan.refused == f"the worker runs as another user (uid {ME + 1}) than Kelpie"
    lib.denied.add(40)
    plan = targets(RunWorker(None, None, 40, "|5", LOCK), Libproc(lib), uid=ME)
    assert plan.refused == "Kelpie cannot read process 40 (EPERM)"
    plan = targets(RunWorker(None, None, 99, "|5", LOCK), Libproc(lib), uid=ME)
    assert (plan.procs, plan.refused) == ([], None)  # no such pid: gone
    assert plan.outcome().describe() == (
        "no worker process of the run was still running on this host"
    )


def test_tree_read_twice() -> None:
    """A tool command whose pid is reused between the two reads brings nothing along."""
    lib = fake_farm()
    reads = iter(range(1000))
    real = lib.proc_pidinfo

    def changing(pid: int, flavor: int, arg: int, buf: object, size: int) -> int:
        if pid == 50 and next(reads) >= 1:  # at the second read of the table, 50 is a stranger
            lib.procs[50] = FakeProc(1, (1792372999, 9))
        return real(pid, flavor, arg, buf, size)

    lib.proc_pidinfo = changing  # type: ignore[method-assign]
    plan = targets(RunWorker(None, None, 40, stamp(lib, 40), LOCK), Libproc(lib), uid=ME)
    assert sorted(p.pid for p in plan.procs) == [40]


def test_a_pid_reused_before_the_signal_is_not_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pidfd on macOS: the start is read again right before each signal, and a pid that is
    another process by then is skipped, and named in the outcome."""
    lib = fake_farm()
    table = Libproc(lib)
    plan = targets(RunWorker(30, stamp(lib, 30), 40, stamp(lib, 40), LOCK), table, uid=ME)
    sent: list[tuple[int, int]] = []

    def kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        lib.procs.pop(pid, None)  # SIGTERM works on the ones it reaches

    monkeypatch.setattr(worker.os, "kill", kill)
    lib.procs[50] = FakeProc(1, (1792372999, 9))  # the tool command went; a stranger took 50
    result = end(plan.procs, grace=1, table=table, sleep=lambda s: None)
    assert sorted(sent) == [(30, signal.SIGTERM), (40, signal.SIGTERM), (60, signal.SIGTERM)]
    assert result.reused == [50] and result.survivors == [] and not result.killed
    assert result.describe().endswith(
        "; pid 50 was another process by the time of the signal, left alone"
    )


def test_kelpie_under_the_agent_through_a_root_process_signals_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lib = fake_farm()
    lib.procs[90] = FakeProc(40, (1792372020, 0), uid=0)
    lib.procs[91] = FakeProc(90, (1792372021, 0))
    lib.denied.add(90)
    monkeypatch.setattr(worker.os, "getpid", lambda: 91)
    plan = targets(RunWorker(30, stamp(lib, 30), 40, stamp(lib, 40), LOCK), Libproc(lib), uid=ME)
    assert plan.procs == [] and plan.refused == "Kelpie itself runs inside the run's worker"


def test_a_process_unreadable_after_the_signal_is_not_counted_as_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that exec'd a setuid program (EPERM) may still run: it is a survivor."""
    lib = fake_farm()
    table = Libproc(lib)
    plan = targets(RunWorker(30, stamp(lib, 30), None, None, LOCK), table, uid=ME)
    monkeypatch.setattr(worker.os, "kill", lambda pid, sig: lib.denied.add(pid))
    ticks = iter(range(1000))
    result = end(plan.procs, grace=1, table=table, clock=lambda: next(ticks), sleep=lambda s: None)
    assert result.killed and result.ended == 0 and result.survivors == [30]


def test_unavailable_libproc_signals_nothing() -> None:
    table = Libproc(error="libproc cannot be loaded (dlopen failed)")
    plan = targets(RunWorker(30, "|5", None, None, LOCK), table, uid=ME)
    assert plan.refused == "libproc cannot be loaded (dlopen failed)"


def test_host_table() -> None:
    assert isinstance(host_table("linux"), Procfs)
    mac = host_table("darwin")
    assert isinstance(mac, Libproc)
    if sys.platform != "darwin":
        assert (mac.unavailable() or "").startswith("libproc cannot be loaded")
    other = host_table("win32")
    assert other.unavailable() == "processes cannot be verified on this system (win32)"
    assert targets(RunWorker(30, "|5", None, None, LOCK), other).refused == other.unavailable()


def procargs(argv: list[bytes], env: list[bytes], exe: bytes = b"/usr/bin/python3") -> bytes:
    """What ``sysctl KERN_PROCARGS2`` returns: argc, the executable, padding, argv, env."""
    body = exe + b"\0" * 5 + b"".join(a + b"\0" for a in argv) + b"".join(e + b"\0" for e in env)
    return len(argv).to_bytes(4, sys.byteorder) + body + b"\0" * 16


def test_environment_and_working_directory() -> None:
    lib = fake_farm()
    lib.args[40] = procargs([b"python", b"-m", b"agent.managed_worker"], [b"HOME=/h", b"A=1"])
    lib.cwds[40] = b"/tmp/kelpie-selftest-x/home/profiles/p/workspace"
    table = Libproc(lib)
    assert table.environ(40) == [b"python", b"-m", b"agent.managed_worker", b"HOME=/h", b"A=1"]
    assert table.environ(50) is None  # another user's, or gone: sysctl refuses
    assert str(table.cwd(40)) == "/tmp/kelpie-selftest-x/home/profiles/p/workspace"
    assert table.cwd(50) is None
    assert procargs_strings(b"") == [] and procargs_strings(procargs([], [])) == []
    assert procargs_strings(procargs([b"a"], [b"X=1"], exe=b"")) == [b"a", b"X=1"]


def retitled(argv: list[bytes], title: bytes, env: list[bytes]) -> bytes:
    """``procargs`` of a process that renamed itself: setproctitle overwrote the argument area
    with the title and NULs (SPT_NOENV: the environment untouched)."""
    area = b"".join(a + b"\0" for a in argv)
    over = (title + b"\0").ljust(len(area), b"\0")[: len(area)]
    raw = procargs(argv, env)
    return raw.replace(area, over, 1)


def test_environment_of_a_retitled_process() -> None:
    """Hermes renames its processes: the cleanup must still read their HERMES_HOME."""
    argv = [b"python3", b"-m", b"hermes_cli.main", b"gateway", b"run"]
    env = [b"HERMES_HOME=/private/var/folders/x/kelpie-selftest-1/home", b"PATH=/bin"]
    home = env[0]
    for raw in (
        procargs(argv, env),
        retitled(argv, b"hermes gateway", env),
        retitled(argv, b"t" * (sum(len(a) + 1 for a in argv) - 1), env),  # the title fills it
    ):
        assert home in procargs_strings(raw)


# --- real processes on macOS --------------------------------------------------------------

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc")


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield child
    child.kill()
    child.wait(timeout=10)


@darwin
def test_real_libproc_reads_processes(sleeper: subprocess.Popen[bytes]) -> None:
    table = Libproc.load()
    assert table.unavailable() is None
    me = table.read(os.getpid())
    child = table.read(sleeper.pid)
    assert me is not None and child is not None
    assert child.ppid == os.getpid() and child.uid == os.getuid() and child.state != "Z"
    assert abs(child.start / 100 - time.time()) < 60  # centiseconds since the epoch
    assert sleeper.pid in set(table.pids())
    assert table.read(99999999) is None and table.missing(99999999) is None
    assert table.parent(os.getpid()) == os.getppid() and table.parent(1) == 0
    env = table.environ(sleeper.pid)
    assert env is not None and f"PATH={os.environ['PATH']}".encode() in env
    assert table.cwd(sleeper.pid) == Path(os.getcwd()).resolve()


@darwin
def test_real_fingerprint_matches_what_hermes_records(sleeper: subprocess.Popen[bytes]) -> None:
    """Hermes on macOS: ``round(process_birth(psutil.Process(pid)) * 100)``, process_birth
    being psutil's unadjusted ``create_time(monotonic=True)``."""
    psutil = pytest.importorskip("psutil")
    proc = psutil.Process(sleeper.pid)
    try:
        hermes = round(proc._proc.create_time(monotonic=True) * 100)
    except TypeError:  # a psutil without the unadjusted read
        hermes = round(proc.create_time() * 100)
    p = Libproc.load().read(sleeper.pid)
    assert p is not None and p.start == hermes


@darwin
def test_real_stop_of_a_tree(sleeper: subprocess.Popen[bytes]) -> None:
    """An agent with a tool command in its own session: both end on SIGTERM."""
    agent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import subprocess, sys, time;"
            " c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
            " start_new_session=True); print(c.pid, flush=True); time.sleep(60)",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert agent.stdout is not None
        tool = int(agent.stdout.readline())
        table = Libproc.load()
        p = table.read(agent.pid)
        assert p is not None
        plan = targets(RunWorker(None, None, agent.pid, f"|{p.start}", LOCK), table)
        assert plan.refused is None and {x.pid for x in plan.procs} == {agent.pid, tool}
        result = end(plan.procs, grace=5, table=table)
        assert result.ended == 2 and not result.killed and result.reused == []
        assert agent.wait(timeout=5) == -signal.SIGTERM
        assert table.read(tool) is None or table.read(tool).state == "Z"  # type: ignore[union-attr]
        assert sleeper.poll() is None  # not the run's: untouched
    finally:
        agent.kill()
        agent.wait(timeout=10)
        with contextlib.suppress(OSError, NameError):
            os.kill(tool, signal.SIGKILL)
