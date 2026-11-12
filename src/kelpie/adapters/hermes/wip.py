"""A local WIP commit in a task's worktree (#399, option 1): commit, never push.

The worktree belongs to the agent, so everything git reads there is untrusted (see
:mod:`kelpie.adapters.hermes.git_state`). A commit is a write, so the rules are stricter
than for reading:

- **Fixed environment.** The environment of :func:`~kelpie.adapters.hermes.git_state.git_env`
  (``PATH`` and ``HOME`` only, no prompts, no fetching, ``GIT_ALLOW_PROTOCOL=none``,
  ``GIT_CONFIG_NOSYSTEM``) plus ``GIT_CONFIG_GLOBAL=/dev/null`` (neither Kelpie's nor anyone's
  global config is read), a fixed author and committer (:data:`AUTHOR`), ``GIT_EDITOR`` and
  ``GIT_PAGER`` that run nothing. Every call gets the read side's ``-c`` overrides (no hooks:
  ``core.hooksPath=/dev/null``, no fsmonitor, no global attributes file) plus no signing, no
  automatic gc or maintenance.
- **Config that is refused, not overridden.** The worktree's own config (``local`` and
  ``worktree`` scope, listed with ``--no-includes``) may not hold ``include.*`` or
  ``includeIf.*`` (git would read further files), ``hook.*`` (config-defined hooks, which
  ``core.hooksPath`` does not turn off) nor any ``filter.*`` driver; a changed path
  with a ``filter`` attribute (Git LFS) is refused too: with the driver off, git would store
  the raw file instead of what the driver makes of it.
- **No links in the git directory** where the commit writes or git reads config
  (:func:`_check_links`): git would write through a symbolic link (a branch ref of another
  repository, a reflog anywhere) or append to a hard-linked reflog. Checked before any
  write and again right before ``add`` and ``update-ref``.
- **Only a plain dirty worktree.** Refused (a person is told, the changes stay where they
  are): a worktree owned by another user, an untrusted ``.git`` layout
  (:func:`~kelpie.adapters.hermes.git_state.common_dir`), ``HEAD`` detached, on another
  branch than ``branch`` or without a commit yet, a merge, rebase, cherry-pick, revert or
  bisect in progress, an ``index.lock``, the branch checked out in another worktree,
  unmerged entries, submodule changes, an untracked nested repository, more than
  :data:`MAX_ENTRIES` changed entries or more output than
  :data:`MAX_LISTING`, or a changed file over :data:`MAX_FILE_BYTES`. Submodules are refused
  before any ``status`` (a ``.gitmodules`` or a gitlink in the index, from ``ls-files -s``,
  which does not recurse): ``status`` would run a child git in each submodule with the
  submodule's own, unchecked config. ``status`` itself gets ``--ignore-submodules=all``. A
  clean worktree has nothing to commit.
- **Plumbing for the commit.** ``add -A`` (the agent's own index: the next session finds it
  matching the commit), ``write-tree``, ``commit-tree --no-gpg-sign`` with the message on
  standard input, then ``update-ref refs/heads/<branch> <new> <old>``: a compare-and-swap, so
  a branch moved since the status is never overwritten. ``git commit`` is not used (hooks,
  templates, editors, signing and a status display all hang off it).
- **Never a push.** No remote is contacted: the commit stays in the task's branch for the next
  session in that worktree.

The config is checked again right before ``add``; all calls end within
:data:`BUDGET_FACTOR` git timeouts. Residual risks: git still parses the hostile repository
(config, index, objects), as on the read side; linked worktrees of one repository share its
``config``, so an agent running in a sibling worktree could add a filter driver after the
first check (``status`` runs clean filters on stat-dirty files, ``add`` too) or a link after
the last link check (narrow races, as on the read side); an absolute ``core.excludesFile``
is read from wherever it points; a worker
the dispatcher starts on the task between the checks and the commit races with it (the
adapter checks for a run right before; ``update-ref`` refuses a branch that moved, an index
lock held by the worker fails ``add``). ``HOME`` is passed on, so a ``~`` in the worktree's
``core.excludesFile`` and git's default ``~/.config/git/ignore`` are read from Kelpie's home:
they decide which files are added, nothing runs. WIP commits need Kelpie to run as the user
that owns the worktrees (another owner's is refused; no ``safe.directory`` applies without a
global config).
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from kelpie.adapters.hermes.git_state import (
    _OID,
    ABBREV,
    SAFE_CONFIG,
    _run,
    _run_capped,
    _Stop,
    common_dir,
    git_env,
    probe_git,
    valid_base_ref,
)

AUTHOR = ("Kelpie", "kelpie@kelpie.invalid")  # author and committer of every WIP commit
MAX_ENTRIES = 1000  # changed entries of a worktree Kelpie still commits
MAX_FILE_BYTES = 25 << 20  # a changed file larger than this is not committed
MAX_MESSAGE = 4000  # characters of the commit message
MAX_SUBJECT = 200  # characters of its first line (a ``[skip ci]`` at its end is kept)
BUDGET_FACTOR = 4  # all git calls of one WIP commit end within this many git timeouts
MAX_LISTING = 8 << 20  # characters of ls-files or status read; beyond: refused
# Overrides on top of the read side's SAFE_CONFIG for every call of a WIP commit.
WRITE_CONFIG = (
    "commit.gpgSign=false",
    "gc.auto=0",
    "maintenance.auto=false",
    "user.useConfigOnly=false",
    "protocol.allow=never",
)
# Files in the git directory that mean an operation is in progress (or a lock is held).
IN_PROGRESS = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
    "index.lock",
)
# Config sections of the worktree that are refused (lowercased, see _check_config).
# ``hook``: config-defined hooks (git 2.46+), which ``core.hooksPath`` does not turn off.
_REFUSED_SECTIONS = ("include", "includeif", "filter", "hook")
_UNSET = ("unspecified", "unset")


class WipRefused(Exception):
    """The worktree cannot be committed safely; the text says why (safe to show)."""


class WipNothing(Exception):
    """Nothing to commit (a clean worktree)."""


class WipFailed(Exception):
    """Git failed or timed out; the text says which step (safe to show)."""


@dataclass(frozen=True, slots=True)
class WipCommit:
    """The commit made: its abbreviated id, the branch and how many entries it saved."""

    commit: str
    branch: str
    entries: int

    def describe(self) -> str:
        return f"local commit {self.commit} on {self.branch} ({self.entries} changed entries)"


def write_env(path: Path) -> dict[str, str]:
    """The whole environment of a git call of a WIP commit in the worktree at ``path``."""
    env = git_env(path)
    name, email = AUTHOR
    env.update(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_EDITOR": ":",
            "GIT_PAGER": "cat",
        }
    )
    return env


def commit_wip(path: Path, branch: str, message: str, timeout: float) -> WipCommit:
    """Commit everything uncommitted in the worktree at ``path`` onto ``branch``, locally.

    Raises :class:`WipRefused` (with why) when it cannot be done safely,
    :class:`WipNothing` for a clean worktree and :class:`WipFailed` when git fails."""
    if not valid_base_ref(branch) or branch.startswith("refs/"):
        raise WipRefused(f"{branch!r} is not a plain branch name")
    try:
        if path.stat().st_uid != os.geteuid():
            raise WipRefused("the worktree belongs to another user")
    except OSError as exc:
        raise WipFailed("the worktree cannot be read") from exc
    common = common_dir(path)
    if common is None:
        raise WipRefused("untrusted .git layout")
    if probe_git(timeout) is not None:
        raise WipFailed("git cannot be run")
    deadline = time.monotonic() + BUDGET_FACTOR * timeout
    env = write_env(path)
    config = [arg for c in (*SAFE_CONFIG, *WRITE_CONFIG) for arg in ("-c", c)]

    def git(*args: str, stdin: str | None = None, step: str, cap: int | None = None) -> str:
        cmd = ["git", "-C", str(path), *config, *args]
        try:
            if cap is None:
                code, out, _ = _run(cmd, timeout, deadline, path, env=env, stdin=stdin)
            else:
                code, out, _, cut = _run_capped(cmd, timeout, deadline, path, cap, env=env)
                if cut:
                    raise WipRefused(f"the worktree is too large to commit ({step})")
        except _Stop as stop:
            raise WipFailed(f"{step}: {stop.problem.value}") from stop
        if code != 0:
            raise WipFailed(f"{step}: git exited {code}")
        return out

    def check_config() -> None:
        _check_config(git("config", "-z", "--no-includes", "--show-scope", "--list", step="config"))

    check_config()
    gitdir = Path(git("rev-parse", "--absolute-git-dir", step="rev-parse").strip())
    _check_links(Path(common), gitdir, branch)
    _check_elsewhere(git("worktree", "list", "--porcelain", "-z", step="worktree"), path, branch)
    for name in IN_PROGRESS:
        if os.path.lexists(gitdir / name):
            raise WipRefused(f"an operation is in progress in the worktree ({name})")
    # Submodules before any status: git would run a child git inside each one, with the
    # submodule's own config (filters, includes) that is not checked here.
    if os.path.lexists(path / ".gitmodules"):
        raise WipRefused("the worktree has submodules")
    if _has_gitlink(git("ls-files", "-s", "-z", step="ls-files", cap=MAX_LISTING)):
        raise WipRefused("the worktree has submodules")
    status = git(
        "status",
        "--porcelain=v2",
        "-z",
        "--branch",
        "--untracked-files=all",
        "--ignore-submodules=all",
        step="status",
        cap=MAX_LISTING,
    )
    oid, head, paths = _parse(status)
    if head is None:
        raise WipRefused("HEAD is detached")
    if head != branch:
        raise WipRefused(f"the worktree is on {head}, not on {branch}")
    if oid is None:
        raise WipRefused(f"{branch} has no commit yet")
    if not paths:
        raise WipNothing(f"the worktree of {branch} is clean")
    _check_sizes(path, paths)
    listing = "".join(f"{p}\0" for p in sorted(paths))
    attrs = git("check-attr", "-z", "--stdin", "filter", stdin=listing, step="check-attr")
    _check_filters(attrs)
    check_config()  # again, right before add: sibling worktrees share the config
    _check_links(Path(common), gitdir, branch)  # and right before the first write
    git("add", "-A", "--", ".", step="add")
    tree = git("write-tree", step="write-tree").strip()
    if not _OID.fullmatch(tree):
        raise WipFailed("write-tree: no tree id")
    text = _message(message)
    new = git(
        "commit-tree", "--no-gpg-sign", "-p", oid, "-F", "-", tree, stdin=text + "\n", step="commit"
    ).strip()
    if not _OID.fullmatch(new):
        raise WipFailed("commit-tree: no commit id")
    _check_links(Path(common), gitdir, branch)  # the ref and its reflogs are written next
    git(
        "update-ref",
        "--no-deref",
        "-m",
        "kelpie: WIP commit",
        f"refs/heads/{branch}",
        new,
        oid,
        step="update-ref",
    )
    return WipCommit(new[:ABBREV], branch, len(paths))


def _message(message: str) -> str:
    """The commit message: never empty, at most :data:`MAX_MESSAGE` characters, its first
    line at most :data:`MAX_SUBJECT` and keeping a ``[skip ci]`` it ends with."""
    subject, _, body = message.strip().partition("\n")
    subject = subject.strip() or "WIP"
    if len(subject) > MAX_SUBJECT:
        keep = " [skip ci]" if subject.endswith("[skip ci]") else ""
        subject = subject[: MAX_SUBJECT - len(keep)].rstrip() + keep
    return f"{subject}\n{body}".strip()[:MAX_MESSAGE]


def _check_links(common: Path, gitdir: Path, branch: str) -> None:
    """Refuse when a place the commit writes (objects, the branch ref, the reflogs, the
    index) or a file git reads as config is a symbolic link, or a reflog or ref is a hard
    link: git would write (or read) through it, outside the repository."""
    places = [
        common / "objects",
        common / "objects" / "pack",
        common / "objects" / "info",
        common / "refs",
        common / "logs",
        common / "logs" / "refs",
        common / "packed-refs",
        common / "config",
        common / "info",
        common / "info" / "exclude",
        common / "info" / "attributes",
        gitdir / "HEAD",
        gitdir / "index",
        gitdir / "logs",
        gitdir / "logs" / "HEAD",
        gitdir / "config.worktree",
    ]
    for root in (common / "refs" / "heads", common / "logs" / "refs" / "heads"):
        places.append(root)
        for part in branch.split("/"):
            root = root / part
            places.append(root)
    for place in places:
        try:
            info = place.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise WipRefused(f"{place.name} in the git directory cannot be read") from exc
        if stat.S_ISLNK(info.st_mode):
            raise WipRefused(f"{place.name} in the git directory is a symbolic link")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1 and place.name != "index":
            raise WipRefused(f"{place.name} in the git directory is a hard link")
    try:
        with os.scandir(common / "objects") as entries:
            if any(e.is_symlink() for e in entries):
                raise WipRefused("an objects directory is a symbolic link")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WipRefused("the objects directory cannot be read") from exc


def _check_elsewhere(listing: str, path: Path, branch: str) -> None:
    """Refuse when ``worktree list --porcelain -z`` shows ``branch`` checked out in another
    worktree of the repository: moving it would move another worker's ``HEAD``."""
    here = path.resolve()
    where: Path | None = None
    for record in listing.split("\0"):
        if record.startswith("worktree "):
            where = Path(record.removeprefix("worktree ")).resolve()
        elif record == f"branch refs/heads/{branch}" and where is not None and where != here:
            raise WipRefused(f"{branch} is checked out in another worktree too")


