"""``kelpie spool scrub``: re-redact the commands in an existing hooks spool.

Spools written before the shared redaction was fixed (the Hermes plugin's and the Claude
Code hook's) can hold secrets in their ``command`` field, e.g. ``curl -u admin:pw``. This
rewrites a spool with :func:`kelpie.traces.command.redact_command` applied to each line's
command; the old, partly redacted command is the only input, and redacting it again masks
what the fixed version masks. Lines that are not JSON objects cannot be read by the adapters
and are dropped, since they could be the cut-off half of a line with a secret.

The rewrite is atomic (a temporary file in the same directory, then ``os.replace``) and
keeps the file's mode and, when run as root, its owner (so the hooks can still append). It
holds ``<spool>.lock``, the lock the Claude Code hook and the Hermes plugin take to rotate the
spool, and checks just before the replace that the path is still the file it read; if the
spool was rotated anyway, it starts over. Lines appended while it runs are scrubbed and
carried over; a line appended in the moment between the last read and the replace is lost,
so run it while the agents are idle if you can.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kelpie.traces.command import redact_command

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

MAX_ROUNDS = 5  # times appended lines are picked up before the replace


@dataclass(frozen=True, slots=True)
class ScrubResult:
    path: Path
    lines: int = 0
    changed: int = 0  # lines whose command was redacted further
    dropped: int = 0  # lines that were not JSON objects

    def render(self, dry_run: bool) -> str:
        verb = "would change" if dry_run else "changed"
        return (
            f"{self.path}: {self.lines} lines, {verb} {self.changed} commands, "
            f"dropped {self.dropped} unreadable lines"
        )


def scrub_line(line: str) -> tuple[str | None, bool]:
    """The scrubbed line (None: drop it) and whether its command changed."""
    if not line.strip():
        return None, False
    try:
        rec = json.loads(line)
    except ValueError:
        return None, False
    if not isinstance(rec, dict):
        return None, False
    command = rec.get("command")
    if not isinstance(command, str):
        return line if line.endswith("\n") else line + "\n", False
    redacted = redact_command(command)
    if redacted == command:
        return line if line.endswith("\n") else line + "\n", False
    rec["command"] = redacted
    return json.dumps(rec, separators=(",", ":"), default=str) + "\n", True


def _scrub_text(text: str) -> tuple[list[str], int, int, int]:
    """Scrubbed lines, and how many (non-blank) lines were read, changed and dropped."""
    out: list[str] = []
    total = changed = dropped = 0
    for line in text.splitlines(keepends=True):
        if not line.strip():
            continue
        total += 1
        new, did = scrub_line(line)
        if new is None:
            dropped += 1
        else:
            changed += did
            out.append(new)
    return out, total, changed, dropped


def _open_spool(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"{path} is not a regular file")
    return fd


def _read_more(fd: int) -> bytes:
    chunks = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def _lines(data: bytes) -> tuple[str, bytes]:
    """Complete lines, and the rest (a line a hook may be in the middle of writing)."""
    cut = data.rfind(b"\n") + 1
    return data[:cut].decode("utf-8", "replace"), data[cut:]


def scrub_file(path: Path, dry_run: bool = False) -> ScrubResult:
    """Re-redact every command in ``path``. Raises OSError when it cannot be read or written,
    or when the spool keeps being rotated while it runs."""
    path = Path(path)
    for _ in range(MAX_ROUNDS):
        result = _scrub_once(path, dry_run)
        if result is not None:
            return result
    raise OSError(f"{path} was replaced (rotated) while it was scrubbed; run it again")


def _scrub_once(path: Path, dry_run: bool) -> ScrubResult | None:
    """One attempt; None when the spool was rotated meanwhile (the caller retries)."""
    lock = None
    if not dry_run:  # the lock the hooks take to rotate the spool
        lock = os.open(path.with_name(path.name + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        fd = _open_spool(path)
        try:
            text, rest = _lines(_read_more(fd))
            lines, total, changed, dropped = _scrub_text(text)
            if dry_run:
                n, c, d = _tail(rest, lines)
                return ScrubResult(path, total + n, changed + c, dropped + d)
            info = os.fstat(fd)
            tmp_fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.scrub-")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.writelines(lines)
                    for _ in range(MAX_ROUNDS):  # lines appended meanwhile
                        more = _read_more(fd)
                        if not more:
                            break
                        text, rest = _lines(rest + more)
                        extra, n, c, d = _scrub_text(text)
                        f.writelines(extra)
                        total, changed, dropped = total + n, changed + c, dropped + d
                    tail: list[str] = []
                    n, c, d = _tail(rest, tail)
                    f.writelines(tail)
                    total, changed, dropped = total + n, changed + c, dropped + d
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, stat.S_IMODE(info.st_mode))
                if os.geteuid() == 0 and (info.st_uid, info.st_gid) != (0, 0):
                    os.chown(tmp, info.st_uid, info.st_gid)  # keep the hooks able to append
                current = os.stat(path, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    Path(tmp).unlink(missing_ok=True)
                    return None  # rotated meanwhile: start over on the new spool
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        finally:
            os.close(fd)
    finally:
        if lock is not None:
            os.close(lock)
    return ScrubResult(path, total, changed, dropped)


def _tail(rest: bytes, out: list[str]) -> tuple[int, int, int]:
    """A last line without a newline: kept (scrubbed) if it is a whole JSON object, else
    dropped. Returns lines read, changed, dropped."""
    if not rest.strip():
        return 0, 0, 0
    new, changed = scrub_line(rest.decode("utf-8", "replace"))
    if new is None:
        return 1, 0, 1
    out.append(new)
    return 1, int(changed), 0


def default_spools(hermes: Path | None, claude_code: Path | None) -> list[Path]:
    """The configured spools and their rotated ``.1`` files that exist."""
    found: list[Path] = []
    for spool in (hermes, claude_code):
        if spool is None:
            continue
        spool = spool.expanduser()
        for path in (spool, spool.with_name(spool.name + ".1")):
            if path.is_file() and path not in found:
                found.append(path)
    return found
