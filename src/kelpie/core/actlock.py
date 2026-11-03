"""One acting Kelpie per board (audit #275, M7).

Two Kelpie instances that act on one board (two ``kelpie serve``, or a ``kelpie agent`` and
a local ``kelpie serve``) each keep their own ledger and park key: they comment and requeue
twice and treat each other's parks and stops as foreign. An instance that may act therefore
holds an exclusive ``flock`` on a lock file per board for as long as it runs, and a second
one refuses to start. Instances that only observe or suggest take no lock.

Where the lock lives is the adapter's choice (:class:`kelpie.core.ports.SupportsActLock`):
next to the board's database for a board on disk, under :func:`state_dir` for a board on a
server (a GitHub repository, a Linear team). ``flock`` is advisory and per host: it keeps
two instances on one machine apart, not two machines acting on one remote board. It works
on Linux and macOS on local file systems; on a network file system it may not.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

SUFFIX = ".kelpie-act.lock"
MAX_HOLDER_BYTES = 256  # what a lock file says about its holder, read for the message only


class ActLockError(RuntimeError):
    """Another instance acts on the board, or its lock cannot be taken; safe to print."""


@dataclass(frozen=True, slots=True)
class ActLock:
    """The lock of one board: ``board`` names it for people, ``path`` is the lock file."""

    board: str
    path: Path
    # Create a missing folder (0700): only for folders Kelpie owns (:func:`keyed`). Next to
    # a board's database a missing folder means a wrong path, which is an error.
    create_folder: bool = False


def beside(path: Path) -> Path:
    """The lock file next to ``path`` (a board's database or spool): ``<path>.kelpie-act.lock``."""
    path = Path(path).expanduser()
    return path.with_name(path.name + SUFFIX)


def state_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """Kelpie's per-user state folder for locks of boards that live on a server:
    ``$XDG_STATE_HOME/kelpie/locks``, by default ``~/.local/state/kelpie/locks``."""
    base = environ.get("XDG_STATE_HOME") or ""
    root = Path(base) if base and Path(base).is_absolute() else Path("~/.local/state")
    return root.expanduser() / "kelpie" / "locks"


def keyed(kind: str, *parts: str, environ: Mapping[str, str] = os.environ) -> Path:
    """The lock file under :func:`state_dir` for a board on a server, named by ``kind`` and
    a hash of ``parts`` (its address and key), so no name from config reaches the path."""
    digest = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in kind)[:32]
    return state_dir(environ) / f"{safe}-{digest}{SUFFIX}"


@dataclass(slots=True)
class HeldLocks:
    """The locks this process holds; released by :meth:`release` or when it exits."""

    held: list[tuple[ActLock, int]] = field(default_factory=list)

    @property
    def boards(self) -> tuple[str, ...]:
        return tuple(lock.board for lock, _ in self.held)

    def release(self) -> None:
        while self.held:
            _, fd = self.held.pop()
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def acquire(locks: Iterable[ActLock], pid: int | None = None) -> HeldLocks:
    """Take every lock in ``locks`` (the same file once), all or none. ActLockError when
    another process holds one (naming the board, the lock file and the holder's pid) or a
    lock file cannot be opened."""
    held = HeldLocks()
    seen: set[Path] = set()
    try:
        for lock in locks:
            path = _canonical(lock.path)
            if path in seen:
                continue
            seen.add(path)
            held.held.append((lock, _take(lock, path, os.getpid() if pid is None else pid)))
    except BaseException:
        held.release()
        raise
    return held


def _canonical(path: Path) -> Path:
    path = Path(path).expanduser()
    try:  # the folder may be reached through a link; the lock file itself never is
        return path.parent.resolve(strict=False) / path.name
    except OSError:
        return path.absolute()


def _take(lock: ActLock, path: Path, pid: int) -> int:
    if not path.parent.is_dir():
        if not lock.create_folder:
            raise ActLockError(
                f"cannot take the act lock of board {lock.board}: folder {path.parent} "
                "does not exist"
            )
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise ActLockError(_cannot(lock, path, exc)) from None
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ActLockError(_cannot(lock, path, exc)) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # a FIFO or device someone put there
            raise ActLockError(
                f"cannot take the act lock of board {lock.board}: {path} is not a regular file"
            )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise ActLockError(_cannot(lock, path, exc)) from None
            raise ActLockError(
                f"another Kelpie acts on board {lock.board}{_holder(fd)}: it holds {path}. "
                "Only one instance may act on a board: stop the other one, or run this one "
                "with every rule in observe or suggest (or dry_run = true)"
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{pid}\n".encode())
        return fd
    except BaseException:
        os.close(fd)
        raise


def _holder(fd: int) -> str:
    try:
        text = os.pread(fd, MAX_HOLDER_BYTES, 0).decode("ascii", "replace").strip()
    except OSError:
        return ""
    return f" (pid {text})" if text.isdigit() else ""


def _cannot(lock: ActLock, path: Path, exc: OSError) -> str:
    return f"cannot take the act lock of board {lock.board} at {path}: {exc.strerror or exc}"
