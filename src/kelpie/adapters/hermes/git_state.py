"""Git state of a task workspace, read without running what the workspace configures.

Workspaces belong to agents, so everything git reads there (``.git/config`` and the files it
includes, ``.gitattributes``, ``.git/info/attributes``, the objects) is untrusted. Git runs
programs named in that config (filter drivers, fsmonitor, gpg for signed commits), so this
module asks git only things that do not need them, and switches off what it can:

- three git commands per workspace: ``config --get-regexp '^filter\\.'`` (lists filter
  drivers, runs nothing), ``status --porcelain=v2`` with every listed driver emptied, and
  ``cat-file commit <oid>`` (no gpg, no filters, unlike ``log``);
- ``-c`` overrides on every call: no fsmonitor, no global attributes file, no hooks, no
  submodule recursion; ``--attr-source=<empty tree>`` where git supports it (2.40+), so the
  worktree's ``.gitattributes`` is not read;
- a minimal environment (``PATH``, ``HOME`` and fixed git settings; nothing else from
  Kelpie's environment, so no tokens and no inherited ``GIT_*``), no prompts, no optional
  locks (``status`` never rewrites the index);
- each call in its own process group with a timeout, the whole group killed on timeout, and an
  optional deadline shared by all calls of one snapshot.

Any failure leaves the fields ``None`` and says why in ``GitState.problem``. Nothing here
raises. ``last_commit`` is the first ``ABBREV`` hex digits of the ``HEAD`` object id.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0  # seconds, per git call
ABBREV = 7  # hex digits of ``last_commit``
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ATTR_SOURCE_SINCE = (2, 40)  # first git with ``--attr-source``

# Fixed environment of every git call: nothing else is inherited from Kelpie's environment.
GIT_ENV = {
    "LC_ALL": "C",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_OPTIONAL_LOCKS": "0",
}
INHERITED_ENV = ("PATH", "HOME")
# Config overrides on every call (``-c`` wins over the repository's own config).
SAFE_CONFIG = (
    "core.fsmonitor=false",
    "core.attributesFile=/dev/null",
    "core.hooksPath=/dev/null",
    "status.showUntrackedFiles=normal",
    "submodule.recurse=false",
)
FILTER_KEYS = ("clean", "smudge", "process")

_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_VERSION = re.compile(r"(\d+)\.(\d+)")
_ENTRY_PREFIXES = ("1 ", "2 ", "u ", "? ")  # changed, renamed, unmerged, untracked


class Problem(StrEnum):
    """Why git gave no (full) answer."""

    NO_GIT = "git not found"
    TIMEOUT = "git timed out"
    BUDGET = "git time budget of the snapshot used up"
    FAILED = "git failed"  # not a repository, dubious ownership, broken config, ...
    UNSAFE = "filter driver name that cannot be switched off"


@dataclass(frozen=True, slots=True)
class GitState:
    """What git says about a workspace; ``None`` is "unknown"."""

    last_commit: str | None = None  # first ABBREV hex digits of HEAD
    last_commit_at: datetime | None = None  # committer date of HEAD, UTC
    branch: str | None = None
    changed_files: int | None = None  # entries of ``status`` (untracked included)
    problem: Problem | None = None

    @property
    def dirty(self) -> bool:
        return bool(self.changed_files)


class _Stop(Exception):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.value)
        self.problem = problem


# --- warnings once ----------------------------------------------------------------------

_WARNED: set[tuple[str, str]] = set()
_WARNED_MAX = 4096
_WARNED_LOCK = threading.Lock()


def warn_once(key: tuple[str, str], msg: str, *args: object) -> None:
    """``log.warning`` the first time ``key`` (e.g. ``(path, cause)``) is seen, then debug."""
    with _WARNED_LOCK:
        new = key not in _WARNED and len(_WARNED) < _WARNED_MAX
        if new:
            _WARNED.add(key)
    (log.warning if new else log.debug)(msg, *args)


# --- running git ------------------------------------------------------------------------


def git_env() -> dict[str, str]:
    """The whole environment of a git call."""
    env = {k: v for k in INHERITED_ENV if (v := os.environ.get(k)) is not None}
    env.update(GIT_ENV)
    return env


def _run(cmd: list[str], timeout: float, deadline: float | None) -> tuple[int, str, str]:
    """``(returncode, stdout, stderr)`` of ``cmd``; raises ``_Stop`` if it cannot finish."""
    limit, cut = timeout, False
    if deadline is not None:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _Stop(Problem.BUDGET)
        if left < timeout:
            limit, cut = left, True
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=git_env(),
            encoding="utf-8",
            errors="replace",
            start_new_session=True,  # its own process group, killed as a whole on timeout
        )
    except FileNotFoundError as exc:
        raise _Stop(Problem.NO_GIT) from exc
    except (OSError, ValueError) as exc:
        log.debug("hermes: cannot run git: %s", exc)
        raise _Stop(Problem.FAILED) from exc
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        raise _Stop(Problem.BUDGET if cut else Problem.TIMEOUT) from exc
    return proc.returncode, out, err


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill git and everything it started in its group, without waiting on their pipes."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # proc is not reaped yet: its pgid is still ours
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


# --- git version ------------------------------------------------------------------------

_ATTR_SOURCE: bool | None = None  # does the installed git take --attr-source; None: not known
_PROBE_LOCK = threading.Lock()


def probe_git(timeout: float = DEFAULT_TIMEOUT) -> Problem | None:
    """Check once per process which git is installed; ``None`` if it can be used.

    The answer is cached once known; "git not found" and timeouts are not cached, so a git
    installed later is picked up.
    """
    global _ATTR_SOURCE
    with _PROBE_LOCK:
        if _ATTR_SOURCE is not None:
            return None
        try:
            code, out, _ = _run(["git", "version"], timeout, None)
        except _Stop as stop:
            return stop.problem
        found = _VERSION.search(out) if code == 0 else None
        version = (int(found[1]), int(found[2])) if found else (0, 0)
        _ATTR_SOURCE = version >= ATTR_SOURCE_SINCE
        log.debug("hermes: %s; --attr-source %s", out.strip(), _ATTR_SOURCE)
        return None


# --- reading a workspace ----------------------------------------------------------------


def read_git_state(
    path: Path,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    branch: str | None = None,
    deadline: float | None = None,
) -> GitState:
    """Git state of the worktree at ``path``.

    ``branch`` is a branch name known from elsewhere (the Kanban task); it wins over git's.
    ``timeout`` limits each git call; ``deadline`` (``time.monotonic()``) limits all of them.
    Only a directory with its own ``.git`` is read: git is never left to search parent
    directories for a repository.
    """
    if not os.path.lexists(path / ".git"):
        return GitState(branch=branch)
    problem = probe_git(timeout)
    if problem is not None:
        return GitState(branch=branch, problem=problem)
    try:
        return _read(path, timeout, deadline, branch)
    except _Stop as stop:
        return GitState(branch=branch, problem=stop.problem)


def _read(path: Path, timeout: float, deadline: float | None, branch: str | None) -> GitState:
    def git(command: str, *args: str, extra: tuple[str, ...] = ()) -> tuple[int, str]:
        config = [arg for c in (*SAFE_CONFIG, *extra) for arg in ("-c", c)]
        options = [f"--attr-source={EMPTY_TREE}"] if _ATTR_SOURCE else []
        cmd = ["git", "-C", str(path), *config, *options, command, *args]
        code, out, err = _run(cmd, timeout, deadline)
        if code != 0 and (command, code) != ("config", 1):  # 1: nothing matched
            log.debug("hermes: git %s exited %s in %s: %s", command, code, path, err.strip())
        return code, out

    code, out = git("config", "-z", "--name-only", "--get-regexp", r"^filter\.")
    if code not in (0, 1):  # 1: no filter driver
        return GitState(branch=branch, problem=Problem.FAILED)
    drivers = _filter_drivers(out if code == 0 else "")
    if drivers is None:
        return GitState(branch=branch, problem=Problem.UNSAFE)
    off = tuple(f"filter.{d}.{k}=" for d in sorted(drivers) for k in FILTER_KEYS)
    off += tuple(f"filter.{d}.required=false" for d in sorted(drivers))
    code, out = git(
        "status",
        "--porcelain=v2",
        "--branch",
        "--untracked-files=normal",
        "--ignore-submodules=all",
        extra=off,
    )
    if code != 0:
        return GitState(branch=branch, problem=Problem.FAILED)
    oid, head, changed = _parse_status(out)
    committed_at = None
    if oid is not None:
        code, raw = git("cat-file", "commit", oid)
        committed_at = _committed_at(raw) if code == 0 else None
    return GitState(
        last_commit=oid[:ABBREV] if oid else None,
        last_commit_at=committed_at,
        branch=branch or head,
        changed_files=changed,
    )


def _filter_drivers(listing: str) -> set[str] | None:
    """Driver names in ``config -z --name-only`` output; ``None`` if one cannot be overridden.

    A name with ``=`` or a line break cannot be given to ``-c``, so its driver would stay on.
    """
    names: set[str] = set()
    for key in listing.split("\0"):
        if not key:
            continue
        name = key[len("filter.") : key.rfind(".")] if key.count(".") >= 2 else ""
        if not name or any(c in name for c in "=\n\r"):
            return None
        names.add(name)
    return names


def _parse_status(out: str) -> tuple[str | None, str | None, int]:
    """``(oid of HEAD, branch, number of entries)`` from ``status --porcelain=v2 --branch``."""
    oid = head = None
    changed = 0
    for line in out.splitlines():
        if line.startswith("# branch.oid "):
            value = line.removeprefix("# branch.oid ").strip()
            oid = value if _OID.fullmatch(value) else None  # "(initial)": no commit yet
        elif line.startswith("# branch.head "):
            value = line.removeprefix("# branch.head ").strip()
            head = value if value and value != "(detached)" else None
        elif line.startswith(_ENTRY_PREFIXES):
            changed += 1
    return oid, head, changed


def _committed_at(raw: str) -> datetime | None:
    """The committer date of a raw commit object (``committer <who> <epoch> <tz>``)."""
    for line in raw.split("\n"):
        if not line:  # end of the headers
            return None
        if line.startswith("committer "):
            parts = line.rsplit(" ", 2)
            try:
                return datetime.fromtimestamp(int(parts[1]), UTC) if len(parts) == 3 else None
            except (ValueError, OverflowError, OSError):
                return None
    return None
