"""Hermes adapter: end the worker of a run Kelpie stopped (Linux and macOS).

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

- Hermes' fingerprint recorded for that pid (``worker_started_at``, or the ``worker_bound``
  event) equals the live process's, computed the same way (``proctable``: on Linux
  ``"<boot_id>:<pid 1 start>|<start>"``, on macOS ``"|<start>"`` with the start in
  centiseconds): a recycled pid or a reboot never match. Rows without a fingerprint (older
  Hermes, or a macOS Hermes without psutil) or marked ``unverified`` are not signalled;
- the run's claim lock names this host (``<hostname>:...``);
- the process belongs to the user Kelpie runs as, is not Kelpie or one of its ancestors (if
  one of the run's processes is, nothing is signalled), and is not a zombie;
- a descendant is read while the agent is still the verified process;
- right before each signal the process still has the start time it was verified with
  (within one boot and PID namespace, that is the same process).

A process is "gone" only when Kelpie can tell (no such pid, dead, another start time on Linux,
another boot). When it cannot see or may not signal one (another user, another PID namespace,
a hidden or unreadable process) nothing is signalled and the outcome says why.

How: SIGTERM to all of them, then, for those still running after the grace period, SIGKILL.
On Linux through a pidfd where the kernel has one (a pid cannot be recycled under it); macOS
has none, so the start time is read again right before each signal and a pid that changed is
skipped (the outcome says so). Kelpie never writes ``kanban.db``: Hermes' reaper later finds
the processes gone and tidies the run row. On a system with neither ``/proc`` nor libproc
nothing is signalled and the outcome says so.

A descendant of the agent is ended whatever it is: if a tool command started a Hermes client
that started a gateway for another profile (``hermes -p other chat``), that gateway goes too.
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
from typing import Any

from kelpie.adapters.hermes.proctable import (
    DEAD,
    GONE,
    PROC,
    SAME,
    UNCHECKED,
    Proc,
    ProcessTable,
    Procfs,
    fingerprint,
    host_table,
    instantiation_epoch,
    read_proc,
)

__all__ = [
    "PROC",
    "Proc",
    "Procfs",
    "fingerprint",
    "host_table",
    "instantiation_epoch",
    "read_proc",
]

DEFAULT_GRACE = 5.0  # seconds between SIGTERM and SIGKILL
KILL_WAIT = 2.0  # seconds to wait for the processes to go after SIGKILL
POLL = 0.05
UNVERIFIED = "unverified"  # Hermes' marker for a spawn whose fingerprint it could not read
_DEAD = DEAD
NO_WORKER = "the board records no worker process for the run"
OLD_BOARD = "this Hermes records no worker processes on the board"


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
    reused: list[int] = field(default_factory=list)  # another process by the signal: skipped

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
        if self.reused:
            pids = ", ".join(str(p) for p in self.reused)
            text += f"; pid {pids} was another process by the time of the signal, left alone"
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


# --- reading the process table -------------------------------------------------------------


def children(table: ProcessTable) -> dict[int, list[Proc]]:
    """Every readable process, by parent pid."""
    out: dict[int, list[Proc]] = {}
    for pid in table.pids():
        if (p := table.read(pid)) is not None:
            out.setdefault(p.ppid, []).append(p)
    return out


def ancestors(pid: int, table: ProcessTable) -> set[int]:
    """``pid`` and every process above it."""
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        p = table.read(pid)
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
    """The run's recorded processes (all ``None`` when the run is not on the board); None on
    a board without these columns (older Hermes). Other database errors propagate."""
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
        return RunWorker(None, None, None, None, None)
    pid, started, lock = row[0], row[1], row[2]
    agent = agent_print = None
    bound = conn.execute(
        "SELECT payload FROM task_events WHERE run_id = ? AND kind = 'worker_bound'"
        " ORDER BY id DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    payload = _json(bound[0]) if bound is not None else {}
    if payload.get("claim_lock") == lock and _pid(payload.get("pid")) is not None:
        agent, agent_print = payload["pid"], _text(payload.get("started_at"))
    return RunWorker(_pid(pid), _text(started), agent, agent_print, _text(lock))


def _pid(value: object) -> int | None:
    return value if type(value) is int else None  # not a bool


def _json(raw: object) -> dict[str, object]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except (ValueError, RecursionError):  # the board is written by agents
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def targets(
    worker: RunWorker,
    table: ProcessTable | None = None,
    uid: int | None = None,
    hostname: str | None = None,
) -> Plan:
    """The run's processes Kelpie may end, or why it may not. A process counts as gone only
    when Kelpie can tell (no such pid, a dead one, another start time, another boot); when it
    cannot see or may not signal it (another user, another PID namespace, a hidden or
    unreadable process, Kelpie inside the run) nothing is signalled and the reason says so."""
    table = host_table() if table is None else table
    if (why := table.unavailable()) is not None:
        return Plan(refused=why)
    if worker.submitter is None and worker.agent is None:
        return Plan(refused=NO_WORKER)
    host = hostname if hostname is not None else (socket.gethostname() or "unknown")
    if worker.claim_lock is None or not worker.claim_lock.startswith(f"{host}:"):
        return Plan(refused="the run was claimed on another host")
    uid = os.getuid() if uid is None else uid
    epoch, why = table.epoch()
    if why is not None:
        return Plan(refused=why)
    mine = ancestors(os.getpid(), table)
    plan = Plan()
    agent: Proc | None = None
    for pid, recorded in ((worker.submitter, worker.submitter_print),
                          (worker.agent, worker.agent_print)):  # fmt: skip
        if pid is None:
            continue
        found, why = _verify(pid, recorded, table, uid, epoch)
        if found is not None and found.pid in mine:
            return Plan(refused="Kelpie itself runs inside the run's worker")
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
        plan.procs.extend(_descendants(agent, table, uid, mine, {p.pid for p in plan.procs}))
    return plan


_UNCHECKED = UNCHECKED


def _verify(
    pid: int, recorded: str | None, table: ProcessTable, uid: int, epoch: str
) -> tuple[Proc | None, str | None]:
    """``(the live process, None)`` when it is the recorded one, ``(None, None)`` when the
    recorded one is gone, ``(None, why)`` when Kelpie cannot tell or may not signal it."""
    if pid <= 1:
        return None, f"Hermes recorded an impossible pid ({pid})"
    p = table.read(pid)
    if p is None:
        return None, table.missing(pid)  # None: no such process, gone
    if p.state in _DEAD:
        return None, None
    if recorded is None or recorded == UNVERIFIED or "|" not in recorded:
        return None, _UNCHECKED
    rec_epoch, _, rec_start = recorded.rpartition("|")
    try:
        started = int(rec_start)
    except ValueError:
        return None, _UNCHECKED
    match = table.epoch_matches(rec_epoch, epoch)
    if match == GONE:
        return None, None
    if match != SAME:
        return None, match  # UNCHECKED, or why Kelpie may not tell
    if p.start != started:
        return None, table.other_start()  # Linux: the pid was reused, the worker is gone
    if p.uid != uid:
        return None, f"the worker runs as another user (uid {p.uid}) than Kelpie"
    return p, None


def _descendants(
    agent: Proc, table: ProcessTable, uid: int, mine: set[int], seen: set[int]
) -> list[Proc]:
    """The agent's process tree below it. The process table is read twice and a process counts
    only if both reads show it with the same start time and parent, and the agent is still the
    verified process after them: a pid reused between the reads (a child gone, a stranger in
    its place) does not bring the stranger's children along."""
    first = children(table)
    second = {(c.pid, c.start, c.ppid) for kids in children(table).values() for c in kids}
    if not _same(agent, table):
        return []  # the agent went (and its pid may be reused) while the tree was read
    found: list[Proc] = []
    queue = [agent]
    while queue:
        for child in first.get(queue.pop().pid, ()):
            fresh = child.pid not in seen and child.pid not in mine
            stable = (child.pid, child.start, child.ppid) in second
            if fresh and stable and child.uid == uid and child.state not in _DEAD:
                seen.add(child.pid)
                found.append(child)
                queue.append(child)
    return found


