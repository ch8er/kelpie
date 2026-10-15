"""Read-only git state of a task workspace.

Hermes queries git as ``git -C <cwd> <args>`` with a timeout and treats any failure as "no
answer"; so does this module. Every call is read-only (``status`` runs with
``--no-optional-locks``, so it never rewrites the index), has a timeout, and a failure
leaves the field ``None``. Nothing here raises.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0  # seconds, per git call


@dataclass(frozen=True, slots=True)
class GitState:
    """What git says about a workspace; ``None`` is "unknown"."""

    last_commit: str | None = None  # short sha of HEAD
    last_commit_at: datetime | None = None  # committer date of HEAD, timezone-aware
    branch: str | None = None
    changed_files: int | None = None  # lines of ``status --porcelain`` (untracked included)

    @property
    def dirty(self) -> bool:
        return bool(self.changed_files)


class _Unavailable(Exception):
    """git cannot be run at all (missing, hangs): further calls would fail the same way."""


def read_git_state(
    path: Path, timeout: float = DEFAULT_TIMEOUT, *, branch: str | None = None
) -> GitState:
    """Git state of the worktree at ``path``.

    ``branch`` is a branch name known from elsewhere (the Kanban task); it is used as is and
    saves the ``rev-parse --abbrev-ref`` call. Otherwise the branch is read from git, where a
    detached HEAD gives ``None``. If git cannot be run or times out, only ``branch`` is kept.
    """
    try:
        sha = _git(path, timeout, "rev-parse", "--short", "HEAD")
        when = _git(path, timeout, "log", "-1", "--format=%cI")
        porcelain = _git(path, timeout, "--no-optional-locks", "status", "--porcelain")
        name = branch or _git(path, timeout, "rev-parse", "--abbrev-ref", "HEAD")
    except _Unavailable:
        return GitState(branch=branch)
    return GitState(
        last_commit=sha or None,
        last_commit_at=_parse_date(when),
        branch=name if name and name != "HEAD" else None,
        changed_files=None if porcelain is None else len(porcelain.splitlines()),
    )


def _git(path: Path, timeout: float, *args: str) -> str | None:
    """Output of ``git -C path args`` without trailing whitespace, ``None`` if git failed."""
    # fsmonitor is a config key that runs a program: never let a workspace's own config do it.
    cmd = ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "LC_ALL": "C"}
    try:
        done = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("hermes: git %s timed out after %ss in %s", args[-1], timeout, path)
        raise _Unavailable from exc
    except (OSError, ValueError) as exc:  # git not installed, bad path or timeout value
        log.warning("hermes: cannot run git in %s: %s", path, exc)
        raise _Unavailable from exc
    if done.returncode != 0:
        log.debug("hermes: git %s failed in %s: %s", args[-1], path, done.stderr.strip())
        return None
    return done.stdout.rstrip()


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
