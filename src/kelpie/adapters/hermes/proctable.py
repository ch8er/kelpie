"""Hermes adapter: the host's process table, the way Hermes fingerprints a process on it.

Kelpie ends a stopped run's worker only when the live process carries the fingerprint Hermes
recorded for it (``kelpie.adapters.hermes.worker``). Hermes computes that fingerprint
differently per system (``hermes_cli/kanban_db_dispatch.py`` ``_process_fingerprint``,
``gateway/status.py`` ``_get_process_start_time``), so each table here reads processes and
computes fingerprints exactly as Hermes does on its system:

- :class:`Procfs` (Linux): ``"<boot_id>:<pid 1 start>|<start>"``, the start being field 22 of
  ``/proc/<pid>/stat`` (clock ticks since boot);
- :class:`Libproc` (macOS): ``"|<start>"``, no epoch (Hermes reads none without ``/proc``),
  the start being the kernel's process start in centiseconds since the epoch, as psutil's
  unadjusted ``create_time(monotonic=True)`` gives it: ``round((tv_sec + tv_usec / 1e6) *
  100)``, read here with ``proc_pidinfo(PROC_PIDTBSDINFO)`` from ``libproc``. A wall-clock
  start never repeats across reboots, so it needs no epoch.

``ps`` is no fallback: its one-second resolution cannot match Hermes' centiseconds. Without
``/proc`` and without libproc nothing is verified, and nothing is signalled.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import errno
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

PROC = Path("/proc")
DEAD = frozenset({"Z", "X", "x"})  # zombie or dead: never signalled, counted as gone

# How a recorded fingerprint's epoch compares with this host's (``ProcessTable.epoch_matches``).
SAME = "same"  # the same system instance: compare start times
GONE = "gone"  # recorded on an earlier boot: that process is gone
UNCHECKED = "unchecked"  # not a fingerprint this system can check


@dataclass(frozen=True, slots=True)
class Proc:
    """A process as the table reads it. ``start`` is in the table's own unit (Linux: clock ticks
    since boot; macOS: centiseconds since the epoch): only compared within one table."""

    pid: int
    ppid: int
    state: str
    start: int
    uid: int


class ProcessTable(Protocol):
    @property
    def name(self) -> str:
        """For people: "Linux /proc", "macOS libproc"."""
        ...

    @property
    def pinned(self) -> bool:
        """Signals go through a pidfd (a pid cannot be recycled under it)."""
        ...

    def unavailable(self) -> str | None:
        """Why processes cannot be verified here at all, or None."""
        ...

    def read(self, pid: int) -> Proc | None:
        """The process ``pid``, or None when it is gone or cannot be read."""
        ...

    def missing(self, pid: int) -> str | None:
        """For a pid :meth:`read` cannot see: None when it is gone, else why Kelpie cannot
        tell (it may be running)."""
        ...

    def pids(self) -> Iterable[int]:
        """Every pid on the system."""
        ...

    def parent(self, pid: int) -> int | None:
        """The parent pid of any process, also one :meth:`read` may not read (another
        user's), or None when it is gone."""
        ...

    def epoch(self) -> tuple[str, str | None]:
        """Hermes' instantiation epoch on this system, and why it cannot be used (or None)."""
        ...

    def epoch_matches(self, recorded: str, epoch: str) -> str:
        """``SAME``, ``GONE``, ``UNCHECKED`` or a reason to refuse, for a recorded epoch."""
        ...

    def other_start(self) -> str | None:
        """For a live pid whose start differs from the recorded one: None when that means the
        worker is gone (the pid was reused), else why Kelpie cannot tell."""
        ...

    def fingerprint(self, p: Proc, epoch: str) -> str:
        """Hermes' fingerprint of ``p`` on this system."""
        ...


# --- Linux ----------------------------------------------------------------------------------


def read_proc(pid: int, proc: Path = PROC) -> Proc | None:
    """The process ``pid`` from ``/proc``, or None when it is gone or unreadable."""
    try:
        text = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        uid = (proc / str(pid)).stat().st_uid
    except OSError:
        return None
    # "<pid> (<comm>) <state> <ppid> ...": comm may hold spaces and parentheses.
    try:
        fields = text.rsplit(")", 1)[1].split()
        return Proc(pid, int(fields[1]), fields[0], int(fields[19]), uid)
    except (IndexError, ValueError):
        return None


def instantiation_epoch(proc: Path = PROC) -> str:
    """``"<boot_id>:<pid 1 start>"``, as Hermes' ``current_instantiation_epoch``."""
    boot = pid1 = ""
    with contextlib.suppress(OSError):
        boot = (proc / "sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    first = read_proc(1, proc)
    if first is not None:
        pid1 = str(first.start)
    return f"{boot}:{pid1}" if (boot or pid1) else ""


def fingerprint(p: Proc, epoch: str) -> str:
    """Hermes' ``_process_fingerprint`` of a process."""
    return f"{epoch}|{p.start}"


_HIDDEN = "Kelpie cannot see other users' processes (/proc is mounted with hidepid)"


@dataclass(frozen=True, slots=True)
class Procfs:
    """Linux: ``/proc`` (``root`` for tests)."""

    root: Path = PROC
    name: ClassVar[str] = "Linux /proc"
    pinned: ClassVar[bool] = True

    def unavailable(self) -> str | None:
        if not (self.root / "self" / "stat").exists():
            return "processes cannot be verified on this system"
        return None

    def read(self, pid: int) -> Proc | None:
        return read_proc(pid, self.root)

    def missing(self, pid: int) -> str | None:
        if (self.root / str(pid)).exists():
            return f"Kelpie cannot read /proc/{pid}"
        if self.hidepid():
            return _HIDDEN
        return None

    def pids(self) -> Iterable[int]:
        with contextlib.suppress(OSError):
            return [int(e.name) for e in self.root.iterdir() if e.name.isdigit()]
        return []

    def parent(self, pid: int) -> int | None:
        p = self.read(pid)
        return None if p is None else p.ppid

    def epoch(self) -> tuple[str, str | None]:
        epoch = instantiation_epoch(self.root)
        boot, _, pid1 = epoch.partition(":")
        if not boot or not pid1:  # Hermes' fingerprint needs both; without them, nothing is proven
            return epoch, "Kelpie cannot read this system's boot id or pid 1 start time"
        return epoch, None

    def epoch_matches(self, recorded: str, epoch: str) -> str:
        if not recorded:
            return UNCHECKED
        if recorded == epoch:
            return SAME
        if recorded.split(":")[0] == epoch.split(":")[0]:  # same boot, another pid 1
            return "Hermes runs in another PID namespace than Kelpie (a container?)"
        return GONE  # recorded before a reboot: that process is gone

    def other_start(self) -> str | None:
        return None  # clock ticks since this boot: another start is another process

    def fingerprint(self, p: Proc, epoch: str) -> str:
        return fingerprint(p, epoch)

    def hidepid(self) -> bool:
        """Is ``/proc`` mounted with ``hidepid`` (other users' processes invisible)? Any proc
        mount with it counts, and a process exempt from it (root, the ``gid=`` group) is not
        told apart: both err toward refusing, never toward signalling."""
        try:
            lines = (self.root / "self" / "mountinfo").read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            _, sep, rest = line.partition(" - ")
            fields = rest.split()
            if sep and len(fields) >= 3 and fields[0] == "proc":
                options = dict(o.partition("=")[::2] for o in fields[2].split(","))
                if options.get("hidepid", "0") not in ("0", "off"):
                    return True
        return False


# --- macOS ----------------------------------------------------------------------------------

PROC_PIDTBSDINFO = 3
MAXCOMLEN = 16
PID_MAX = 2**31 - 1  # pid_t; macOS hands out pids up to 99999
SZOMB = 5  # pbi_status of a zombie (sys/proc.h)


class ProcBsdInfo(ctypes.Structure):
    """``struct proc_bsdinfo`` (``sys/proc_info.h``), 136 bytes."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (2 * MAXCOMLEN)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def birth(tv_sec: int, tv_usec: int) -> int:
    """Hermes' macOS start time: psutil's ``tv_sec + tv_usec / 1000000.0`` (the same doubles),
    in centiseconds as Hermes rounds it."""
    return round((float(tv_sec) + tv_usec / 1000000.0) * 100)


class Libproc:
    """macOS: ``libproc`` (``lib`` for tests: anything with ``proc_pidinfo`` and
    ``proc_listallpids`` that behave like the C functions, and ``errno()`` for the last
    error)."""

    name: ClassVar[str] = "macOS libproc"
    pinned: ClassVar[bool] = False  # no pidfd: the start time is read again before each signal

    def __init__(self, lib: Any | None = None, error: str | None = None) -> None:
        self._lib = lib
        self._error = error

    @classmethod
    def load(cls) -> Libproc:
        """The system's libproc, or a table that says why it is unavailable."""
        try:
            lib = _CLibproc()
        except (OSError, AttributeError) as exc:  # no library, or a missing symbol
            return cls(error=f"libproc cannot be loaded ({exc})")
        return cls(lib)

    def unavailable(self) -> str | None:
        if self._lib is None:
            return self._error or "libproc cannot be loaded"
        return None

    def _bsdinfo(self, pid: int) -> tuple[ProcBsdInfo | None, int]:
        """``proc_pidinfo(PROC_PIDTBSDINFO)``: the info, or None and the errno."""
        info = ProcBsdInfo()
        size = ctypes.sizeof(info)
        if self._lib is None or not 0 < pid <= PID_MAX:  # ctypes would truncate a larger int
            return None, errno.ESRCH
        self._lib.set_errno(0)
        got = self._lib.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), size)
        if got == size:
            return info, 0
        err = self._lib.errno() if got == 0 else errno.EIO  # a short read sets no errno
        return None, err

    def read(self, pid: int) -> Proc | None:
        info, _ = self._bsdinfo(pid)
        if info is None:
            return None
        state = "Z" if info.pbi_status == SZOMB else "R"
        start = birth(info.pbi_start_tvsec, info.pbi_start_tvusec)
        return Proc(pid, int(info.pbi_ppid), state, start, int(info.pbi_uid))

    def missing(self, pid: int) -> str | None:
        if self._lib is None:
            return self.unavailable()
        info, err = self._bsdinfo(pid)
        if info is None and err == errno.ESRCH:
            return None  # no such process (or a zombie: libproc does not show them)
        if info is not None:  # readable now: it was not a moment ago, so Kelpie cannot tell
            return f"process {pid} changed while Kelpie read it"
        return f"Kelpie cannot read process {pid} ({errno.errorcode.get(err, err)})"

    def parent(self, pid: int) -> int | None:
        p = self.read(pid)
        if p is not None:
            return p.ppid
        if self._lib is None or not 0 < pid <= PID_MAX:
            return None
        return self._lib.ppid(pid)  # another user's: sysctl reads every process

    def pids(self) -> Iterable[int]:
        if self._lib is None:
            return []
        for _ in range(3):  # the table may grow between the two calls
            count = self._lib.proc_listallpids(None, 0)
            if count <= 0:
                return []
            buf = (ctypes.c_int * (count + 64))()
            got = self._lib.proc_listallpids(buf, ctypes.sizeof(buf))
            if 0 < got < len(buf):
                return [p for p in buf[:got] if p > 0]
        return []

    def epoch(self) -> tuple[str, str | None]:
        return "", None  # Hermes reads no epoch without /proc

    def epoch_matches(self, recorded: str, epoch: str) -> str:
        return SAME if recorded == "" else UNCHECKED

    def other_start(self) -> str | None:
        # Usually a reused pid, but a Hermes whose psutil predates the unadjusted read
        # recorded a start shifted by the last clock step: Kelpie cannot tell which.
        return "its start time differs from Hermes' record (a reused pid, or a clock step)"

    def fingerprint(self, p: Proc, epoch: str) -> str:
        return f"{epoch}|{p.start}"


