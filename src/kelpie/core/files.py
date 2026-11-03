"""Private files: what Kelpie creates for its own data (``kelpie.db`` and its ``-wal`` and
``-shm``, report files) is readable by its own user only.

New files are created mode 0600 and new folders (the report folder) 0700, both narrowed
further by the process umask: a stricter umask such as 0077 is respected, a looser one
never widens them. The umask must leave the owner read and write (0077 is the strictest
that works): under 0277 a new database would be read-only. A missing database folder is
not created: a mistyped ``[storage] path`` fails loudly, as before.

Existing files and folders are never chmodded here: ``kelpie doctor`` names the ones
others may read (:func:`loose`), and the operator decides.
"""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Iterable
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def private_dir(path: Path) -> None:
    """Create ``path`` and any missing parent, each mode 0700; existing ones are left as
    they are."""
    missing: list[Path] = []
    folder = path
    while not folder.exists():
        missing.append(folder)
        if folder.parent == folder:
            break
        folder = folder.parent
    for folder in reversed(missing):
        with contextlib.suppress(FileExistsError):  # created meanwhile: left as it is
            folder.mkdir(mode=DIR_MODE)


def create_private(path: Path) -> bool:
    """Create ``path`` empty, mode 0600, in an existing folder; False when it already
    exists (left as it is). Never through a symlink. ``FileNotFoundError`` when the folder
    is missing."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, FILE_MODE)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path``: a new file is created 0600 (missing folders 0700); an
    existing one is replaced in place and keeps its mode. Never through a symlink, never
    into a FIFO or anything else that is not a regular file (``OSError``)."""
    private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | _NOFOLLOW | _NONBLOCK | _CLOEXEC
    fd = os.open(path, flags, FILE_MODE)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        os.ftruncate(fd, 0)
        if _NONBLOCK:
            os.set_blocking(fd, True)
        f = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with f:
        f.write(text)


def prepare_db(path: Path | str) -> None:
    """Before SQLite opens ``path``: create it 0600 when it is missing and its folder
    exists (SQLite fails as before when the folder is missing). SQLite gives the ``-wal``
    and ``-shm`` it creates the database file's mode."""
    if str(path) in ("", ":memory:"):
        return
    with contextlib.suppress(FileNotFoundError):
        create_private(Path(path))


def db_files(path: Path) -> tuple[Path, ...]:
    """The database file and the ``-wal`` and ``-shm`` SQLite keeps beside it."""
    return (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))


def loose(paths: Iterable[Path]) -> list[Path]:
    """The existing regular files among ``paths`` (symlinks followed: the mode that counts
    is the target's) that others than their owner may read or write."""
    found: list[Path] = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            found.append(path)
    return found
