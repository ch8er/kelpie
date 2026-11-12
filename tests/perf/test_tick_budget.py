"""Per-tick time budget on a large synthetic farm (roadmap "code health", #248).

A tick here is what ``kelpie serve`` does with a snapshot: every registered rule with its
defaults, then dispatch through the ledger and the alert states, in dry-run. The budgets
are several times what a 2-vCPU cloud sandbox measures (the failure message gives the
time), so a slow CI runner passes and a change that makes the tick quadratic fails.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import LedgerStore, MemoryLedgerStore
from kelpie.core.model import Snapshot
from kelpie.core.states import MemoryStateStore, StateStore
from kelpie.core.storage import SqliteLedgerStore
from kelpie.rules import REGISTRY
from tests.fakes import FakeAdapter, FakeAlerter
from tests.perf.farm import large_farm

# Measured on a 2-vCPU sandbox (2026-11-12), without coverage: ~0.5 s per tick with
# SQLite, ~0.1 s in memory, the busy farm (about 19,000 actions a tick) ~0.8 s in memory.
# CI runs under coverage, 2-3x slower (~0.9 s, ~0.4 s, ~2 s): the margin there is ~5x.
TICK_BUDGET = timedelta(seconds=5)
BUSY_TICK_BUDGET = timedelta(seconds=10)


def _engine(ledger: LedgerStore, states: StateStore) -> Engine:
    rules = [REGISTRY[name]({}) for name in sorted(REGISTRY)]
    config = EngineConfig(dry_run=True, default_mode=Mode.SUGGEST)
    return Engine(
        rules,  # type: ignore[arg-type]
        adapters=[FakeAdapter()],
        alerter=FakeAlerter(),
        config=config,
        ledger_store=ledger,
        state_store=states,
    )


def _slowest_tick(engine: Engine, snapshot: Snapshot, ticks: int = 3) -> tuple[float, int]:
    """The slowest of ``ticks`` ticks a minute apart (the first opens every state, later
    ones find them in the ledger), and how many actions the last one handled."""
    slowest, handled = 0.0, 0
    for k in range(ticks):
        at = replace(snapshot, now=snapshot.now + timedelta(minutes=k))
        start = time.perf_counter()
        handled = len(engine.tick(lambda at=at: at))
        slowest = max(slowest, time.perf_counter() - start)
    return slowest, handled


def _busy(snapshot: Snapshot) -> Snapshot:
    """The same farm with its tasks crowded onto 300 shared files: thousands of conflict
    warnings a tick, enough actions to show dispatch that grows faster than linearly."""
    rng = random.Random(1)
    return replace(
        snapshot,
        tasks=tuple(
            replace(t, files=tuple(f"src/shared_{rng.randrange(300)}.py" for _ in range(3)))
            for t in snapshot.tasks
        ),
    )


def _run(make: Callable[[], Engine], snapshot: Snapshot) -> tuple[float, int]:
    return _slowest_tick(make(), snapshot)


def test_a_tick_on_a_large_farm_fits_the_budget(tmp_path: Path) -> None:
    farm = large_farm()
    assert len(farm.tasks) == 2000 and len(farm.spans) > 10_000
    db = SqliteLedgerStore(tmp_path / "kelpie.db")
    seconds, handled = _run(lambda: _engine(db, db), farm)
    assert handled > 500  # the farm gives every rule work
    assert seconds < TICK_BUDGET.total_seconds(), f"{seconds:.2f}s for {handled} actions"
    seconds, _ = _run(lambda: _engine(MemoryLedgerStore(), MemoryStateStore()), farm)
    assert seconds < TICK_BUDGET.total_seconds(), f"{seconds:.2f}s in memory"


def test_a_busy_tick_in_memory_fits_the_budget() -> None:
    """Thousands of actions in one tick: the in-memory ledger looks entries up by index,
    not by a scan of everything it keeps (16 s a tick before, about 1 s after)."""
    farm = _busy(large_farm())
    seconds, handled = _run(lambda: _engine(MemoryLedgerStore(), MemoryStateStore()), farm)
    assert handled > 10_000
    assert seconds < BUSY_TICK_BUDGET.total_seconds(), f"{seconds:.2f}s for {handled} actions"
