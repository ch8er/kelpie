"""Span store helpers for the app: what each snapshot sees, and retention.

Both run against a :class:`kelpie.core.spans.SpanStore`; the SQLite store has its own lock
and batched writes, so neither blocks ingest for long nor ingest them.
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
from kelpie.core.spans import SpanStore, span_key

log = logging.getLogger(__name__)

PRUNE_EVERY = timedelta(hours=1)


class StoreObserver(Protocol):
    def set_span_rows(self, rows: int) -> None: ...

    def span_store_failed(self) -> None: ...


@dataclass(frozen=True, slots=True)
class SpanWindow:
    """Reads the spans and per-session totals a snapshot taken at ``now`` should carry."""

    store: SpanStore
    window: timedelta
    totals_window: timedelta

    def spans(self, now: datetime) -> tuple[Span, ...]:
        return tuple(self.store.spans_since(now - self.window))

    def totals(self, now: datetime) -> tuple[SpanTotals, ...]:
        return tuple(self.store.totals_since(now - self.totals_window))


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
        if self.observer is not None:
            self.observer.set_span_rows(rows)
        if deleted:
            log.info(
                "pruned %d spans in %.2fs (%d left)", deleted, time.monotonic() - started, rows
            )
        return deleted
