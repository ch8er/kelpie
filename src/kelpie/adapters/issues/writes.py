"""How a tracker's writes are paced and capped, for every tracker the issues adapter talks to.

- One write at a time, at least ``gap`` seconds apart (GitHub's guidance against secondary
  rate limits; Linear meters requests per hour too).
- At most ``per_tick`` writes a tick and ``per_hour`` an hour.
- Every action reserves all its writes before the first (:meth:`WriteGate.reserve`): one the
  caps cannot take whole, or one asked of a tracker that is backing off, is deferred
  (``Deferred``: not an attempt, the engine asks again later), never cut halfway.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from kelpie.core.ports import Deferred

WRITE_GAP = 1.0  # seconds between writes
MAX_WRITES_PER_TICK = 10  # beyond: deferred to the next tick (pacing blocks the loop)
MAX_WRITES_PER_HOUR = 300  # GitHub allows 500 content-creating requests an hour
HOUR = 3600.0


@dataclass(slots=True)
class WriteGate:
    gap: float = WRITE_GAP
    per_tick: int = MAX_WRITES_PER_TICK
    per_hour: int = MAX_WRITES_PER_HOUR
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    _last: float | None = field(default=None, init=False, repr=False)
    _tick: int = field(default=0, init=False, repr=False)
    _reserved: int = field(default=0, init=False, repr=False)  # writes left of this action's
    _written: int = field(default=0, init=False, repr=False)  # writes made by this action
    _hour: deque[float] = field(default_factory=deque, init=False, repr=False)

    def new_tick(self) -> None:
        self._tick = self._reserved = self._written = 0

    def reserve(self, writes: int, waiting: str | None = None) -> None:
        """Hold ``writes`` for one action. ``waiting``: why the tracker cannot be written now
        (a back-off), if it cannot; the action is then deferred whole."""
        now = self.monotonic()
        while self._hour and now - self._hour[0] > HOUR:
            self._hour.popleft()
        if waiting is not None:
            raise Deferred(f"issues: the tracker asks Kelpie to wait ({waiting})")
        if self._tick + writes > self.per_tick or len(self._hour) + writes > self.per_hour:
            raise Deferred(
                f"issues: write cap reached ({self.per_tick} a tick, {self.per_hour} an hour)"
            )
        self._reserved, self._written = writes, 0

    def pace(self) -> None:
        """Wait out the gap since the last write (before a read a write depends on, too)."""
        if self._last is not None:
            wait = self._last + self.gap - self.monotonic()
            if wait > 0:
                self.sleep(min(wait, self.gap))

    def begin(self) -> bool:
        """Before one write (paced): whether it is the action's first, after which a refused
        write is an error rather than a deferral. A write with nothing reserved reserves
        itself."""
        if self._reserved <= 0:
            self.reserve(1)
        self._reserved -= 1
        first = self._written == 0
        self.pace()
        self._tick += 1
        self._written += 1
        self._hour.append(self.monotonic())
        return first

    def defer_rest(self) -> None:
        """The first write was refused for a rate limit: nothing changed, drop the rest."""
        self._reserved = 0

    def done(self) -> None:
        self._last = self.monotonic()