class _CLibproc:
    """The real ``libproc`` through ctypes."""

    def __init__(self) -> None:
        try:
            path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
            lib = ctypes.CDLL(path, use_errno=True)
            lib.proc_pidinfo  # noqa: B018 (a missing symbol raises here)
        except (OSError, AttributeError):
            lib = ctypes.CDLL(None, use_errno=True)  # libSystem exports proc_* too
        self.proc_pidinfo = lib.proc_pidinfo
        self.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        self.proc_pidinfo.restype = ctypes.c_int
        self.proc_listallpids = lib.proc_listallpids
        self.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.proc_listallpids.restype = ctypes.c_int
        libc = ctypes.CDLL(None, use_errno=True)
        self._sysctl = libc.sysctl
        self._sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self._sysctl.restype = ctypes.c_int

    @staticmethod
    def errno() -> int:
        return ctypes.get_errno()

    @staticmethod
    def set_errno(value: int) -> None:
        ctypes.set_errno(value)

    def ppid(self, pid: int) -> int | None:
        """``sysctl {CTL_KERN, KERN_PROC, KERN_PROC_PID, pid}``: ``kinfo_proc.kp_eproc.e_ppid``,
        readable for every process (as psutil reads it)."""
        buf = ctypes.create_string_buffer(KINFO_PROC_SIZE)
        size = ctypes.c_size_t(KINFO_PROC_SIZE)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        if self._sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
        if size.value != KINFO_PROC_SIZE:
            return None  # 0: no such process
        return int.from_bytes(buf.raw[E_PPID_OFFSET : E_PPID_OFFSET + 4], sys.byteorder)


