"""Long tool runs and tool slots on the Hermes host, read from its process table.

A farm's agents run long tools (on a live iOS farm ``xcodebuild`` and ``ios_sim.sh``) from a
shell in the task's workspace (there ``.worktrees/<task>``, ``workspaces/<task>``). A task is
busy with such a tool while a process of one of ``names`` (config, required: no tool is
built in) is alive and it, or one of its ancestors, has its working directory in the task's
workspace: the workspace Hermes records for the task (the longest one that contains it; a
path several tasks share belongs to none), or a directory matching one of
``workspace_patterns`` (``{task}`` stands for a task id on the board). Rule 9 holds its
commit reminders meanwhile. An orphaned tool of an earlier run that still works there keeps
the task busy too, up to rule 9's ``tool_grace``.

Tools that need exclusive hardware take a slot first: on that farm simulator slots are
empty lock files held with ``lockf -s -t 0 <slot> <command>`` by agents and CI runners alike,
under the same user. A slot is held while some process has its file open (``lockf`` keeps
it open for as long as the command runs); its holder is an agent's when the holder or one of
its ancestors works in a task workspace, else CI's. Kelpie never takes a lock to probe one: a
probe would make a real ``lockf -t 0`` fail. Pools of slots (``[[...slots]]``, glob patterns)
are reported as ``slots`` readings, one per pool and holder, for the metrics.

Everything is read with ``ps -axww -o pid= -o ppid= -o etime= -o command=`` and ``lsof``
(working directories of the candidates and their ancestors, holders of the slot files), each
with a timeout, all within ``BUDGET_FACTOR`` timeouts per snapshot, and a minimal
environment. A command line is matched by the base names of its first word and of the first
of the next words that is no option (an executable, or an interpreter and its script:
``bash -x ios_sim.sh``) and is never kept or logged: it may carry secrets. Any failure (a
timeout, an error exit, lsof's error on stderr, more output than ``MAX_OUTPUT``) reads
nothing for this snapshot and is warned once per cause; the adapter keeps the last good read
a few minutes. Processes are those Kelpie may see: run it as the agents' user. Relative slot
globs resolve against Kelpie's working directory.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import subprocess
import time
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

from kelpie.adapters.hermes.git_state import warn_once

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0  # seconds, per ps or lsof call
BUDGET_FACTOR = 3  # all calls of one snapshot within this many timeouts
MAX_DEPTH = 32  # ancestors walked from a tool or a slot holder
MAX_OUTPUT = 4_000_000  # characters of ps or lsof output; more is a failed read
_SCRIPT_WORDS = 4  # words after the first searched for an interpreter's script
# Commands whose first non-option word is the script they run ("vim ios_sim.sh" is none).
INTERPRETERS = frozenset(
    {"sh", "bash", "zsh", "dash", "ksh", "fish", "env", "python", "python3", "node",
     "ruby", "perl", "osascript", "swift", "xcrun"}
)  # fmt: skip
AGENT, CI = "agent", "ci"
# lsof on a file or pid that went away between the listing and the call: not a failure.
_GONE = re.compile(r"No such file or directory|no such process", re.IGNORECASE)
_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}
_ETIME = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")  # [[dd-]hh:]mm:ss


@dataclass(frozen=True, slots=True)
class SlotPool:
    """A named pool of tool slots: lock files matched by ``paths`` (glob patterns). ``size``
    is the number of slots when set; else the files that exist count. BSD ``lockf`` without
    ``-k`` deletes a slot's file when its command ends, so set ``size`` unless the files are
    created once and kept (``lockf -k``)."""

    name: str
    paths: tuple[str, ...]
    size: int | None = None


@dataclass(frozen=True, slots=True)
class ToolProcesses:
    """What to look for (adapter config ``[adapters.hermes.tools]``)."""

    names: tuple[str, ...] = ()
    workspace_patterns: tuple[str, ...] = ()
    slots: tuple[SlotPool, ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    _patterns: tuple[re.Pattern[str], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        compiled = []
        for p in self.workspace_patterns:
            if p.count("{task}") != 1:
                raise ValueError(f"workspace pattern {p!r} must name {{task}} once")
            if p.startswith("~"):
                raise ValueError(f"workspace pattern {p!r}: path components, not a ~ path")
            head, tail = (re.escape(x) for x in p.strip("/").split("{task}"))
            # The lookahead leaves the "/" for the next match: ~/workspaces/repo/workspaces/t_1
            compiled.append(re.compile(rf"(?:^|/){head}(?P<task>[^/]+){tail}(?=/|$)"))
        object.__setattr__(self, "_patterns", tuple(compiled))

    def task_of(
        self, cwd: str, tasks: Collection[str], workspaces: Mapping[str, str | None]
    ) -> str | None:
        """The task whose workspace ``cwd`` is in: Hermes' own path for it (the longest
        that contains it; a shared path, mapped to None, belongs to no task and stops the
        search), else a ``workspace_patterns`` match naming a task on the board."""
        for root, task in sorted(workspaces.items(), key=lambda kv: (-len(kv[0]), kv[0])):
            if cwd == root or cwd.startswith(root.rstrip("/") + "/"):
                if task is not None:
                    return task
                break  # shared: no shorter root claims it; a pattern still may
        for p in self._patterns:
            for m in p.finditer(cwd):
                if m["task"] in tasks:
                    return m["task"]
        return None


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    ppid: int
    started: datetime
    names: tuple[str, ...]  # base names of its executable and of its script, if any


@dataclass(frozen=True, slots=True)
class ToolRun:
    tool: str
    since: datetime  # when its earliest tool process started


@dataclass(frozen=True, slots=True)
class PoolUse:
    total: int
    agent: int = 0
    ci: int = 0


@dataclass(frozen=True, slots=True)
class HostTools:
    """Tool runs per task id and the use of each slot pool, read once per snapshot."""

    runs: Mapping[str, ToolRun] = field(default_factory=dict)
    pools: Mapping[str, PoolUse] = field(default_factory=dict)


def read_host(
    config: ToolProcesses,
    now: datetime,
    tasks: Collection[str],
    workspaces: Mapping[str, str],
) -> HostTools | None:
    """Tool runs and slot use on this host; None when the process table cannot be read."""
    budget = _Budget(config.timeout)
    procs = read_processes(now, budget)
    if procs is None:
        return None
    wanted = set(config.names)
    tools = [p for p in procs.values() if wanted.intersection(p.names)]
    holders: dict[str, set[int]] = {}
    files = {pool.name: _slot_files(pool) for pool in config.slots}
    every = sorted({f for found in files.values() for f in found})
    if every:
        held = open_by(every, budget)
        if held is None:
            return None
        holders = held
    chains = {p.pid: _ancestry(p.pid, procs) for p in tools}
    for pids in holders.values():
        for pid in pids:
            chains.setdefault(pid, _ancestry(pid, procs))
    cwds = working_dirs({pid for chain in chains.values() for pid in chain}, budget)
    if cwds is None:
        return None

    def owner(pid: int) -> str | None:
        for ancestor in chains[pid]:
            if (cwd := cwds.get(ancestor)) is not None and (
                task := config.task_of(cwd, tasks, workspaces)
            ) is not None:
                return task
        return None

    runs: dict[str, ToolRun] = {}
    for p in tools:
        task = owner(p.pid)
        if task is None:
            continue
        name = next(n for n in p.names if n in wanted)
        if task not in runs or p.started < runs[task].since:
            runs[task] = ToolRun(name, p.started)
    pools: dict[str, PoolUse] = {}
    for pool in config.slots:
        agent = ci = 0
        for f in files[pool.name]:
            pids = holders.get(f, set())
            if not pids:
                continue
            if any(owner(pid) is not None for pid in pids):
                agent += 1
            else:
                ci += 1
        total = pool.size if pool.size is not None else len(files[pool.name])
        pools[pool.name] = PoolUse(max(total, agent + ci), agent, ci)
    return HostTools(runs, pools)


class _Budget:
    """Each call's timeout, all calls of one snapshot within ``BUDGET_FACTOR`` of them."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self.deadline = time.monotonic() + BUDGET_FACTOR * timeout

    def next(self) -> float:
        return min(self.timeout, self.deadline - time.monotonic())


