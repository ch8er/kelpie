"""Action-ledger retention (#318): raw rows pruned by age and size, closed alert states with
them, daily sums written first and kept, read by before/after once raw rows are gone; open
states and active snoozes never pruned; the prune never holds the writer slot for long."""

from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import ConfigError, build_app, load_settings
from kelpie.core.actions import ActionKind, Severity
from kelpie.core.budgets import PriceTable
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.ledger import OBSERVE_ONLY, LedgerEntry, MemoryLedgerStore, Outcome
from kelpie.core.model import Snapshot, Snooze
from kelpie.core.retention import (
    DEFAULT_MAX_ROWS,
    DEFAULT_RETENTION,
    LedgerRetention,
    StorageRetention,
    ledger_keep,
    load_storage_retention,
)
from kelpie.core.states import MemoryStateStore, OpenState
from kelpie.core.storage import (
    MIGRATIONS,
    ActionDay,
    SqliteLedgerPruner,
    SqliteLedgerStore,
    read_action_days,
)
from kelpie.doctor import DATABASE_CHECK, check_database
from kelpie.report.config import ReportSettings
from kelpie.report.daily import build_report
from kelpie.report.fixes import MODE
from kelpie.report.run import ReportSources, load_inputs
from kelpie.report.store import ReportStore
from kelpie.traces.outputs import OutputSettings

NOW = datetime(2026, 4, 15, 12, tzinfo=UTC)  # the fake clock
KEEP = timedelta(days=7)
MONTH = timedelta(days=30)


def entry(
    at: datetime,
    rule: str = "stale_blocks",
    mode: Mode = Mode.ACT,
    outcome: Outcome = Outcome.EXECUTED,
    kind: ActionKind = ActionKind.UNBLOCK,
    suppressed: int = 0,
    observe_only: bool = False,
    n: int = 0,
) -> LedgerEntry:
    return LedgerEntry(
        at=at,
        fingerprint=f"fp-{rule}-{n}",
        subject=f"task:T-{n}",
        rule=rule,
        kind=kind,
        mode=mode,
        outcome=outcome,
        reason="synthetic",
        task_id=f"T-{n}",
        params={OBSERVE_ONLY: True} if observe_only else {},
        suppressed=suppressed,
    )


def days_ago(days: float, hours: float = 0) -> datetime:
    return NOW - timedelta(days=days) + timedelta(hours=hours)


def raw_rows(db: Path) -> int:
    with sqlite3.connect(db) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0])


def sums(db: Path) -> list[ActionDay]:
    with sqlite3.connect(db) as conn:
        return read_action_days(conn, date(2000, 1, 1), date(2100, 1, 1))


def ledger(db: Path, entries: list[LedgerEntry]) -> None:
    store = SqliteLedgerStore(db)
    try:
        for e in entries:
            store.append(e)
    finally:
        store.close()


def prune(db: Path, max_rows: int = DEFAULT_MAX_ROWS, batch: int = 500) -> tuple[int, int, int]:
    pruner = SqliteLedgerPruner(db, batch=batch)
    try:
        return tuple(pruner.prune(NOW, MONTH, max_rows, KEEP))  # type: ignore[return-value]
    finally:
        pruner.close()


def expected(entries: list[LedgerEntry]) -> dict[tuple[str, str, str, str], tuple[int, ...]]:
    """What the daily sums must hold, counted here from the entries themselves."""
    out: dict[tuple[str, str, str, str], Counter[str]] = {}
    for e in entries:
        key = (e.at.astimezone(UTC).date().isoformat(), e.rule, str(e.kind), str(e.mode))
        c = out.setdefault(key, Counter())
        c["entries"] += 1
        c["executed"] += e.outcome is Outcome.EXECUTED
        c["failed"] += e.outcome is Outcome.FAILED
        c["suppressed"] += (e.outcome is Outcome.SUPPRESSED) + e.suppressed
    return {k: (c["entries"], c["executed"], c["suppressed"], c["failed"]) for k, c in out.items()}


