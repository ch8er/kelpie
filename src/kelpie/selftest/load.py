"""This host's load and swap, for ``kelpie selftest`` (issue #483).

A self-test on a host that is too busy (load in the hundreds, swap full: simulators and
agents running) times out and reads like a Kelpie or Hermes failure. The self-test reads the
load before its first step, skips the run on an overloaded host, and prints the load when a
step times out, so a busy host is told apart from a broken stop.

Read locally: ``os.getloadavg()`` and the CPU count, and swap from ``/proc/meminfo`` (Linux)
or ``sysctl vm.swapusage`` (macOS). The self-test runs on the Hermes host itself, so this is
the machine the steps run on. Any part that cannot be read is None and judged by the rest.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAX_LOAD = 4.0  # load1 per CPU core above which the host is overloaded
DEFAULT_MAX_SWAP = 0.9  # share of swap used above which the host is overloaded
MEMINFO = Path("/proc/meminfo")
_SWAPUSAGE = re.compile(r"total\s*=\s*([\d.]+)([KMGT]?)\s+used\s*=\s*([\d.]+)([KMGT]?)")
_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}


@dataclass(frozen=True, slots=True)
class HostLoad:
    load1: float | None  # the 1-minute load average
    cores: int | None
    swap_used: float | None  # share of swap in use, 0..1; None: no swap, or unknown

    @property
    def per_core(self) -> float | None:
        if self.load1 is None or not self.cores:
            return None
        return self.load1 / self.cores

    def describe(self) -> str:
        """``load 450 (45.0 per core), swap 98% used``; parts that are unknown say so."""
        if self.load1 is None:
            load = "load unknown"
        elif self.per_core is None:
            load = f"load {self.load1:g}"
        else:
            load = f"load {self.load1:g} ({self.per_core:.1f} per core)"
        swap = "swap unknown" if self.swap_used is None else f"swap {self.swap_used:.0%} used"
        return f"{load}, {swap}"

    def overloaded(
        self, max_load: float = DEFAULT_MAX_LOAD, max_swap: float = DEFAULT_MAX_SWAP
    ) -> bool:
        """Above either threshold: load per core over ``max_load``, or more than
        ``max_swap`` of swap in use. Unknown parts never make a host overloaded."""
        per_core = self.per_core
        if per_core is not None and per_core > max_load:
            return True
        return self.swap_used is not None and self.swap_used > max_swap


def read_host_load(
    meminfo: Path = MEMINFO,
    platform: str = sys.platform,
    sysctl: Callable[[], str | None] | None = None,
) -> HostLoad:
    """This host's load and swap; never raises."""
    try:
        load1: float | None = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = None
    swap = _linux_swap(meminfo) if platform.startswith("linux") else None
    if swap is None and platform == "darwin":
        swap = _darwin_swap((sysctl or _sysctl_swapusage)())
    return HostLoad(load1, _cores(), swap)


def _cores() -> int | None:
    """The CPUs this process may run on (a container's or affinity's limit), else all."""
    try:
        return len(os.sched_getaffinity(0)) or os.cpu_count()
    except (AttributeError, OSError):  # macOS has no sched_getaffinity
        return os.cpu_count()


def _linux_swap(meminfo: Path) -> float | None:
    try:
        text = meminfo.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in {"SwapTotal", "SwapFree"} and parts and parts[0].isdigit():
            values[key] = int(parts[0])
    total, free = values.get("SwapTotal"), values.get("SwapFree")
    if not total or free is None:
        return None  # no swap configured: nothing to fill
    return max(0.0, min(1.0, (total - free) / total))


def _sysctl_swapusage() -> str | None:
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _darwin_swap(text: str | None) -> float | None:
    """``total = 2048.00M  used = 1536.00M  free = 512.00M  (encrypted)``."""
    m = _SWAPUSAGE.search(text or "")
    if m is None:
        return None
    total = float(m[1]) * _UNITS[m[2]]
    used = float(m[3]) * _UNITS[m[4]]
    if total <= 0:
        return None
    return max(0.0, min(1.0, used / total))