def _has_gitlink(listing: str) -> bool:
    """Whether ``ls-files -s -z`` lists a gitlink (mode 160000: a submodule)."""
    return any(record.startswith("160000 ") for record in listing.split("\0"))


def _check_config(listing: str) -> None:
    """Refuse includes and filter drivers in the worktree's own config (``scope\\0key=value``
    records of ``config -z --show-scope --list``)."""
    parts = listing.split("\0")
    for scope, entry in zip(parts[::2], parts[1::2], strict=False):
        if scope == "command":
            continue  # the -c overrides of this call (global and system config are off)
        key = entry.split("\n", 1)[0]
        section = key.split(".", 1)[0].casefold()
        if section in _REFUSED_SECTIONS:
            raise WipRefused(f"the worktree's git config sets {section}.*")


def _parse(out: str) -> tuple[str | None, str | None, set[str]]:
    """``(HEAD oid, branch, changed paths)`` of ``status --porcelain=v2 -z --branch
    --untracked-files=all``; raises :class:`WipRefused` for what is not committed safely."""
    oid = head = None
    paths: set[str] = set()
    records = iter(out.split("\0"))
    for record in records:
        if record.startswith("# branch.oid "):
            value = record.removeprefix("# branch.oid ").strip()
            oid = value if _OID.fullmatch(value) else None
        elif record.startswith("# branch.head "):
            value = record.removeprefix("# branch.head ").strip()
            head = value if value and value != "(detached)" else None
        elif record.startswith("u "):
            raise WipRefused("the worktree has unmerged files")
        elif record.startswith(("1 ", "2 ")):
            fields = record.split(" ", 8 if record[0] == "1" else 9)
            if len(fields) < 3 or not fields[2].startswith("N"):
                raise WipRefused("the worktree has submodule changes")
            paths.add(fields[-1])
            if record[0] == "2":
                paths.add(next(records, ""))
        elif record.startswith("? "):
            name = record[2:]
            if name.endswith("/"):
                raise WipRefused("the worktree has an untracked nested repository")
            paths.add(name)
        if len(paths) > MAX_ENTRIES:
            raise WipRefused(f"the worktree has more than {MAX_ENTRIES} changed entries")
    paths.discard("")
    return oid, head, paths


def _check_sizes(path: Path, paths: set[str]) -> None:
    """Refuse a changed file over :data:`MAX_FILE_BYTES` (a build output nobody ignored)."""
    for name in paths:
        try:
            size = (path / name).lstat().st_size
        except OSError:
            continue  # deleted: nothing to store
        if size > MAX_FILE_BYTES:
            raise WipRefused(f"a changed file is larger than {MAX_FILE_BYTES >> 20} MiB")


def _check_filters(out: str) -> None:
    """Refuse when a changed path has a ``filter`` attribute (``path\\0attr\\0value\\0``)."""
    parts = out.split("\0")
    for value in parts[2::3]:
        if value and value not in _UNSET:
            raise WipRefused(f"a changed file has a filter attribute ({value[:40]})")