def test_daily_sums_are_exact_and_written_before_raw_rows_go(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    old = [
        entry(days_ago(40, 1), outcome=Outcome.EXECUTED, n=1),
        entry(days_ago(40, 2), outcome=Outcome.FAILED, n=2),
        entry(days_ago(40, 3), outcome=Outcome.EXECUTED, suppressed=4, n=3),
        entry(days_ago(40, 4), outcome=Outcome.SUPPRESSED, n=4),
        entry(days_ago(40, 5), mode=Mode.OBSERVE, outcome=Outcome.OBSERVED, observe_only=True),
        entry(days_ago(39, 1), rule="exit_contract", kind=ActionKind.REQUEUE, n=5),
        entry(days_ago(39, 2), rule="exit_contract", kind=ActionKind.REQUEUE, n=6),
        entry(days_ago(39, 3), rule="needs_you", kind=ActionKind.ESCALATE,
              outcome=Outcome.ALERTED, n=7),
    ]  # fmt: skip
    recent = [entry(days_ago(29), n=8), entry(days_ago(1), n=9)]
    ledger(db, old + recent)
    days, deleted, _ = prune(db)
    assert (days, deleted) == (2, len(old))
    assert raw_rows(db) == len(recent)  # within retention: kept raw, not summed yet
    found = {(d.day.isoformat(), d.rule, d.kind, d.mode): d for d in sums(db)}
    assert {k: (d.entries, d.executed, d.suppressed, d.failed) for k, d in found.items()} == (
        expected(old)
    )
    # the mode shows on entries that are not suppressed and not an observe-only check's
    act = found[(days_ago(40).date().isoformat(), "stale_blocks", "unblock", "act")]
    assert (act.first_at, act.last_at) == (days_ago(40, 1), days_ago(40, 3))
    observe = found[(days_ago(40).date().isoformat(), "stale_blocks", "unblock", "observe")]
    assert observe.first_at is None and observe.last_at is None
    # pruning again sums nothing twice
    assert prune(db) == (0, 0, 0)
    assert len(sums(db)) == len(found)


def test_a_prune_cut_short_never_sums_a_day_twice(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    old = [entry(days_ago(40, h), n=h) for h in range(10)]
    ledger(db, old)
    calls = 0
    pruner: SqliteLedgerPruner | None = None

    def stop_after_first_batch() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # the day is summed (1) and one batch of 3 rows deleted (2)
            assert pruner is not None
            pruner._closing.set()

    pruner = SqliteLedgerPruner(db, batch=3, on_batch=stop_after_first_batch)
    pruner.prune(NOW, MONTH, DEFAULT_MAX_ROWS, KEEP)
    pruner.close()
    assert raw_rows(db) == 7  # cut short
    prune(db)  # the next run deletes the rest and keeps the first run's sums
    assert raw_rows(db) == 0
    [day] = sums(db)
    assert (day.entries, day.executed) == (10, 10)


def test_open_states_and_active_snoozes_are_never_pruned(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    store = SqliteLedgerStore(db)

    def state(sid: str, opened: datetime) -> OpenState:
        return OpenState(sid, "stale_blocks", opened, opened, opened, 1, Severity.WARNING,
                         "s", "T-1", None, Mode.ACT)  # fmt: skip

    try:
        store.save(state("open-for-months", days_ago(200)))
        store.save(state("closed-long-ago", days_ago(90)))
        store.close_state("closed-long-ago", days_ago(60))
        store.save(state("closed-lately", days_ago(20)))
        store.close_state("closed-lately", days_ago(10))
        store.put_snooze(Snooze("T-9", NOW + timedelta(days=60), "release", None, days_ago(120)))
    finally:
        store.close()
    assert prune(db)[2] == 1
    with sqlite3.connect(db) as conn:
        left = {r[0] for r in conn.execute("SELECT id FROM states")}
        snoozes = [r[0] for r in conn.execute("SELECT task_id FROM snoozes")]
    assert left == {"open-for-months", "closed-lately"}
    assert snoozes == ["T-9"]


def test_the_size_cap_prunes_whole_old_days_but_never_the_ledgers_own_windows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "kelpie.db"
    entries = [entry(days_ago(d, h), n=d * 10 + h) for d in (20, 15, 10, 3, 1) for h in range(4)]
    ledger(db, entries)
    days, deleted, _ = prune(db, max_rows=6)
    # 20 rows over 6: days 20, 15 and 10 go (all older than the 7-day keep); 3 and 1 stay
    # even though 8 rows are still over the cap: the cooldown and once windows read them
    assert (days, deleted) == (3, 12)
    assert raw_rows(db) == 8
    assert [d.entries for d in sums(db)] == [4, 4, 4]


def test_a_large_ledger_prunes_in_batches_and_never_holds_the_writer(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    old = [entry(days_ago(45 - i // 1000, (i % 1000) / 100), n=i) for i in range(3000)]
    ledger(db, old)
    # a dispatching ledger that does not wait at all for the writer slot: it fails at
    # once if the prune held it
    tick = SqliteLedgerStore(db, busy_timeout_ms=0)
    written = 0

    def dispatch() -> None:
        nonlocal written
        tick.append(entry(NOW, rule="exit_contract", n=10_000 + written))
        written += 1

    pruner = SqliteLedgerPruner(db, batch=200, on_batch=dispatch)
    try:
        assert tuple(pruner.prune(NOW, MONTH, DEFAULT_MAX_ROWS, KEEP))[:2] == (3, 3000)
    finally:
        pruner.close()
        tick.close()
    assert written == 3 + 3000 // 200  # three sums, fifteen batches, each its own commit
    assert raw_rows(db) == written
    assert sum(d.entries for d in sums(db)) == 3000


def test_retention_runs_in_the_background_and_never_blocks_a_tick(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    ledger(db, [entry(days_ago(40, i / 10), n=i) for i in range(50)])
    inside, release = threading.Event(), threading.Event()

    def held() -> None:
        inside.set()
        assert release.wait(5)

    pruner = SqliteLedgerPruner(db, batch=10, on_batch=held)
    retention = LedgerRetention(
        lambda now: pruner.prune(now, MONTH, DEFAULT_MAX_ROWS, KEEP), lambda: NOW
    )
    tick = SqliteLedgerStore(db, busy_timeout_ms=0)
    try:
        assert retention.maybe_start()  # returns at once
        assert inside.wait(5)  # the prune is between two transactions
        tick.append(entry(NOW, n=999))  # the tick's write goes through
        assert not retention.maybe_start()  # one prune at a time, and once an hour
        release.set()
        retention.join(5)
    finally:
        tick.close()
        pruner.close()
    assert raw_rows(db) == 1 and retention.failures == 0


def test_a_failed_prune_is_counted_and_logged() -> None:
    def broken(now: datetime) -> object:
        raise sqlite3.OperationalError("disk I/O error")

    retention = LedgerRetention(broken, lambda: NOW)
    assert retention.run_once() is None and retention.failures == 1


def test_the_in_memory_ledger_and_closed_states_are_bounded() -> None:
    store = MemoryLedgerStore(max_entries=100)
    for i in range(250):
        store.append(entry(days_ago(40) + timedelta(minutes=i), n=i))
    assert 90 <= len(store.entries) <= 100
    assert store.entries[-1].task_id == "T-249"  # the newest kept
    store.append(entry(days_ago(1), n=1000))
    held = len(store.entries)
    assert store.prune(NOW - MONTH) == held - 1 and len(store.entries) == 1
    states = MemoryStateStore()
    for sid, closed in (("old", days_ago(40)), ("new", days_ago(1))):
        state = OpenState(sid, "r", days_ago(50), days_ago(50), days_ago(50), 1,
                          Severity.WARNING, "s")  # fmt: skip
        states.save(state)
        states.close_state(sid, closed)
    states.save(OpenState("open", "r", days_ago(90), days_ago(90), days_ago(90), 1,
                          Severity.WARNING, "s"))  # fmt: skip
    assert states.prune_closed(NOW - MONTH) == 1
    assert [s.id for s, _ in states.closed] == ["new"] and "open" in states.states


def test_storage_config() -> None:
    engine = EngineConfig()
    assert load_storage_retention({}, engine) == StorageRetention(
        DEFAULT_RETENTION, DEFAULT_MAX_ROWS
    )
    found = load_storage_retention({"retention": "90d", "max_rows": 5000}, engine)
    assert found == StorageRetention(timedelta(days=90), 5000)
    assert ledger_keep(engine) == timedelta(days=7)  # the once window
    with pytest.raises(ValueError, match="at least 7d"):
        load_storage_retention({"retention": "3d"}, engine)
    for bad in (0, -1, True, "10"):
        with pytest.raises(ValueError, match="max_rows"):
            load_storage_retention({"max_rows": bad}, engine)
    with pytest.raises(ValueError, match="3650d"):
        load_storage_retention({"retention": "4000d"}, engine)


def test_storage_keys_in_the_config_file(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text('[storage]\npath = "k.db"\nretention = "60d"\nmax_rows = 2000\n')
    assert load_settings(path).storage == StorageRetention(timedelta(days=60), 2000)
    path.write_text('[storage]\nretain = "60d"\n')
    with pytest.raises(ConfigError, match="unknown \\[storage\\] keys"):
        load_settings(path)


def test_kelpie_serve_prunes_off_the_dispatch_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    db = tmp_path / "kelpie.db"
    ledger(db, [entry(days_ago(40), n=1), entry(days_ago(1), n=2)])
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{db}"\n[traces]\nenabled = false\n')
    app = build_app(load_settings(path), clock=lambda: NOW)
    try:
        assert app.ledger_retention is not None
        assert app.loop.housekeeping is not None
        app.loop.housekeeping()  # after a tick: starts the prune and returns
        app.ledger_retention.join(5)
    finally:
        app.close()
    assert raw_rows(db) == 1 and len(sums(db)) == 1


def test_an_existing_database_is_migrated_and_pruned(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    conn = sqlite3.connect(db)
    for version in range(1, 11):  # a database of the release before #318
        conn.executescript(
            f"BEGIN;\n{MIGRATIONS[version]}\nPRAGMA user_version = {version};\nCOMMIT;"
        )
    conn.execute(
        "INSERT INTO actions (at, fingerprint, subject, rule, kind, mode, outcome, reason,"
        " params) VALUES (?, 'fp', 'task:T-1', 'stale_blocks', 'unblock', 'act', 'executed',"
        " 'r', '{}')",
        (days_ago(40).isoformat(timespec="microseconds"),),
    )
    conn.commit()
    conn.close()
    assert prune(db)[:2] == (1, 1)
    with sqlite3.connect(db) as check:
        assert check.execute("PRAGMA user_version").fetchone()[0] == max(MIGRATIONS)
    assert [d.entries for d in sums(db)] == [1]


def test_before_after_reads_a_mode_change_from_the_sums_once_raw_rows_are_gone(
    tmp_path: Path,
) -> None:
    db = tmp_path / "kelpie.db"
    day = date(2026, 3, 2)  # the report's day; the rule went live the day before at 10:00
    switch = datetime(2026, 3, 1, 10, tzinfo=UTC)
    entries = [
        *(entry(switch - timedelta(hours=h), mode=Mode.OBSERVE, outcome=Outcome.OBSERVED, n=h)
          for h in (30, 20, 5)),
        entry(switch, n=50),
        entry(switch + timedelta(hours=3), n=51),
        entry(switch + timedelta(hours=4), mode=Mode.OBSERVE, outcome=Outcome.OBSERVED,
              observe_only=True, n=52),  # an observe-only check: not the rule's mode
    ]  # fmt: skip
    ledger(db, entries)
    sources = ReportSources(db, ReportSettings(), PriceTable(), OutputSettings(), False, {})
    board = Snapshot(datetime(2026, 3, 3, tzinfo=UTC))

    def mode_fixes() -> list[tuple[datetime, str]]:
        report = build_report(load_inputs(sources, day, board), sections={})
        return [(e.at, e.note) for v in report.verifications for e in v.events if e.kind == MODE]

    raw = mode_fixes()
    assert raw == [(switch, "stale_blocks: observe -> act")]
    later = datetime(2026, 4, 15, tzinfo=UTC)  # a month and a half later: rows pruned
    pruner = SqliteLedgerPruner(db)
    try:
        pruner.prune(later, MONTH, DEFAULT_MAX_ROWS, KEEP)
    finally:
        pruner.close()
    assert raw_rows(db) == 0
    with ReportStore(db) as store:
        assert store.actions(datetime(2026, 2, 1, tzinfo=UTC), later) == []
    assert mode_fixes() == raw  # the same fix, at the same time, from the daily sums


def test_doctor_shows_the_database_size_and_the_oldest_raw_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    db = tmp_path / "kelpie.db"
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{db}"\n')
    settings = load_settings(path)
    assert check_database(settings, NOW).status == "SKIP"
    ledger(db, [entry(days_ago(12), n=1), entry(days_ago(1), n=2)])
    check = check_database(settings, NOW)
    assert check.name == DATABASE_CHECK and check.status == "PASS"
    assert " kB at " in check.detail or " MB at " in check.detail
    assert "2 raw ledger row(s), oldest 2026-04-03 12:00 UTC (12d ago)" in check.detail
    assert "retention 30d" in check.detail
    late = check_database(settings, NOW + timedelta(days=25))  # 37 days: nothing prunes
    assert late.status == "WARN" and "kelpie serve" in late.fix


def test_a_day_summed_but_cut_short_reads_from_its_sums() -> None:
    from kelpie.report.verify import mode_events, summed_entries

    switch = datetime(2026, 3, 1, 10, tzinfo=UTC)
    day = ActionDay(switch.date(), "stale_blocks", "unblock", "observe", 3, 0, 0, 0,
                    switch - timedelta(hours=9), switch - timedelta(hours=1))  # fmt: skip
    act = ActionDay(switch.date(), "stale_blocks", "unblock", "act", 2, 2, 0, 0,
                    switch, switch + timedelta(hours=5))  # fmt: skip
    # a prune cut short left the day's last row; it alone would hide the switch
    left = [entry(switch + timedelta(hours=5), n=1)]
    later = [entry(switch + timedelta(days=1), n=2)]  # a day not summed: raw
    found = summed_entries([day, act], left + later)
    assert later[0] in found and left[0] not in found
    [event] = mode_events(found)
    assert (event.at, event.note) == (switch, "stale_blocks: observe -> act")


def test_the_in_memory_prune_runs_in_the_control_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    path = tmp_path / "kelpie.toml"
    path.write_text('[storage]\npath = ":memory:"\n[traces]\nenabled = false\n')
    app = build_app(load_settings(path), clock=lambda: NOW)
    try:
        assert isinstance(app.store, MemoryLedgerStore)
        app.store.append(entry(days_ago(40), n=1))
        app.store.append(entry(days_ago(1), n=2))
        assert app.ledger_retention is not None and not app.ledger_retention.background
        assert app.loop.housekeeping is not None
        app.loop.housekeeping()  # pruned before it returns, in this thread
        assert [e.task_id for e in app.store.entries] == ["T-2"]
    finally:
        app.close()