# --- signalling -----------------------------------------------------------------------------


def _same(p: Proc, table: ProcessTable) -> bool:
    now = table.read(p.pid)
    return now is not None and now.start == p.start and now.state not in _DEAD


def _reused(p: Proc, table: ProcessTable) -> bool:
    """Is ``p``'s pid now another live process (another start time)?"""
    now = table.read(p.pid)
    return now is not None and now.start != p.start and now.state not in _DEAD


def _signal(p: Proc, sig: int, table: ProcessTable) -> bool:
    """Send ``sig`` to ``p`` only while it is still that process; False when it was not sent
    because the pid is another process now."""
    pidfd = None
    if table.pinned:
        with contextlib.suppress(OSError, AttributeError):
            pidfd = os.pidfd_open(p.pid)  # type: ignore[attr-defined]
    try:
        if not _same(p, table):  # checked after the pidfd pins the process (or right before)
            return not _reused(p, table)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            if pidfd is not None:
                signal.pidfd_send_signal(pidfd, sig)  # type: ignore[attr-defined]
            else:
                os.kill(p.pid, sig)
        return True
    finally:
        if pidfd is not None:
            os.close(pidfd)


def end(
    procs: Iterable[Proc],
    grace: float = DEFAULT_GRACE,
    table: ProcessTable | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Ended:
    """SIGTERM ``procs``, SIGKILL the ones left after ``grace``; how it went."""
    table = host_table() if table is None else table
    procs = list(procs)
    result = Ended(found=len(procs))
    if not procs:
        return result
    started = clock()
    for p in procs:
        if not _signal(p, signal.SIGTERM, table):
            result.reused.append(p.pid)
    left = _wait(procs, started + grace, table, clock, sleep)
    if left:
        result.killed = True
        for p in left:
            if not _signal(p, signal.SIGKILL, table):
                result.reused.append(p.pid)
        left = _wait(left, clock() + KILL_WAIT, table, clock, sleep)
    result.seconds = clock() - started
    result.ended = len(procs) - len(left)
    result.survivors = [p.pid for p in left]
    return result


def _wait(
    procs: list[Proc],
    deadline: float,
    table: ProcessTable,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> list[Proc]:
    left = [p for p in procs if _same(p, table)]
    while left and clock() < deadline:
        sleep(POLL)
        left = [p for p in left if _same(p, table)]
    return left
