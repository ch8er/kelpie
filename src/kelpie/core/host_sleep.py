"""Host sleep and wake (issue #296): a suspended host is not a farm that waits.

When the machine running agents is suspended (a laptop lid closed), every heartbeat stops
and every age grows by the time it slept; on wake, sessions that were only asleep look
dead. The wall clock keeps counting through a suspend, the monotonic clock does not (Linux
``CLOCK_MONOTONIC``, macOS ``mach_absolute_time``): between two ticks, the wall clock
running ahead of the monotonic one by more than ``min_gap`` is a suspend of that length.
A forward step of the wall clock itself (a manual change, NTP stepping a badly drifted
clock) looks the same; NTP slews small drifts, far below ``min_gap``. A long tick (a slow
adapter) advances both clocks and is no suspend. On a platform whose monotonic clock
keeps counting through a suspend no jump shows; a wake log configured as data (``[host]
wake_log``) reports the sleeps there.

The engine owns the clocks: :class:`SleepWatch` reads them, rules only see the
``HostSleep`` records in ``Snapshot.host_sleeps``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from kelpie.core.model import HostSleep

log = logging.getLogger(__name__)

DEFAULT_MIN_GAP = timedelta(minutes=2)
DEFAULT_KEEP = timedelta(days=1)


@dataclass(slots=True)
class SleepWatch:
    """Finds suspends of this host between ticks and keeps those of the last ``keep``.

    Call :meth:`mark` right before the tick reads the wall clock and :meth:`observe` with
    that reading; :meth:`sleeps` then holds the stretches the host slept, newest last.
    ``on_sleep`` is called once per new sleep (to store it, or to send it to the server).
    """

    min_gap: timedelta = DEFAULT_MIN_GAP
    keep: timedelta = DEFAULT_KEEP
    agent: str | None = None
    monotonic: Callable[[], float] = time.monotonic
    on_sleep: Callable[[HostSleep], object] | None = None
    _mark: float | None = field(init=False, default=None)
    _last: tuple[datetime, float] | None = field(init=False, default=None)
    _sleeps: list[HostSleep] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if self.min_gap <= timedelta(0):
            raise ValueError("host sleep min_gap must be positive")

    def mark(self) -> None:
        """Read the monotonic clock: the tick is about to read the wall clock."""
        self._mark = self.monotonic()

    def observe(self, now: datetime) -> HostSleep | None:
        """Compare ``now`` (the tick's wall clock) with the last tick; the sleep found, if
        any. Without a :meth:`mark` since the last call, the monotonic clock is read now."""
        mono = self._mark if self._mark is not None else self.monotonic()
        self._mark = None
        last, self._last = self._last, (now, mono)
        if last is None:
            return None
        wall_gap = now - last[0]
        mono_gap = timedelta(seconds=mono - last[1])
        asleep = wall_gap - mono_gap
        if asleep < self.min_gap:
            return None
        sleep = HostSleep(start=now - asleep, end=now, agent=self.agent)
        log.warning(
            "host %s slept %s (%s to %s): wall clock ran ahead of the monotonic clock",
            self.agent or "of kelpie serve",
            _minutes(asleep),
            sleep.start.isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        )
        self.add(sleep)
        return sleep

    def add(self, sleep: HostSleep) -> None:
        """Keep a sleep found another way (an OS wake log)."""
        if any(s.start == sleep.start and s.end == sleep.end for s in self._sleeps):
            return
        self._sleeps.append(sleep)
        self._sleeps.sort(key=lambda s: s.end)
        if self.on_sleep is not None:
            try:
                self.on_sleep(sleep)
            except Exception:  # storing it is best effort; the rules still see it
                log.exception("recording a host sleep failed")

    def sleeps(self, now: datetime) -> tuple[HostSleep, ...]:
        """The sleeps that ended within ``keep`` of ``now``."""
        self._sleeps = [s for s in self._sleeps if now - s.end <= self.keep]
        return tuple(self._sleeps)


def merge_sleeps(*groups: Iterable[HostSleep]) -> tuple[HostSleep, ...]:
    """Every sleep once, by end time."""
    seen: dict[tuple[str | None, datetime, datetime], HostSleep] = {}
    for group in groups:
        for s in group:
            seen.setdefault((s.agent, s.start, s.end), s)
    return tuple(sorted(seen.values(), key=lambda s: (s.end, s.agent or "")))


def _minutes(d: timedelta) -> str:
    return f"{round(d.total_seconds() / 60)} min"
