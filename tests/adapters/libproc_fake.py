"""A fake ``libproc`` for the macOS process table and the Hermes plugin's macOS fingerprint,
shared by the tests of both (#353)."""

from __future__ import annotations

import ctypes
import errno
import os
from dataclasses import dataclass, field

from kelpie.adapters.hermes.proctable import (
    PROC_PIDVNODEPATHINFO,
    VNODEPATHINFO_SIZE,
    ProcBsdInfo,
    birth,
)

ME = os.getuid()


@dataclass
class FakeProc:
    ppid: int
    start: tuple[int, int]  # tv_sec, tv_usec
    uid: int = ME
    status: int = 2  # SRUN


@dataclass
class FakeLib:
    """``proc_pidinfo``/``proc_listallpids`` as libproc behaves: fills the caller's buffers,
    returns the bytes or pids written, sets errno."""

    procs: dict[int, FakeProc] = field(default_factory=dict)
    denied: set[int] = field(default_factory=set)
    grow: int = 0  # pids appearing between proc_listallpids' two calls, that many times
    last_errno: int = 0
    reads: int = 0

    cwds: dict[int, bytes] = field(default_factory=dict)
    args: dict[int, bytes] = field(default_factory=dict)

    def procargs(self, pid: int) -> bytes | None:
        return self.args.get(pid)

    def proc_pidinfo(self, pid: int, flavor: int, arg: int, buf: object, size: int) -> int:
        self.reads += 1
        if flavor == PROC_PIDVNODEPATHINFO:
            assert size == VNODEPATHINFO_SIZE == 2352
            if pid not in self.cwds:
                self.last_errno = errno.ESRCH
                return 0
            path = self.cwds[pid]
            ctypes.memmove(ctypes.addressof(buf) + 152, path + b"\0", len(path) + 1)  # type: ignore[call-overload]
            return size
        assert flavor == 3 and size == ctypes.sizeof(ProcBsdInfo) == 136
        if pid in self.denied:
            self.last_errno = errno.EPERM
            return 0
        p = self.procs.get(pid)
        if p is None:
            self.last_errno = errno.ESRCH
            return 0
        info = buf._obj  # type: ignore[attr-defined]  # ctypes.byref(...)
        info.pbi_pid, info.pbi_ppid, info.pbi_uid = pid, p.ppid, p.uid
        info.pbi_status = p.status
        info.pbi_start_tvsec, info.pbi_start_tvusec = p.start
        return size

    def proc_listallpids(self, buf: object, size: int) -> int:
        pids = sorted(self.procs)
        if buf is None:
            return len(pids)
        if self.grow:
            self.grow -= 1
            return size // ctypes.sizeof(ctypes.c_int)  # filled up: the table grew
        arr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_int))
        for i, pid in enumerate(pids):
            arr[i] = pid
        return len(pids)

    def errno(self) -> int:
        return self.last_errno

    def set_errno(self, value: int) -> None:
        self.last_errno = value

    def ppid(self, pid: int) -> int | None:
        """sysctl KERN_PROC_PID: every process, also those proc_pidinfo refuses."""
        p = self.procs.get(pid)
        return None if p is None else p.ppid


T0 = (1792372019, 283603)  # a start: 2026-10-19T...


def fake_farm() -> FakeLib:
    """pid 30 a submitter, pid 40 its run's agent under a gateway (20), 50 and 60 the agent's
    tool command and its child, 70 another run's agent under the same gateway."""
    return FakeLib(
        {
            1: FakeProc(0, (1792000000, 0), uid=0),
            20: FakeProc(1, (1792372000, 1)),
            30: FakeProc(1, (1792372010, 2)),
            40: FakeProc(20, (1792372011, 3)),
            50: FakeProc(40, (1792372012, 4)),
            60: FakeProc(50, (1792372013, 5)),
            70: FakeProc(20, (1792372014, 6)),
        }
    )


def stamp(lib: FakeLib, pid: int) -> str:
    """The fingerprint a macOS Hermes records: no epoch, the start in centiseconds."""
    return f"|{birth(*lib.procs[pid].start)}"
