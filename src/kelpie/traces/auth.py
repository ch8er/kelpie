"""The bearer token of OTLP ingest, and its rotation.

The token lives in the process environment (``[traces] token_env``) or, so that it can be
rotated without a restart, in a token file (``[traces] token_file``): an env file, as
systemd ``EnvironmentFile=``, docker ``env_file`` and a shell's ``source`` read it, that
holds ``<token_env>=<token>``.

**Rotation** (:func:`rotate`, ``kelpie traces rotate-token``) writes a new token to the
file (mode 600, owner kept, atomically, under a lock) and keeps the previous one valid for
``[traces] token_grace`` so that sessions which started with it keep reporting. A grace of
0 revokes every previous token at once. The previous token is kept only as a SHA-256 hash
with its expiry, on the line ``<token_env>_GRACE`` of the same file:
``sha256:<hex>@<ISO time>``, comma-separated. ``kelpie serve`` reads the file again when
it changes (:class:`TokenFile`); a file that turns unreadable or loses its token revokes
nothing (the last good tokens stay, and it is logged). Nothing here logs, prints or puts a
token into a message or a repr.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

__all__ = [
    "Copy",
    "Grace",
    "Rotation",
    "TokenCheck",
    "TokenFile",
    "TokenFileError",
    "looks_like_token",
    "parse_token_file",
    "path_problem",
    "read_token_file",
    "rotate",
    "token_copies",
    "trust_problem",
    "valid_name",
]

MAX_FILE_BYTES = 64 * 1024
RECHECK = timedelta(seconds=1)  # a token file is stat'ed at most this often
TOKEN_BYTES = 24  # 48 hex characters, as `openssl rand -hex 24`
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPORT = re.compile(r"^export\s+")
_GRACE_ITEM = re.compile(r"sha256:(?P<hash>[0-9a-f]{64})@(?P<until>\S+)")


class TokenFileError(ValueError):
    """The token file cannot be used; the message names the file, never a token."""


class TokenCheck(StrEnum):
    CURRENT = "current"
    GRACE = "grace"  # a previous token within its grace period
    INVALID = "invalid"


def valid_name(name: str) -> bool:
    """Whether ``name`` can be an environment variable (``token_env``)."""
    return _NAME.fullmatch(name) is not None


_SECRETISH = re.compile(r"[A-Za-z0-9_+/=-]{20,}")


def looks_like_token(text: str) -> bool:
    """Whether ``text`` (a variable name or a file name from the config) looks like a
    pasted token rather than a name: long, lower case or mixed, with digits, and not
    words joined by ``_`` (``kelpie_traces_token_v2`` is a name) unless digits make a
    quarter of it."""
    if _SECRETISH.fullmatch(text) is None or not any(c.islower() for c in text):
        return False
    digits = sum(c.isdigit() for c in text)
    return digits > 0 and ("_" not in text or digits * 4 >= len(text))


@dataclass(frozen=True, slots=True)
class Grace:
    """A previous token: its SHA-256 and when it stops being accepted."""

    sha256: str = field(repr=False)
    until: datetime

    def render(self) -> str:
        return f"sha256:{self.sha256}@{self.until.isoformat(timespec='seconds')}"


@dataclass(frozen=True, slots=True)
class TokenState:
    token: str | None = field(repr=False)
    grace: tuple[Grace, ...] = ()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _unquote(value: str, where: str, key: str) -> str:
    value = value.strip()
    if value[:1] in ("'", '"'):
        if len(value) < 2 or value[-1] != value[0]:
            raise TokenFileError(f"{where}: {key} has an unmatched quote")
        return value[1:-1]
    return value


def _key(line: str) -> str:
    return _EXPORT.sub("", line.strip()).partition("=")[0].strip()


def parse_token_file(text: str, name: str, where: str = "the token file") -> TokenState:
    """The token on ``<name>=`` and the grace list on ``<name>_GRACE=`` of an env file
    (``export`` prefixes, comments and blank lines allowed; other lines ignored)."""
    token: str | None = None
    grace: list[Grace] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = _EXPORT.sub("", line).partition("=")
        if not sep:
            continue
        key = key.strip()
        if key == name:
            token = _unquote(value, where, name) or None
        elif key == f"{name}_GRACE":
            grace = []
            for item in filter(None, (i.strip() for i in _unquote(value, where, key).split(","))):
                m = _GRACE_ITEM.fullmatch(item)
                try:
                    until = datetime.fromisoformat(m["until"]) if m else None
                except ValueError:
                    until = None
                if m is None or until is None or until.tzinfo is None:
                    raise TokenFileError(f"{where}: {name}_GRACE has an entry it cannot read")
                grace.append(Grace(m["hash"], until))
    if token is not None and (len(token) < 16 or any(c.isspace() for c in token)):
        raise TokenFileError(f"{where}: {name} must be at least 16 characters, no spaces")
    return TokenState(token, tuple(grace))


class _Opened:
    """A token file opened once, without following a symlink or blocking on a FIFO: its
    text and the identity and owner of what was read."""

    def __init__(self, path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise TokenFileError(f"{path}: not a regular file")
            data = b""
            while len(data) <= MAX_FILE_BYTES:
                chunk = os.read(fd, MAX_FILE_BYTES + 1 - len(data))
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(fd)
        if len(data) > MAX_FILE_BYTES:
            raise TokenFileError(f"{path}: larger than {MAX_FILE_BYTES} bytes")
        try:
            self.text = data.decode()
        except UnicodeDecodeError:
            raise TokenFileError(f"{path}: not UTF-8 text") from None
        self.stat = st


def _open(path: Path) -> _Opened:
    """:class:`_Opened`, with a symlink named as such; OSError when it cannot be opened."""
    try:
        return _Opened(path)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # O_NOFOLLOW on a symlink
            raise TokenFileError(
                f"{path}: a symlink; set [traces] token_file to the file itself"
            ) from None
        raise


def read_token_file(path: Path) -> str:
    """The file's text (at most :data:`MAX_FILE_BYTES`, a regular file, no symlink);
    TokenFileError otherwise, OSError when it cannot be opened."""
    return _open(path).text


def _folder_problem(path: Path, st: os.stat_result, me: int) -> str | None:
    if not stat.S_ISDIR(st.st_mode):
        return f"{path}: not a folder"
    if st.st_uid not in (me, 0):
        return f"{path}: owned by uid {st.st_uid}, not by this user (uid {me}) or root"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not st.st_mode & stat.S_ISVTX:
        return f"{path}: others may write to this folder; chmod go-w it"
    return None


MAX_LINKS = 40


def path_problem(folder: Path) -> str | None:
    """Why someone other than this user (or root) could change what ``folder`` is, or
    None. The path is walked from ``/`` one part at a time, as the kernel does: every
    folder must belong to this user or root and be closed to others' writes (a sticky
    folder such as ``/tmp`` excepted), and a symlink on the way is followed only when this
    user or root owns it (its target then checked the same way). Nobody else can then
    swap a part of the path, or have planted a link in it. The walk stops at the first
    part that does not exist yet (it will be created by this user)."""
    me = os.geteuid()
    todo = list(Path(os.path.abspath(folder)).parts)
    current = Path(os.sep)
    links = 0
    while todo:
        part = todo.pop(0)
        if part == os.sep:
            current = Path(os.sep)
            if (why := _folder_problem(current, os.lstat(current), me)) is not None:
                return why
            continue
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        step = current / part
        try:
            st = os.lstat(step)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(st.st_mode):
            if st.st_uid not in (me, 0):
                return f"{step}: a symlink of uid {st.st_uid}, not of this user (uid {me}) or root"
            links += 1
            if links > MAX_LINKS:
                return f"{folder}: too many symlinks"
            todo = list(Path(os.readlink(step)).parts) + todo
            continue
        if (why := _folder_problem(step, st, me)) is not None:
            return why
        current = step
    return None


def trust_problem(path: Path, st: os.stat_result | None = None) -> str | None:
    """Why a token file must not be trusted, or None: someone other than this user (or
    root) owns it, others may write to it, or someone else could change its folder or a
    folder above it (:func:`path_problem`). Kelpie then would take whatever token they
    chose."""
    st = st if st is not None else os.lstat(path)
    me = os.geteuid()
    if st.st_uid not in (me, 0):
        return f"{path}: owned by uid {st.st_uid}, not by this user (uid {me})"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path}: others may write to it; chmod 600 it"
    return path_problem(path.parent)


def loose_mode(path: Path) -> bool:
    """Whether others than the owner may read or write ``path``."""
    return bool(path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO))


class TokenFile:
    """The token file as ``kelpie serve`` checks bearer tokens against it: read at start,
    read again when it changed (stat'ed at most every :data:`RECHECK`, or at once when the
    clock went back). Only a file :func:`trust_problem` finds nothing wrong with is read. A
    file that turns unreadable or untrusted keeps the last good tokens (``ok`` turns
    false, ``/metrics`` shows it) and is reported through ``on_error`` once."""

    def __init__(
        self,
        path: Path,
        name: str,
        clock: Callable[[], datetime],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.path = path
        self.name = name
        self.clock = clock
        self.on_error = on_error
        self._lock = threading.Lock()
        self._seen: tuple[int, int, int, int] | None = None
        self._checked: datetime | None = None
        self.ok = True  # the last read worked
        self.loaded_at: datetime | None = None  # when the tokens in use were read
        self._state = self._load()  # raises TokenFileError: kelpie serve does not start

    def __repr__(self) -> str:
        return f"TokenFile({str(self.path)!r}, {self.name!r})"

    def _signature(self) -> tuple[int, int, int, int]:
        st = os.lstat(self.path)
        return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)

    def _load(self) -> TokenState:
        try:
            opened = _open(self.path)
            if (why := trust_problem(self.path, opened.stat)) is not None:
                raise TokenFileError(why)
            state = parse_token_file(opened.text, self.name, str(self.path))
        except OSError as exc:
            raise TokenFileError(f"{self.path}: cannot read it ({exc.strerror})") from None
        if state.token is None:
            raise TokenFileError(f"{self.path}: no {self.name}= line")
        st = opened.stat
        self._seen = (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)
        self.loaded_at = self.clock()
        return state

    def _refresh(self) -> None:
        now = self.clock()
        if self._checked is not None and timedelta(0) <= now - self._checked < RECHECK:
            return
        self._checked = now
        try:
            if self._signature() == self._seen:
                return
            self._state = self._load()
        except (OSError, TokenFileError) as exc:
            if self.ok and self.on_error is not None:
                self.on_error(f"keeping the tokens read at {self.loaded_at}: {exc}")
            self.ok = False
            return
        self.ok = True

    def status(self) -> tuple[bool, int]:
        """(the last read worked, previous tokens still accepted)."""
        with self._lock:
            self._refresh()
            now = self.clock()
            return self.ok, sum(1 for g in self._state.grace if g.until > now)

    def grace_count(self) -> int:
        """Previous tokens still accepted."""
        return self.status()[1]

    def check(self, credentials: str) -> TokenCheck:
        """``credentials`` against the current token, then the previous ones within their
        grace period; every comparison takes constant time."""
        with self._lock:
            self._refresh()
            state, now = self._state, self.clock()
        presented = credentials.encode()
        if state.token is not None and hmac.compare_digest(presented, state.token.encode()):
            return TokenCheck.CURRENT
        digest = _digest(credentials).encode()
        valid = [g for g in state.grace if g.until > now]
        if any(hmac.compare_digest(digest, g.sha256.encode()) for g in valid):
            return TokenCheck.GRACE
        return TokenCheck.INVALID


@dataclass(frozen=True, slots=True)
class Rotation:
    """What :func:`rotate` did, in counts and times only."""

    path: Path
    created: bool  # there was no token before
    grace: int  # previous tokens accepted after the rotation
    grace_until: datetime | None  # when the one just replaced stops being accepted


MAX_GRACE = 16  # previous tokens kept at most (the newest); keeps the file small


def rotate(
    path: Path,
    name: str,
    grace: timedelta,
    now: datetime,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(TOKEN_BYTES),
) -> Rotation:
    """Write a new token to ``path`` and keep the one it replaces valid until
    ``now + grace`` (as a hash); expired previous ones are dropped (and all but the newest
    :data:`MAX_GRACE`), and ``grace`` 0 drops every previous one: revokes them at once.

    Runs only as the user who owns the file's folder (root only in a folder root owns), on
    a regular file of that user, never through a symlink: so it cannot be led to write a
    file someone else chose, and ``kelpie serve`` (that same user) trusts what it writes.
    A missing file is created (a missing folder too, mode 700). The file is replaced
    atomically with mode 600, under a lock; other lines are kept, except commented-out
    copies of the token lines."""
    if not valid_name(name):
        raise TokenFileError("[traces] token_env is not a variable name")
    path = Path(os.path.abspath(path.expanduser()))
    # Before anything is created: no one else may change any folder on the way, so the
    # checks below stay true while the file is written.
    if (why := path_problem(path.parent)) is not None:
        raise TokenFileError(why)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Again, now that every part exists: a link planted where a folder was still missing
    # (in /tmp, between the check and mkdir) is refused here, before anything is written.
    if (why := path_problem(path.parent)) is not None:
        raise TokenFileError(why)
    folder = Path(os.path.realpath(path.parent))
    path = folder / path.name
    me = os.geteuid()
    fst = os.lstat(folder)
    if not stat.S_ISDIR(fst.st_mode):
        raise TokenFileError(f"{folder}: not a folder")
    if fst.st_uid != me:
        raise TokenFileError(f"{folder}: owned by uid {fst.st_uid}; run as that user")
    if fst.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TokenFileError(f"{folder}: others may write to this folder; chmod go-w it")
    lock_flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        lock = os.open(folder / f".{path.name}.lock", lock_flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TokenFileError(f"{folder}: its lock file is a symlink; remove it") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise TokenFileError(f"{folder}: its lock file is not a regular file")
        fcntl.flock(lock, fcntl.LOCK_EX)  # one rotation at a time: none loses a token
        return _rotate(path, name, grace, now, token_factory)
    finally:
        os.close(lock)


def _rotate(
    path: Path, name: str, grace: timedelta, now: datetime, token_factory: Callable[[], str]
) -> Rotation:
    lines: list[str] = []
    state = TokenState(None)
    seen: tuple[int, int, int] | None = None
    try:
        opened = _open(path)  # once: the text and the owner come from the same file
    except FileNotFoundError:
        opened = None
    except OSError as exc:
        raise TokenFileError(f"{path}: cannot read it ({exc.strerror})") from None
    if opened is not None:
        if opened.stat.st_uid != os.geteuid():
            raise TokenFileError(f"{path}: owned by uid {opened.stat.st_uid}; run as that user")
        seen = (opened.stat.st_dev, opened.stat.st_ino, opened.stat.st_ctime_ns)
        state = parse_token_file(opened.text, name, str(path))
        lines = opened.text.splitlines()
    kept: list[Grace] = []
    until = None
    if grace > timedelta(0):
        kept = [g for g in state.grace if g.until > now]
        if state.token is not None:
            until = now + grace
            kept.append(Grace(_digest(state.token), until))
        kept = kept[-MAX_GRACE:]
    token = token_factory()
    ours = {name, f"{name}_GRACE"}
    out = [line for line in lines if _key(line.lstrip().lstrip("#")) not in ours]
    out.append(f"{name}={token}")
    if kept:
        out.append(f"{name}_GRACE={','.join(g.render() for g in kept)}")
    _write_private(path, "\n".join(out) + "\n", seen)
    return Rotation(path, state.token is None, len(kept), until)


def _write_private(path: Path, text: str, seen: tuple[int, int, int] | None) -> None:
    """``text`` to ``path`` through a new mode-600 file in the same folder and a rename,
    unless the file was swapped or changed since it was read (``seen``: its device, inode
    and ctime); the folder is synced so the rename survives a crash."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        try:
            now = os.lstat(path)
            current: tuple[int, int, int] | None = (now.st_dev, now.st_ino, now.st_ctime_ns)
        except FileNotFoundError:
            current = None
        if current != seen:
            raise TokenFileError(f"{path}: changed while it was rotated; run it again")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    folder = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


