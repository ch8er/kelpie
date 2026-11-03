"""Private files: what Kelpie creates for its own data (``kelpie.db`` and its ``-wal`` and
``-shm``, report files, keys) is readable by its own user only.

New folders are created mode 0700 and new files 0600, both further narrowed by the
process umask (an explicit stricter umask such as 0077 or 0277 is respected; a looser
one never widens them). Existing files and folders are never chmodded: ``kelpie doctor``
names the ones others may read (:func:`loose`), and the operator decides.
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
    """Create ``path`` empty, mode 0600, with missing folders 0700; False when it already
    exists (left as it is). Never through a symlink."""
    private_dir(path.parent)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, FILE_MODE)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path``: a new file is created 0600 (folders 0700); an existing one
    is replaced in place and keeps its mode. Never through a symlink."""
    private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC
    fd = os.open(path, flags, FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def prepare_db(path: Path | str) -> None:
    """Before SQLite opens ``path``: create it 0600 (its folder 0700) when missing. SQLite
    gives the ``-wal`` and ``-shm`` it creates the database file's mode."""
    if str(path) in ("", ":memory:"):
        return
    create_private(Path(path))


def db_files(path: Path) -> tuple[Path, ...]:
    """The database file and the ``-wal`` and ``-shm`` SQLite keeps beside it."""
    return (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))


def loose(paths: Iterable[Path]) -> list[Path]:
    """The existing regular files among ``paths`` that others than their owner may read or
    write (not followed through symlinks)."""
    found: list[Path] = []
    for path in paths:
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            found.append(path)
    return found
