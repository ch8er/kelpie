"""Reading a hook spool safely: the JSON-lines file an agent-side hook appends to.

Shared by the Claude Code and OpenClaw adapters. Agents can write next to the spool, so a
file is opened without following a symlink or blocking on a FIFO, checked to be a regular
file on the open descriptor, and read up to a byte limit with overlong lines skipped.
:func:`open_regular` is also the safe open for any other file agents can write (stop
markers, the doctors' reads of settings and config files). :func:`write_in_dir` is the
matching write: every step relative to one handle on the directory.

:class:`SpoolCache` is the per-tick read of a spool (#392): it parses only what was appended
since the last tick, remembering an offset per file (by device and inode, so a rotation
keeps it), and caps the lines it parses per tick, the lines it keeps and the tool calls it
keeps per run.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import secrets
import stat
from collections import Counter, deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# Per spool (the live file and ``.1`` together). A hook rotates at 20 MiB, so a spool holds
# at most about 40 MiB: some 150,000 to 300,000 lines, all kept. Only an agent writing the
# spool directly, past the rotation, reaches these. The bytes kept between ticks are capped
# at twice the per-file read limit: what the old full read took in each tick.
MAX_LINES = 400_000  # lines parsed per tick, and lines kept between ticks (the newest)
MAX_RUN_CALLS = 20_000  # tool calls per named run or session in a snapshot (the newest)
READ_BLOCK = 256 * 1024  # bytes read (and split into lines) at a time
HEAD = 256  # bytes of a file's start remembered, to tell a new file on a reused inode


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


class _Item(Protocol):
    """What :class:`SpoolCache` needs of a parsed line."""

    @property
    def at(self) -> datetime: ...

    @property
    def session_id(self) -> str | None: ...


@dataclass(slots=True)
class _File[T: _Item]:
    """What was read of one spool file (one inode)."""

    offset: int = 0  # bytes read: up to the end of the last whole line
    head: bytes = b""  # its first bytes, as read
    skipping: bool = False  # in a line too long (or cut by a skip) whose end is not read yet
    items: list[T] = field(default_factory=list)  # its parsed lines, in file order
    sizes: list[int] = field(default_factory=list)  # each one's bytes in the file


@dataclass(slots=True)
class SpoolCache[T: _Item]:
    """Incremental reads of a hook spool (``<spool>.1``, then ``<spool>``), one per adapter.

    Each call parses only the lines appended since the last one: it remembers, per file
    (device and inode, so the file keeps its offset when the hook renames it to ``.1``), how
    far it read and the lines it parsed, and drops what fell out of the lookback. A file
    that shrank (truncated) or whose first bytes changed (a new file on a reused inode) is
    read again from its start; a file no longer at either path is forgotten. Lines are
    filtered by the caller's ``cutoff`` only (``until`` is left to the caller, as a line dated
    ahead may become current). An unterminated last line is parsed but not kept: it is read
    again, whole, once its writer ends it. A ``cutoff`` earlier than the last call's (a
    snapshot of an earlier time) starts over. Lines read are what :func:`spool_lines` gives,
    except that past ``limit`` new bytes in a file the newest ``limit`` are read, not the
    oldest.

    Agents can append to the spool without bound, so caps keep a tick's time and memory
    bounded, each counted in :attr:`dropped` and logged once: at most ``limit`` new bytes
    read per file and tick (the newest); ``max_lines`` lines parsed per tick, and kept
    between ticks with at most ``2 * limit`` bytes of lines (the newest); and
    ``max_run_calls`` tool calls (``capped`` items) per named run or session in what a call
    returns (the newest; a run's first line and anything not ``capped`` are always kept;
    lines naming no run are not capped this way). A spool the hooks rotate reaches none of
    them. ``dropped["run_calls"]`` is the count left out of the latest call (it applies to
    each call afresh); the others add up.

    One cache serves one control loop (no lock: snapshots are taken one at a time).
    """

    name: str
    parse: Callable[[str, datetime], T | None]  # (line, cutoff) -> item or None
    limit: int
    max_line: int
    capped: Callable[[T], bool] = lambda item: True  # counts toward max_run_calls
    max_lines: int = MAX_LINES
    max_run_calls: int = MAX_RUN_CALLS
    dropped: Counter[str] = field(default_factory=Counter)  # cap -> lines or calls dropped
    _spool: Path | None = None
    _floor: datetime | None = None
    _files: dict[tuple[int, int], _File[T]] = field(default_factory=dict)
    _warned: set[str] = field(default_factory=set)
    _partial: bytes | None = None  # the last file's unterminated line, from _read

    def items(self, spool: Path | None, cutoff: datetime) -> list[T]:
        """The spool's parsed lines dated at or after ``cutoff``, in file order (the rotated
        file first)."""
        if spool is None:
            return []  # a copy without a spool (a board-only reader) leaves the cache alone
        if spool != self._spool or (self._floor is not None and cutoff < self._floor):
            self._files.clear()
        self._spool, self._floor = spool, cutoff
        rotated = spool.with_name(spool.name + ".1")
        seen: list[tuple[_File[T], bytes | None]] = []  # with its unterminated last line
        keys: set[tuple[int, int]] = set()
        fresh: deque[tuple[int, bytes]] = deque(maxlen=self.max_lines)
        read = 0
        for path in (rotated, spool):
            try:
                fd = open_regular(path)
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                key = (info.st_dev, info.st_ino)
                if key in keys or (path is rotated and info.st_mtime < cutoff.timestamp()):
                    continue  # read already (a hard link), or nothing in it is in the lookback
                state = self._files.get(key)
                if state is None or not self._same(fd, state, info.st_size):
                    state = self._files[key] = _File()
                read += self._read(fd, state, info.st_size, fresh, len(seen))
                partial = self._partial
            except OSError:  # unread, and forgotten (not in ``keys``): read again next time
                while fresh and fresh[-1][0] == len(seen):  # what it read before failing
                    fresh.pop()
                continue
            finally:
                os.close(fd)
            keys.add(key)
            seen.append((state, partial))
        if read > len(fresh):
            self._drop("lines", read - len(fresh), "parsed in one tick")
        for index, line in fresh:
            if (item := self.parse(line.decode("utf-8", "replace"), cutoff)) is not None:
                state = seen[index][0]
                state.items.append(item)
                state.sizes.append(len(line) + 1)
        self._files = {k: f for k, f in self._files.items() if k in keys}
        kept, held = 0, 0
        for state, _ in seen:
            keep = [i for i, item in enumerate(state.items) if item.at >= cutoff]
            if len(keep) < len(state.items):
                state.items = [state.items[i] for i in keep]
                state.sizes = [state.sizes[i] for i in keep]
            kept += len(state.items)
            held += sum(state.sizes)
        for state, _ in seen:  # the oldest go first: the rotated file's head
            cut = 0
            while cut < len(state.items) and (kept > self.max_lines or held > 2 * self.limit):
                kept, held, cut = kept - 1, held - state.sizes[cut], cut + 1
            if cut:
                del state.items[:cut], state.sizes[:cut]
                self._drop("kept", cut, "kept between ticks")
        out: list[T] = []
        for state, partial in seen:
            out.extend(state.items)
            if partial is not None:
                item = self.parse(partial.decode("utf-8", "replace"), cutoff)
                if item is not None:
                    out.append(item)
        return self._cap_runs(out)

    def _same(self, fd: int, state: _File[T], size: int) -> bool:
        """Whether the file is still the one ``state`` read: not shorter than what was read,
        and starting with the same bytes."""
        if size < state.offset:
            return False
        return not state.head or os.pread(fd, len(state.head), 0) == state.head

    def _read(
        self,
        fd: int,
        state: _File[T],
        size: int,
        into: deque[tuple[int, bytes]],
        index: int,
    ) -> int:
        """Append ``(index, line)`` to ``into`` for each whole line appended since
        ``state.offset`` (empty and overlong ones skipped; ``into`` keeps only the newest, so
        a flood of short lines never piles up). Returns how many; the unterminated last line
        goes to ``self._partial`` (None when there is none or it is too long). Advances
        ``state.offset`` past the whole lines."""
        start = state.offset
        if size - start > self.limit:
            self._drop("bytes", size - self.limit - start, "read in one tick")
            start = size - self.limit
            state.skipping = os.pread(fd, 1, start - 1) != b"\n"  # mid-line: from the next
        count = 0
        buf = b""
        pos = start
        while pos < size:
            block = os.pread(fd, min(READ_BLOCK, size - pos), pos)
            if not block:
                break
            pos += len(block)
            parts = (buf + block).split(b"\n")
            buf = parts.pop()
            for raw in parts:
                if state.skipping:
                    state.skipping = False
                elif raw and len(raw) < self.max_line:  # with its newline, at most max_line
                    into.append((index, raw))
                    count += 1
            if len(buf) > self.max_line:
                buf, state.skipping = b"", True
        state.offset = pos - len(buf)
        if not state.head and state.offset > 0:
            state.head = os.pread(fd, min(HEAD, state.offset), 0)
        self._partial = buf if buf and not state.skipping else None
        return count

    def _cap_runs(self, items: list[T]) -> list[T]:
        """At most ``max_run_calls`` capped items per named run or session (its newest; its
        first line is always kept)."""
        counts = Counter(i.session_id for i in items if i.session_id is not None and self.capped(i))
        over = {run: n - self.max_run_calls for run, n in counts.items() if n > self.max_run_calls}
        self.dropped["run_calls"] = 0
        if not over:
            return items
        out: list[T] = []
        first: set[str] = set()
        for item in items:
            run = item.session_id
            if run is not None and run not in first:
                first.add(run)  # the run's start stays where it was
            elif run in over and over[run] > 0 and self.capped(item):
                over[run] -= 1
                continue
            out.append(item)
        self._drop("run_calls", len(items) - len(out), "per run or session in a snapshot")
        return out

    def _drop(self, cap: str, count: int, what: str) -> None:
        self.dropped[cap] += count
        caps = {
            "lines": f"{self.max_lines} lines",
            "kept": f"{self.max_lines} lines or {2 * self.limit} bytes",
            "bytes": f"{self.limit} bytes",
            "run_calls": f"{self.max_run_calls} tool calls",
        }
        if cap not in self._warned:
            self._warned.add(cap)
            log.warning(
                "%s: the spool is over Kelpie's cap of %s %s; the oldest are left out "
                "(%d this time; logged once)",
                self.name,
                caps[cap],
                what,
                count,
            )