# --- copies of the token on disk (kelpie doctor) -------------------------------------------

BACKUP = re.compile(
    r"(\.bak[^/]*|\.backup|\.bk|\.orig|\.old|\.dpkg-old|\.save|\.sw[op]|\.tmp|~)$|^#.*#$",
    re.IGNORECASE,
)
MAX_SCAN_FILES = 2000
MAX_SCAN_DIRS = 500
MAX_SCAN_ENTRIES = 5000  # per folder
MAX_SCAN_FILE_BYTES = 64 * 1024 * 1024  # larger files are skipped
MAX_SCAN_BYTES = 256 * 1024 * 1024  # in all; the scan stops there
MAX_SCAN_DEPTH = 3
_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Copy:
    """A file that holds the token, or (``marker``) a backup with a ``<token_env>=`` line
    that may hold an older one; ``backup`` (``*.bak*``, ``~``...), ``loose`` (others may
    read it)."""

    path: Path
    backup: bool
    loose: bool
    marker: bool = False


def _contains(fd: int, needles: tuple[bytes, ...], size: int) -> tuple[bool, ...]:
    """Which of ``needles`` the open file's first ``size`` bytes contain, read in chunks."""
    found = [False] * len(needles)
    keep = max(len(n) for n in needles) - 1
    tail = b""
    left = size
    while not all(found) and left > 0:
        chunk = os.read(fd, min(_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        data = tail + chunk
        for i, needle in enumerate(needles):
            found[i] = found[i] or needle in data
        tail = data[-keep:] if keep else b""
    return tuple(found)


def token_copies(
    token: str, roots: list[Path], skip: Path | None = None, marker: str | None = None
) -> tuple[list[Copy], str | None]:
    """The regular files under ``roots`` (``MAX_SCAN_DEPTH`` levels, symlinks not followed,
    at most ``MAX_SCAN_FILES`` files and ``MAX_SCAN_BYTES`` read) that contain ``token``,
    and backups that contain ``marker`` (``<token_env>=``), except ``skip`` (the token file
    itself); and the limit the scan hit, if any (a folder with more than
    ``MAX_SCAN_ENTRIES`` entries is skipped, the scan goes on)."""
    needles = (token.encode(), *((marker.encode(),) if marker else ()))
    found: list[Copy] = []
    seen: set[Path] = set()
    files = 0
    folders = 0
    budget = MAX_SCAN_BYTES
    limit: str | None = None
    for root in roots:
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            folders += 1
            if folders > MAX_SCAN_DIRS:
                return found, f"stopped after {MAX_SCAN_DIRS} folders"
            try:
                with os.scandir(folder) as it:
                    entries = []
                    for entry in it:
                        entries.append(entry)
                        if len(entries) > MAX_SCAN_ENTRIES:
                            break
                if len(entries) > MAX_SCAN_ENTRIES:
                    limit = f"skipped {folder} (over {MAX_SCAN_ENTRIES} entries)"
                    continue
                entries.sort(key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                path = Path(entry.path)
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir():
                        if depth < MAX_SCAN_DEPTH:
                            stack.append((path, depth + 1))
                        continue
                    if not entry.is_file() or path in seen or path == skip:
                        continue
                    seen.add(path)
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError:
                    continue
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SCAN_FILE_BYTES:
                        continue
                    files += 1
                    budget -= st.st_size
                    if files > MAX_SCAN_FILES:
                        return found, f"stopped after {MAX_SCAN_FILES} files"
                    if budget < 0:
                        return found, f"stopped after {MAX_SCAN_BYTES // 2**20} MiB"
                    hits = _contains(fd, needles, st.st_size)
                except OSError:
                    continue
                finally:
                    os.close(fd)
                backup = bool(BACKUP.search(entry.name))
                loose = bool(st.st_mode & (stat.S_IRGRP | stat.S_IROTH))
                if hits[0]:
                    found.append(Copy(path, backup, loose))
                elif backup and len(hits) > 1 and hits[1]:
                    found.append(Copy(path, True, loose, marker=True))
    return found, limit
