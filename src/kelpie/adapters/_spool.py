"""Reading a hook spool safely: the JSON-lines file an agent-side hook appends to.

Shared by the Claude Code and OpenClaw adapters. Agents can write next to the spool, so a
file is opened without following a symlink or blocking on a FIFO, checked to be a regular
file on the open descriptor, and read up to a byte limit with overlong lines skipped.
:func:`open_regular` is also the safe open for any other file agents can write (stop
markers, the doctors' reads of settings and config files).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from datetime import datetime
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
