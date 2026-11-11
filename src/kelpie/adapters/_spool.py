"""Reading a hook spool safely: the JSON-lines file an agent-side hook appends to.

Shared by the Claude Code and OpenClaw adapters. Agents can write next to the spool, so a
file is opened without following a symlink or blocking on a FIFO, checked to be a regular
file on the open descriptor, and read up to a byte limit with overlong lines skipped.
:func:`open_regular` is also the safe open for any other file agents can write (stop
markers, the doctors' reads of settings and config files). :func:`write_in_dir` is the
matching write: every step relative to one handle on the directory.
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import stat
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path


def open_regular(path: Path) -> int:
    """A descriptor for a regular file at ``path``, opened without following a symlink or
    blocking on a FIFO (agents can write next to the spool). Raises OSError otherwise."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
    except OSError:
        os.close(fd)
        raise
    return fd


def write_in_dir(directory: Path, name: str, body: bytes, prefix: str = ".tmp-") -> Path:
    """Write ``<directory>/<name>`` atomically (mode 0600), replacing an older file, with
    every step relative to one descriptor on the directory: the directory is opened without
    following a symlink (its last component), checked on the descriptor, and the temporary
    file is created (``O_EXCL``, ``O_NOFOLLOW``) and renamed through that descriptor. So an
    agent that can write where the directory lives cannot swap it for a symlink between the
    check and the write and make Kelpie write elsewhere. A symlink at ``name`` is replaced,
    never followed. The directory is created (0700) when missing; its parents are trusted.
    Raises OSError when the directory is a symlink or not a directory."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"not a plain file name: {name!r}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        dfd = os.open(directory, flags)
    except OSError as exc:
        plain = exc.errno not in (errno.ELOOP, errno.ENOTDIR)
        what = "cannot be opened" if plain else "is not a plain directory"
        raise OSError(exc.errno, f"{directory} {what} ({exc.strerror})") from None
    try:
        if not stat.S_ISDIR(os.fstat(dfd).st_mode):  # defence in depth: O_DIRECTORY did
            raise OSError(f"{directory} is not a plain directory")
        tmp = f"{prefix}{secrets.token_hex(8)}.tmp"
        create = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(tmp, create | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
        try:
            try:
                f = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with f:
                f.write(body)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
    finally:
        os.close(dfd)
    return directory / name


def read_lines(path: Path, limit: int, max_line: int) -> Iterator[str]:
    """Lines of a regular file, reading at most ``limit`` bytes; lines over ``max_line``
    bytes skipped."""
    fd = open_regular(path)
    with os.fdopen(fd, "rb") as f:
        left = limit
        while left > 0:
            line = f.readline(min(max_line + 1, left))
            if not line:
                return
            left -= len(line)
            if len(line) > max_line:
                while line and not line.endswith(b"\n") and left > 0:  # skip its rest
                    line = f.readline(min(max_line + 1, left))
                    left -= len(line)
                continue
            yield line.decode("utf-8", "replace")


def spool_lines(spool: Path | None, cutoff: datetime, limit: int, max_line: int) -> Iterator[str]:
    """The lines of the rotated spool (``<spool>.1``, skipped when it was last written
    before ``cutoff``) and then of the spool, each read as :func:`read_lines` does; a file
    that cannot be read is skipped."""
    if spool is None:
        return
    rotated = spool.with_name(spool.name + ".1")
    for path in (rotated, spool):
        try:
            if path is rotated and path.stat().st_mtime < cutoff.timestamp():
                continue  # nothing in it can be inside the lookback
            yield from read_lines(path, limit, max_line)
        except OSError:
            continue


def within(at: datetime, now: datetime, back: timedelta, ahead: timedelta) -> bool:
    """Whether ``at`` (a time an agent could write, such as a stop marker's) lies in
    ``[now - back, now + ahead]``. Compared as a difference, which no two datetimes overflow,
    so a time dated in year 1 or 9999 is simply out of range (#388)."""
    return -back <= at - now <= ahead


def tail_lines(spool: Path, size: int) -> list[str] | None:
    """The lines in the last ``size`` bytes of the spool (the first may be partial), or None
    when it cannot be read."""
    try:
        fd = open_regular(spool)
    except OSError:
        return None
    try:
        file_size = os.fstat(fd).st_size
        os.lseek(fd, max(0, file_size - size), os.SEEK_SET)
        return os.read(fd, size).decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    finally:
        os.close(fd)
