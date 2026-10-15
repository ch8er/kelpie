"""Span store helpers for the app: what each snapshot sees, and retention.

Both run against a :class:`kelpie.core.spans.SpanStore`; the SQLite store reads and
writes through separate connections and writes in short batches, so neither blocks ingest
for long nor ingest them.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from kelpie.core.model import Span, SpanTotals
from kelpie.core.spans import SpanKey, SpanStore, last_at, span_key

log = logging.getLogger(__name__)

PRUNE_EVERY = timedelta(hours=1)
DEFAULT_MAX_SNAPSHOT_SPANS = 50_000
TOTALS_EVERY = timedelta(minutes=1)


class StoreObserver(Protocol):
    def span_store_failed(self) -> None: ...


class WindowObserver(Protocol):
    def snapshot_spans(self, count: int, dropped: int) -> None: ...


@dataclass(slots=True)
class SpanWindow:
    """The stored spans and per-session totals a snapshot taken at ``now`` carries.

    Spans are cached between ticks: each call fetches only the rows inserted or updated
    (links) since the last one, evicts spans that left the window, and starts over when
    the store deleted rows (``epoch``). At most ``max_spans`` are returned, the most recent
    by start; the rest are counted as dropped. Totals cover the longer ``totals_window`` and
    are recomputed at most every ``totals_every`` of snapshot time.
    """

    store: SpanStore
    window: timedelta
    totals_window: timedelta
    max_spans: int = DEFAULT_MAX_SNAPSHOT_SPANS
    observer: WindowObserver | None = None
    totals_every: timedelta = TOTALS_EVERY
    _cache: dict[SpanKey, Span] = field(default_factory=dict)
    _version: int = 0
    _epoch: int | None = None
    _totals: tuple[SpanTotals, ...] = ()
    _totals_at: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def spans(self, now: datetime) -> tuple[Span, ...]:
        cutoff = now - self.window
        with self._lock:
            epoch = self.store.epoch  # read first: a delete after this reloads next time
            if epoch != self._epoch:
                self._cache.clear()
                self._version = 0
            changes = self.store.changes(cutoff, self._version)
            for span in changes.spans:
                self._cache[span_key(span)] = span
            self._version, self._epoch = changes.version, epoch
            gone = [k for k, s in self._cache.items() if last_at(s) < cutoff]
            for key in gone:
                del self._cache[key]
            found = sorted(self._cache.values(), key=lambda s: s.start)
        dropped = max(0, len(found) - self.max_spans)
        if dropped:
            found = found[dropped:]
        if self.observer is not None:
            self.observer.snapshot_spans(len(found), dropped)
        return tuple(found)

    def totals(self, now: datetime) -> tuple[SpanTotals, ...]:
        with self._lock:
            at = self._totals_at
            if at is not None and timedelta(0) <= now - at < self.totals_every:
                return self._totals
        totals = tuple(self.store.totals_since(now - self.totals_window))
        with self._lock:
            self._totals, self._totals_at = totals, now
        return totals


def merge_spans(*groups: Iterable[Span]) -> tuple[Span, ...]:
    """Concatenate span groups, keeping the first span seen for each (trace, span) id."""
    seen: set[tuple[str, str]] = set()
    out: list[Span] = []
    for group in groups:
        for span in group:
            key = span_key(span)
            if key not in seen:
                seen.add(key)
                out.append(span)
    return tuple(out)


@dataclass(slots=True)
class SpanRetention:
    """Prunes spans past ``retention`` and beyond ``max_rows`` at most once per ``every``.

    :meth:`maybe_start` is called by the control loop after each tick and returns at once:
    the prune runs in a background thread (batched deletes, see the SQLite store), so it
    never delays dispatch or the next tick.
    """

    store: SpanStore
    clock: Callable[[], datetime]
    retention: timedelta
    max_rows: int
    observer: StoreObserver | None = None
    every: timedelta = PRUNE_EVERY
    _last: float | None = None  # monotonic time of the last start
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def maybe_start(self) -> bool:
        with self._lock:
            now = time.monotonic()
            if self._thread is not None and self._thread.is_alive():
                return False
            if self._last is not None and now - self._last < self.every.total_seconds():
                return False
            self._last = now
            self._thread = threading.Thread(
                target=self.run_once, name="kelpie-span-prune", daemon=True
            )
            self._thread.start()
            return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def run_once(self) -> int:
        started = time.monotonic()
        try:
            deleted = self.store.prune(self.clock() - self.retention, self.max_rows)
            rows = self.store.count()
        except Exception as exc:
            log.error("span retention failed: %s", exc)
            if self.observer is not None:
                self.observer.span_store_failed()
            return 0
        if deleted:
            log.info(
                "pruned %d spans in %.2fs (%d left)", deleted, time.monotonic() - started, rows
            )
        return deleted
