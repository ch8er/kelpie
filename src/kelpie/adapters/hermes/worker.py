"""Hermes adapter: end the worker of a run Kelpie stopped (Linux).

``hermes kanban block`` closes the run but leaves its worker alive: Hermes' dispatcher reaps
it only on a tick at least ``TERMINAL_WORKER_REAP_GRACE_SECONDS`` (120 s) later, and a live
farm saw a stopped worker make ~17 more model calls over ~3 minutes. Hermes has no CLI command
that ends a worker without returning the task to the queue (``reclaim``) or archiving it, so
after a successful block Kelpie ends the worker itself, the way Hermes' reaper would, and
with the same proof of identity.

Which processes (Hermes ``hermes_cli/kanban_db_dispatch.py`` and ``gateway/session_kanban.py``):

- the run's **submitter**, ``task_runs.worker_pid``: the process the dispatcher spawned. The
  agent watches it and exits within a second once it is gone. Only the submitter itself: when
  no gateway serves the profile it starts one as its child, and that gateway is shared by the
  profile's other runs;
- the run's **agent**, the pid of the run's ``worker_bound`` event (its claim lock must be the
  run's): a process of its own that the profile's gateway spawned for this run only;
- every **descendant** of the agent, found by parent pid before anything is signalled: tool
  commands (the ``terminal`` tool) start their own sessions, lose the run's environment and
  would outlive the agent.

(An older Hermes ran the agent inside the submitter and records no ``worker_bound``: then the
submitter is ended and tool commands it started are left to finish.)

What makes a process safe to signal:

- Hermes' fingerprint ``"<boot_id>:<pid 1 start>|<start>"`` recorded for that pid
  (``worker_started_at``, or the ``worker_bound`` event) equals the live process's, computed
  the same way: a recycled pid or a reboot never match. Rows without a fingerprint (older
  Hermes) or marked ``unverified`` are not signalled;
- the run's claim lock names this host (``<hostname>:...``);
- the process belongs to the user Kelpie runs as, is not Kelpie or one of its ancestors (if
  one of the run's processes is, nothing is signalled), and is not a zombie;
- a descendant is read while the agent is still the verified process;
- right before each signal the process still has the start time it was verified with
  (within one boot and PID namespace, that is the same process).

A process is "gone" only when Kelpie can tell (no such pid, dead, another start time, another
boot). When it cannot see or may not signal one (another user, another PID namespace, a hidden
or unreadable ``/proc`` entry) nothing is signalled and the outcome says why.

How: SIGTERM to all of them, then, for those still running after the grace period, SIGKILL,
through a pidfd where the kernel has one (a pid cannot be recycled under it). Kelpie never
writes ``kanban.db``: Hermes' reaper later finds the processes gone and tidies the run row.

Without ``/proc`` (macOS) nothing is signalled; the outcome says so and Hermes' reaper ends the
worker as before.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import sqlite3
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROC = Path("/proc")
DEFAULT_GRACE = 5.0  # seconds between SIGTERM and SIGKILL
KILL_WAIT = 2.0  # seconds to wait for the processes to go after SIGKILL
POLL = 0.05
UNVERIFIED = "unverified"  # Hermes' marker for a spawn whose fingerprint it could not read
_DEAD = frozenset({"Z", "X", "x"})
NO_WORKER = "the board records no worker process for the run"
OLD_BOARD = "this Hermes records no worker processes on the board"


@dataclass(frozen=True, slots=True)
class Proc:
    """A process as read from ``/proc``."""

    pid: int
    ppid: int
    state: str
    start: int  # clock ticks since boot (``/proc/<pid>/stat`` field 22)
    uid: int


@dataclass(slots=True)
class Ended:
    """What ending a run's worker came to, worded for the human told about the stop."""

    found: int = 0  # processes Kelpie identified and signalled
    ended: int = 0  # of those, gone in the end
    killed: bool = False  # SIGKILL was needed
    seconds: float = 0.0  # from the first SIGTERM until the last one went
    refused: str | None = None  # why nothing was signalled (cannot verify, not this host)
    survivors: list[int] = field(default_factory=list)
    unchecked: list[int] = field(default_factory=list)  # alive, no fingerprint: not signalled

    def describe(self) -> str:
        if self.refused is not None:
            return f"Kelpie did not end its worker ({self.refused})"
        if self.found == 0:
            text = "no worker process of the run was still running on this host"
        elif self.survivors:
            pids = ", ".join(str(p) for p in self.survivors[:5])
            text = f"its worker did not end even after SIGKILL (pid {pids})"
        else:
            how = "SIGKILL" if self.killed else "SIGTERM"
            text = f"its worker ended {self.seconds:.1f}s after {how}"
        if self.unchecked:
            pids = ", ".join(str(p) for p in self.unchecked)
            text += f"; pid {pids} has no fingerprint Kelpie can check and was left running"
        return text


