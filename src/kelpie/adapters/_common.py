"""Small helpers that several adapters share, one copy each (#248 D13): the age a status
line shows, a spool timestamp as a UTC time, a warning logged once, the act lock beside a
spool, and killing a command's process group."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.core import actlock
from kelpie.core.actlock import ActLock


def format_age(delta: timedelta) -> str:
    """An age as ``kelpie status`` shows it: whole days, hours, minutes or seconds."""
    seconds = max(0, int(delta.total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def epoch_utc(epoch: Any) -> datetime | None:
    """A spool line's epoch seconds (a number, not a bool) as a UTC time; None otherwise."""
    if isinstance(epoch, bool) or not isinstance(epoch, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), UTC)
    except (ValueError, OverflowError, OSError):
        return None


def warn_once(logger: logging.Logger, warned: set[str], key: str, msg: str, *args: object) -> None:
    """Log ``msg`` on ``logger`` the first time ``key`` is seen in ``warned``."""
    if key not in warned:
        warned.add(key)
        logger.warning(msg, *args)


def spool_act_locks(name: str, spool: Path | None, stop_dir: Path | None) -> tuple[ActLock, ...]:
    """The act lock next to the spool (audit M7): instances that read one spool see the
    same sessions, and both would stop them. None without a spool or a stop folder:
    nothing can be stopped then."""
    if spool is None or stop_dir is None:
        return ()
    # The hook makes the spool's folder (0700) on its first line: a fresh host has none.
    return (ActLock(f"{name} ({spool})", actlock.beside(spool), True),)


def kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill the command and anything it started; it is our own child in its own session."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