def read_processes(now: datetime, budget: _Budget | float) -> dict[int, Process] | None:
    """Every process this user may see, by pid."""
    budget = budget if isinstance(budget, _Budget) else _Budget(budget)
    argv = ["ps", "-axww", "-o", "pid=", "-o", "ppid=", "-o", "etime=", "-o", "command="]
    out = _run(argv, budget)
    if out is None:
        return None
    procs: dict[int, Process] = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        age = _elapsed(parts[2])
        if age is None:
            continue
        words = parts[3].split(None, _SCRIPT_WORDS + 1)[: _SCRIPT_WORDS + 1]
        script = None
        if _interpreter(PurePosixPath(words[0]).name):  # "bash -x ./ios_sim.sh"
            script = next(
                (w for w in words[1:] if not w.startswith("-") and "=" not in w), None
            )  # options and env's KEY=VALUE are no script
        names = tuple(PurePosixPath(w).name for w in (words[0], script) if w)
        procs[int(parts[0])] = Process(int(parts[0]), int(parts[1]), now - age, names)
    return procs


def working_dirs(pids: Iterable[int], budget: _Budget | float) -> dict[int, str] | None:
    """The working directory of each of ``pids`` that lsof can tell."""
    budget = budget if isinstance(budget, _Budget) else _Budget(budget)
    wanted = sorted(set(pids))
    if not wanted:
        return {}
    found: dict[int, str] = {}
    for i in range(0, len(wanted), 200):  # bounded command lines
        chunk = ",".join(str(p) for p in wanted[i : i + 200])
        out = _run(["lsof", "-w", "-a", "-d", "cwd", "-p", chunk, "-Fn"], budget, ok=(0, 1))
        if out is None:
            return None
        for pid, name in _fields(out):
            found.setdefault(pid, name)
    return found


