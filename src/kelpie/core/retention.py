"""Retention of the action ledger and of closed alert states (#318).

``[storage] retention`` (30 days) and ``max_rows`` (a million) bound the raw action ledger;
closed alert states go with it, open states and snoozes never. Each UTC day is summed into
daily aggregates (per rule, action kind and mode) before its raw rows go, and the sums are
kept for good: the report's before/after reads a rule's mode change from them once raw rows
are gone. The size cap never prunes within :func:`ledger_keep` (the ledger's own windows:
cooldown, retries, once actions), so a full ledger never lets an action repeat.

:class:`LedgerRetention` runs the prune at most once per ``every`` in a background thread,
started by the control loop after a tick: it never delays dispatch or the next tick (the
SQLite pruner writes in short transactions on its own connection).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.config import EngineConfig
from kelpie.core.serde import parse_duration

log = logging.getLogger(__name__)

DEFAULT_RETENTION = timedelta(days=30)
DEFAULT_MAX_ROWS = 1_000_000
MAX_RETENTION = timedelta(days=3650)
PRUNE_EVERY = timedelta(hours=1)
STORAGE_KEYS = frozenset({"path", "retention", "max_rows"})


@dataclass(frozen=True, slots=True)
class StorageRetention:
    """``[storage] retention`` and ``max_rows``: how long raw ledger rows (and closed alert
    states) are kept, and how many raw ledger rows at most."""

    retention: timedelta = DEFAULT_RETENTION
    max_rows: int = DEFAULT_MAX_ROWS


def ledger_keep(engine: EngineConfig) -> timedelta:
    """What the ledger itself reads back: a day at least, its cooldown and retry window,
    and its once-actions' window. Neither retention nor the size cap prunes within it."""
    return max(timedelta(days=1), engine.cooldown, engine.retry_after, engine.once_window)


def load_storage_retention(section: Mapping[str, Any], engine: EngineConfig) -> StorageRetention:
    """``retention`` and ``max_rows`` of ``[storage]``; ValueError when invalid."""
    raw = section.get("retention", "30d")
    if not isinstance(raw, str):
        raise ValueError('[storage] retention must be a duration string such as "30d"')
    retention = parse_duration(raw)
    keep = ledger_keep(engine)
    if retention < keep:
        raise ValueError(
            f"[storage] retention must cover the ledger's own windows (engine once_window, "
            f"cooldown, retry_after, and a day at least): at least {_days(keep)}"
        )
    if retention > MAX_RETENTION:
        raise ValueError("[storage] retention must be at most 3650d")
    max_rows = section.get("max_rows", DEFAULT_MAX_ROWS)
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows <= 0:
        raise ValueError("[storage] max_rows must be a positive integer")
    return StorageRetention(retention, max_rows)


def _days(span: timedelta) -> str:
    days = span / timedelta(days=1)
    return f"{days:g}d" if days == int(days) else f"{span.total_seconds() / 3600:g}h"


@dataclass(slots=True)
class LedgerRetention:
    """Runs ``prune(now)`` at most once per ``every``, in a background thread.

    :meth:`maybe_start` is called by the control loop after each tick and returns at once.
    ``prune`` returns what it removed (logged); ``close`` stops a running prune at its next
    batch (:meth:`stop`).
    """

    prune: Callable[[datetime], object]
    clock: Callable[[], datetime]
    every: timedelta = PRUNE_EVERY
    close: Callable[[], None] | None = None
    failures: int = 0
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
                target=self.run_once, name="kelpie-ledger-prune", daemon=True
            )
            self._thread.start()
            return True

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        if self.close is not None:
            self.close()
        self.join(timeout)

    def run_once(self) -> object:
        started = time.monotonic()
        try:
            pruned = self.prune(self.clock())
        except Exception as exc:
            self.failures += 1
            log.error("ledger retention failed: %s", exc)
            return None
        if any(pruned) if isinstance(pruned, tuple) else pruned:
            log.info("ledger retention: %s in %.2fs", pruned, time.monotonic() - started)
        return pruned