@dataclass(slots=True)
class Plan:
    """The processes to end, or why none may be signalled."""

    procs: list[Proc] = field(default_factory=list)
    refused: str | None = None
    unchecked: list[int] = field(default_factory=list)  # alive but without a usable fingerprint

    def outcome(self) -> Ended:
        """The outcome when nothing is signalled."""
        return Ended(refused=self.refused, unchecked=list(self.unchecked))


# --- reading /proc --------------------------------------------------------------------------


def read_proc(pid: int, proc: Path = PROC) -> Proc | None:
    """The process ``pid``, or None when it is gone or unreadable."""
    try:
        text = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        uid = (proc / str(pid)).stat().st_uid
    except OSError:
        return None
    # "<pid> (<comm>) <state> <ppid> ...": comm may hold spaces and parentheses.
    try:
        fields = text.rsplit(")", 1)[1].split()
        return Proc(pid, int(fields[1]), fields[0], int(fields[19]), uid)
    except (IndexError, ValueError):
        return None


def instantiation_epoch(proc: Path = PROC) -> str:
    """``"<boot_id>:<pid 1 start>"``, as Hermes' ``current_instantiation_epoch``."""
    boot = pid1 = ""
    with contextlib.suppress(OSError):
        boot = (proc / "sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    first = read_proc(1, proc)
    if first is not None:
        pid1 = str(first.start)
    return f"{boot}:{pid1}" if (boot or pid1) else ""


def fingerprint(p: Proc, epoch: str) -> str:
    """Hermes' ``_process_fingerprint`` of a process."""
    return f"{epoch}|{p.start}"


def children(proc: Path = PROC) -> dict[int, list[Proc]]:
    """Every readable process, by parent pid."""
    out: dict[int, list[Proc]] = {}
    with contextlib.suppress(OSError):
        for entry in proc.iterdir():
            if entry.name.isdigit() and (p := read_proc(int(entry.name), proc)) is not None:
                out.setdefault(p.ppid, []).append(p)
    return out


def ancestors(pid: int, proc: Path = PROC) -> set[int]:
    """``pid`` and every process above it."""
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        p = read_proc(pid, proc)
        if p is None:
            break
        pid = p.ppid
    return seen


# --- which processes ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunWorker:
    """What ``kanban.db`` says about a run's processes."""

    submitter: int | None
    submitter_print: str | None
    agent: int | None
    agent_print: str | None
    claim_lock: str | None


def run_worker(conn: Any, run_id: int) -> RunWorker | None:
    """The run's recorded processes; None on a board without these columns (older Hermes) or
    without the run. Other database errors propagate."""
    try:
        row = conn.execute(
            "SELECT worker_pid, worker_started_at, claim_lock FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such" in str(exc):  # column or table
            return None
        raise
    if row is None:
        return None
    pid, started, lock = row[0], row[1], row[2]
    agent = agent_print = None
    with contextlib.suppress(sqlite3.Error):
        bound = conn.execute(
            "SELECT payload FROM task_events WHERE run_id = ? AND kind = 'worker_bound'"
            " ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        payload = _json(bound[0]) if bound is not None else {}
        if payload.get("claim_lock") == lock and isinstance(payload.get("pid"), int):
            agent, agent_print = payload["pid"], _text(payload.get("started_at"))
    return RunWorker(
        pid if isinstance(pid, int) else None, _text(started), agent, agent_print, _text(lock)
    )


def _json(raw: object) -> dict[str, object]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def targets(
    worker: RunWorker, proc: Path = PROC, uid: int | None = None, hostname: str | None = None
) -> Plan:
    """The run's processes Kelpie may end, or why it may not. A process counts as gone only
    when Kelpie can tell (no such pid, a dead one, another start time, another boot); when it
    cannot see or may not signal it (another user, another PID namespace, a hidden or
    unreadable ``/proc`` entry, Kelpie inside the run) nothing is signalled and the reason says
    so."""
    if not (proc / "self" / "stat").exists():
        return Plan(refused="processes cannot be verified on this system")
    if worker.submitter is None and worker.agent is None:
        return Plan(refused=NO_WORKER)
    host = hostname if hostname is not None else (socket.gethostname() or "unknown")
    if worker.claim_lock is None or not worker.claim_lock.startswith(f"{host}:"):
        return Plan(refused="the run was claimed on another host")
    uid = os.getuid() if uid is None else uid
    epoch = instantiation_epoch(proc)
    mine = ancestors(os.getpid(), proc)
    plan = Plan()
    agent: Proc | None = None
    for pid, recorded in ((worker.submitter, worker.submitter_print),
                          (worker.agent, worker.agent_print)):  # fmt: skip
        if pid is None:
            continue
        if pid in mine:
            return Plan(refused="Kelpie itself runs inside the run's worker")
        found, why = _verify(pid, recorded, proc, uid, epoch)
        if why == _UNCHECKED:
            plan.unchecked.append(pid)
        elif why is not None:
            return Plan(refused=why)
        elif found is not None and found.pid not in {p.pid for p in plan.procs}:
            plan.procs.append(found)
            if pid == worker.agent:
                agent = found
    if not plan.procs and plan.unchecked:
        return Plan(refused="Hermes recorded no fingerprint Kelpie can check")
    if agent is not None:
        plan.procs.extend(_descendants(agent, proc, uid, mine, {p.pid for p in plan.procs}))
    return plan


_UNCHECKED = "unchecked"
_HIDDEN = "Kelpie cannot see other users' processes (/proc is mounted with hidepid)"


def _verify(
    pid: int, recorded: str | None, proc: Path, uid: int, epoch: str
) -> tuple[Proc | None, str | None]:
    """``(the live process, None)`` when it is the recorded one, ``(None, None)`` when the
    recorded one is gone, ``(None, why)`` when Kelpie cannot tell or may not signal it."""
    if pid <= 1:
        return None, f"Hermes recorded an impossible pid ({pid})"
    p = read_proc(pid, proc)
    if p is None:
        if (proc / str(pid)).exists():
            return None, f"Kelpie cannot read /proc/{pid}"
        if _hidepid(proc):
            return None, _HIDDEN
        return None, None  # no such process: gone
    if p.state in _DEAD:
        return None, None
    rec_epoch, _, rec_start = (recorded or "").rpartition("|")
    if recorded is None or recorded == UNVERIFIED or not rec_epoch or not rec_start.isdigit():
        return None, _UNCHECKED
    if rec_epoch != epoch:
        same_boot = rec_epoch.split(":")[0] == epoch.split(":")[0] != ""
        if same_boot:  # same kernel boot, another pid 1: another PID namespace
            return None, "Hermes runs in another PID namespace than Kelpie (a container?)"
        return None, None  # recorded before a reboot: that process is gone
    if p.start != int(rec_start):
        return None, None  # the pid now belongs to another process: the worker is gone
    if p.uid != uid:
        return None, f"the worker runs as another user (uid {p.uid}) than Kelpie"
    return p, None


def _descendants(agent: Proc, proc: Path, uid: int, mine: set[int], seen: set[int]) -> list[Proc]:
    """The agent's process tree below it, read while the agent is still that process."""
    by_parent = children(proc)
    if not _same(agent, proc):
        return []  # the agent went (and its pid may be reused) while the tree was read
    found: list[Proc] = []
    queue = [agent]
    while queue:
        for child in by_parent.get(queue.pop().pid, ()):
            fresh = child.pid not in seen and child.pid not in mine
            if fresh and child.uid == uid and child.state not in _DEAD:
                seen.add(child.pid)
                found.append(child)
                queue.append(child)
    return found


def _hidepid(proc: Path) -> bool:
    """Is ``/proc`` mounted with ``hidepid`` (other users' processes invisible)?"""
    try:
        lines = (proc / "self" / "mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        _, sep, rest = line.partition(" - ")
        fields = rest.split()
        if sep and len(fields) >= 3 and fields[0] == "proc":
            options = dict(o.partition("=")[::2] for o in fields[2].split(","))
            if options.get("hidepid", "0") not in ("0", "off"):
                return True
    return False


# --- signalling -----------------------------------------------------------------------------


def _same(p: Proc, proc: Path) -> bool:
    now = read_proc(p.pid, proc)
    return now is not None and now.start == p.start and now.state not in _DEAD


def _signal(p: Proc, sig: int, proc: Path) -> None:
    """Send ``sig`` to ``p`` only while it is still that process."""
    pidfd = None
    with contextlib.suppress(OSError, AttributeError):
        pidfd = os.pidfd_open(p.pid)  # type: ignore[attr-defined]
    try:
        if not _same(p, proc):  # checked after the pidfd pins the process
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if pidfd is not None:
                signal.pidfd_send_signal(pidfd, sig)  # type: ignore[attr-defined]
            else:
                os.kill(p.pid, sig)
    finally:
        if pidfd is not None:
            os.close(pidfd)


def end(
    procs: Iterable[Proc],
    grace: float = DEFAULT_GRACE,
    proc: Path = PROC,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Ended:
    """SIGTERM ``procs``, SIGKILL the ones left after ``grace``; how it went."""
    procs = list(procs)
    result = Ended(found=len(procs))
    if not procs:
        return result
    started = clock()
    for p in procs:
        _signal(p, signal.SIGTERM, proc)
    left = _wait(procs, started + grace, proc, clock, sleep)
    if left:
        result.killed = True
        for p in left:
            _signal(p, signal.SIGKILL, proc)
        left = _wait(left, clock() + KILL_WAIT, proc, clock, sleep)
    result.seconds = clock() - started
    result.ended = len(procs) - len(left)
    result.survivors = [p.pid for p in left]
    return result


def _wait(
    procs: list[Proc],
    deadline: float,
    proc: Path,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> list[Proc]:
    left = [p for p in procs if _same(p, proc)]
    while left and clock() < deadline:
        sleep(POLL)
        left = [p for p in left if _same(p, proc)]
    return left