def open_by(files: Sequence[str], budget: _Budget | float) -> dict[str, set[int]] | None:
    """The processes that have each of ``files`` open (lsof exits 1 when none does)."""
    budget = budget if isinstance(budget, _Budget) else _Budget(budget)
    out = _run(["lsof", "-w", "-Fn", "--", *files], budget, ok=(0, 1))
    if out is None:
        return None
    held: dict[str, set[int]] = {}
    for pid, name in _fields(out):
        held.setdefault(os.path.realpath(name), set()).add(pid)
    return held


def _interpreter(name: str) -> bool:
    """A command that runs a script: INTERPRETERS, or a versioned or framework Python
    ("python3.12", "Python")."""
    return name in INTERPRETERS or bool(_PYTHON.fullmatch(name))


_PYTHON = re.compile(r"python\d*(?:\.\d+)?", re.IGNORECASE)


def _slot_files(pool: SlotPool) -> list[str]:
    found = {
        os.path.realpath(p)
        for pattern in pool.paths
        for p in glob.glob(os.path.expanduser(pattern))
    }
    return sorted(f for f in found if os.path.isfile(f))


def _ancestry(pid: int, procs: Mapping[int, Process]) -> list[int]:
    """``pid`` and its ancestors, nearest first, up to ``MAX_DEPTH`` (cycles stop it)."""
    chain = [pid]
    while len(chain) < MAX_DEPTH and (p := procs.get(chain[-1])) is not None:
        if p.ppid <= 1 or p.ppid in chain:
            break
        chain.append(p.ppid)
    return chain


def _fields(out: str) -> Iterable[tuple[int, str]]:
    """(pid, name) pairs of lsof's ``-F`` output: ``p<pid>`` starts a process, ``n<name>``
    names one of its files; other fields are skipped."""
    pid: int | None = None
    for line in out.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            yield pid, line[1:]


def _elapsed(text: str) -> timedelta | None:
    m = _ETIME.match(text)
    if m is None:
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in m.groups())
    return timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)


def _run(argv: list[str], budget: _Budget, ok: tuple[int, ...] = (0,)) -> str | None:
    """The output of one call, or None (warned once per cause). Exit 1 is lsof's "not
    every file or pid was found" only with nothing on stderr (it runs with ``-w``); with an
    error there and no output it is a failure (no /proc access, an unknown option)."""
    timeout = budget.next()
    if timeout <= 0:
        warn_once((argv[0], "budget"), "hermes: tool runs not read: %s over the budget", argv[0])
        return None
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env={**_ENV, "HOME": os.environ.get("HOME", "/")},
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        warn_once((argv[0], type(exc).__name__), "hermes: tool runs not read, %s: %s", argv[0], exc)
        return None
    errors = [ln for ln in done.stderr.splitlines() if ln.strip() and not _GONE.search(ln)]
    failed = done.returncode not in ok or (
        done.returncode == 1 and errors and not done.stdout.strip()
    )
    if failed:
        warn_once((argv[0], str(done.returncode)), "hermes: tool runs not read, %s exited %s",
                  argv[0], done.returncode)  # fmt: skip
        return None
    if len(done.stdout) > MAX_OUTPUT:
        warn_once((argv[0], "output"), "hermes: tool runs not read: %s wrote too much", argv[0])
        return None
    return done.stdout


def resolve(path: str | Path) -> str:
    """A workspace path as the process table shows it (symlinks resolved)."""
    return os.path.realpath(os.path.expanduser(str(path)))