# struct kinfo_proc on 64-bit macOS: extern_proc (296 bytes) then eproc, e_ppid at 264 in it.
KINFO_PROC_SIZE = 648
E_PPID_OFFSET = 560


# --- the host's table -----------------------------------------------------------------------


class _Unsupported:
    """A system Kelpie cannot read processes on: nothing is verified, nothing signalled."""

    name = "none"
    pinned = False

    def __init__(self, platform: str) -> None:
        self._why = f"processes cannot be verified on this system ({platform})"

    def unavailable(self) -> str | None:
        return self._why

    def read(self, pid: int) -> Proc | None:
        return None

    def missing(self, pid: int) -> str | None:
        return self._why

    def pids(self) -> Iterable[int]:
        return []

    def parent(self, pid: int) -> int | None:
        return None

    def epoch(self) -> tuple[str, str | None]:
        return "", self._why

    def epoch_matches(self, recorded: str, epoch: str) -> str:
        return UNCHECKED

    def other_start(self) -> str | None:
        return self._why

    def fingerprint(self, p: Proc, epoch: str) -> str:
        return ""


def host_table(platform: str | None = None) -> ProcessTable:
    """The process table of this host: ``/proc`` on Linux, libproc on macOS."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("linux"):
        return Procfs()
    if platform == "darwin":
        return Libproc.load()
    return _Unsupported(platform)
