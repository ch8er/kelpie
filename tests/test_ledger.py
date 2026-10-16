import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import (
    ActionLedger,
    Dispatched,
    LedgerEntry,
    MemoryLedgerStore,
    Outcome,
    Suppression,
    fingerprint,
    subject,
)
from kelpie.core.model import Snapshot
from kelpie.core.storage import SCHEMA_VERSION, SqliteLedgerStore
from tests.fakes import Emit, FakeAdapter, FakeAlerter
from tests.test_engine import RaisingAlerter

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
ACT = EngineConfig(dry_run=False, default_mode=Mode.ACT)


def action(task_id: str | None = "T-1", reason: str = "r", **params: object) -> Action:
    return Action(ActionKind.REQUEUE, "emit", reason, task_id=task_id, params=dict(params))


def done(a: Action, mode: Mode = Mode.ACT, outcome: Outcome = Outcome.EXECUTED) -> Dispatched:
    return Dispatched(a, mode, outcome)


def tick(engine: Engine, at: datetime) -> list[Outcome]:
    return [d.outcome for d in engine.tick(lambda: Snapshot(now=at))]


# --- fingerprint and subject ------------------------------------------------------------


def test_fingerprint_ignores_reason_but_not_params() -> None:
    assert fingerprint(action(reason="a")) == fingerprint(action(reason="b"))
    assert fingerprint(action(x=1)) != fingerprint(action(x=2))
    assert fingerprint(action("T-1")) != fingerprint(action("T-2"))


def test_key_makes_volatile_params_irrelevant() -> None:
    def disk(pct: float) -> Action:
        return Action(ActionKind.ALERT, "resources", "disk", key="disk:host-a", params={"pct": pct})

    assert fingerprint(disk(93.1)) == fingerprint(disk(97.4))


def test_subject_falls_back_to_session_then_rule() -> None:
    assert subject(action("T-1")) == "task:T-1"
    assert subject(action(None, session_id="S-1")) == "session:S-1"
    assert subject(Action(ActionKind.ALERT, "resources", "r", executor="ci")) == "rule:ci:resources"


# --- cooldown -------------------------------------------------------------------------


def test_cooldown_suppresses_then_expires_exclusively() -> None:
    ledger = ActionLedger(MemoryLedgerStore(), cooldown=timedelta(minutes=30))
    ledger.record(done(action()), T0)
    assert ledger.check(action(reason="again"), Mode.ACT, T0 + timedelta(minutes=29)) == (
        Suppression.COOLDOWN,
        False,
    )
    assert ledger.check(action(), Mode.ACT, T0 + timedelta(minutes=30)) is None


def test_cooldown_is_per_mode_so_turning_a_rule_on_acts_at_once() -> None:
    adapter, store = FakeAdapter(), MemoryLedgerStore()
    observing = Engine(
        [Emit([action()])],
        adapters=[adapter],
        config=EngineConfig(dry_run=False),
        ledger_store=store,
    )
    assert tick(observing, T0) == [Outcome.OBSERVED]
    acting = Engine([Emit([action()])], adapters=[adapter], config=ACT, ledger_store=store)
    assert tick(acting, T0 + timedelta(minutes=5)) == [Outcome.EXECUTED]
    assert adapter.calls == [("requeue", "T-1")]


def test_failed_action_retries_after_shorter_delay() -> None:
    ledger = ActionLedger(
        MemoryLedgerStore(), cooldown=timedelta(minutes=30), retry_after=timedelta(minutes=5)
    )
    ledger.record(done(action(), outcome=Outcome.FAILED), T0)
    assert ledger.check(action(), Mode.ACT, T0 + timedelta(minutes=4)) is not None
    assert ledger.check(action(), Mode.ACT, T0 + timedelta(minutes=5)) is None


def test_engine_dedups_within_and_across_ticks_and_counts_repeats() -> None:
    adapter, store = FakeAdapter(), MemoryLedgerStore()
    engine = Engine(
        [Emit([action(), action(reason="dup")])],
        adapters=[adapter],
        config=ACT,
        ledger_store=store,
    )
    assert tick(engine, T0) == [Outcome.EXECUTED, Outcome.SUPPRESSED]
    assert tick(engine, T0 + timedelta(minutes=1)) == [Outcome.SUPPRESSED] * 2
    assert adapter.calls == [("requeue", "T-1")]
    [entry] = store.entries
    assert (entry.outcome, entry.suppressed) == (Outcome.EXECUTED, 3)


# --- rate limit -----------------------------------------------------------------------


def test_rate_limit_counts_effects_including_failures() -> None:
    ledger = ActionLedger(MemoryLedgerStore(), cooldown=timedelta(0), max_per_subject_per_hour=2)
    ledger.record(done(action(n=1), Mode.OBSERVE, Outcome.OBSERVED), T0)
    ledger.record(done(action(n=2), outcome=Outcome.FAILED), T0)
    assert ledger.check(action(n=3), Mode.ACT, T0) is None
    ledger.record(done(action(n=3), Mode.SUGGEST, Outcome.SUGGESTED), T0)
    assert ledger.check(action(n=4), Mode.ACT, T0) == (Suppression.RATE_LIMIT, True)
    assert ledger.check(action(n=5), Mode.ACT, T0) == (Suppression.RATE_LIMIT, False)
    assert ledger.check(action("T-2"), Mode.ACT, T0) is None
    assert ledger.check(action(n=4), Mode.ACT, T0 + timedelta(hours=1)) is None  # exclusive


def test_rate_limit_applies_to_actions_without_task() -> None:
    alerter = FakeAlerter()
    config = EngineConfig(
        dry_run=False, default_mode=Mode.ACT, cooldown=timedelta(0), max_actions_per_task_per_hour=3
    )
    alerts = [
        Action(ActionKind.ALERT, "resources", "disk", params={"pct": 90 + i}) for i in range(20)
    ]
    engine = Engine([Emit(alerts)], adapters=[FakeAdapter()], alerter=alerter, config=config)
    assert tick(engine, T0).count(Outcome.ALERTED) == 3
    # 3 alerts plus one notice about the rate limit.
    assert len(alerter.sent) == 4
    assert alerter.sent[-1][0] == "[kelpie] alert"


def test_rate_limit_stops_a_looping_rule_against_a_failing_adapter() -> None:
    adapter, alerter = FakeAdapter(fail_on="requeue"), FakeAlerter()
    config = EngineConfig(
        dry_run=False, default_mode=Mode.ACT, cooldown=timedelta(0), max_actions_per_task_per_hour=3
    )
    engine = Engine(
        [Emit([action(n=i) for i in range(20)])], adapters=[adapter], alerter=alerter, config=config
    )
    outcomes = tick(engine, T0)
    assert outcomes.count(Outcome.FAILED) == 3
    assert outcomes.count(Outcome.SUPPRESSED) == 17
    titles = [t for t, _ in alerter.sent]
    assert titles.count("[emit] failed requeue T-1 (not executed, needs a human)") == 3
    assert titles.count("[kelpie] alert T-1") == 1


# --- store failures ---------------------------------------------------------------------


class BrokenStore(MemoryLedgerStore):
    def __init__(self, fail_reads: bool = False) -> None:
        super().__init__()
        self.fail_reads = fail_reads
        self.fail_writes = True

    def append(self, entry: LedgerEntry) -> None:
        if self.fail_writes:
            raise sqlite3.OperationalError("disk I/O error")
        super().append(entry)

    def by_fingerprint(self, fingerprint: str, after: datetime) -> Sequence[LedgerEntry]:
        if self.fail_reads:
            raise sqlite3.OperationalError("database is locked")
        return super().by_fingerprint(fingerprint, after)


def test_write_failure_does_not_make_kelpie_repeat_itself() -> None:
    adapter, store = FakeAdapter(), BrokenStore()
    engine = Engine([Emit([action()])], adapters=[adapter], config=ACT, ledger_store=store)
    assert tick(engine, T0) == [Outcome.EXECUTED]
    assert tick(engine, T0 + timedelta(minutes=1)) == [Outcome.SUPPRESSED]
    assert adapter.calls == [("requeue", "T-1")]
    store.fail_writes = False
    tick(engine, T0 + timedelta(minutes=2))  # next record flushes the backlog
    assert [(e.outcome, e.suppressed) for e in store.entries] == [(Outcome.EXECUTED, 2)]


def test_read_failure_fails_closed_and_tells_a_human_once() -> None:
    adapter, alerter = FakeAdapter(), FakeAlerter()
    rules = [Emit([action("T-1"), action("T-2")])]
    engine = Engine(
        rules, adapters=[adapter], alerter=alerter, config=ACT, ledger_store=BrokenStore(True)
    )
    results = engine.tick(lambda: Snapshot(now=T0))
    assert [(d.outcome, d.suppression) for d in results] == [
        (Outcome.SUPPRESSED, Suppression.LEDGER_DOWN)
    ] * 2
    assert adapter.calls == []
    assert [t for t, _ in alerter.sent] == ["[kelpie] alert"]


def test_ledger_down_notice_once_per_outage() -> None:
    alerter, store = FakeAlerter(), BrokenStore(fail_reads=True)
    engine = Engine(
        [Emit([action()])],
        adapters=[FakeAdapter()],
        alerter=alerter,
        config=ACT,
        ledger_store=store,
    )

    def notices() -> int:
        return sum(title.startswith("[kelpie]") for title, _ in alerter.sent)

    for minute in range(5):
        tick(engine, T0 + timedelta(minutes=minute))
    assert notices() == 1
    assert not engine.ledger_available
    store.fail_reads = store.fail_writes = False
    assert tick(engine, T0 + timedelta(minutes=10)) == [Outcome.EXECUTED]
    assert engine.ledger_available
    store.fail_reads = True
    tick(engine, T0 + timedelta(minutes=11))
    assert notices() == 2  # one notice per outage


def test_read_failure_still_lets_observe_through() -> None:
    engine = Engine(
        [Emit([action()])],
        adapters=[FakeAdapter()],
        config=EngineConfig(dry_run=False),
        ledger_store=BrokenStore(True),
    )
    assert tick(engine, T0) == [Outcome.OBSERVED]


# --- SQLite -----------------------------------------------------------------------------


def entry(at: datetime, **params: object) -> LedgerEntry:
    a = Action(ActionKind.STOP, "loop_breaker", "loop", executor="hermes", params=dict(params))
    return LedgerEntry.of(done(a), at)


def test_sqlite_roundtrip_reopen_and_ordering(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.db"
    store = SqliteLedgerStore(path)
    plus3 = timezone(timedelta(hours=3))
    store.append(entry(T0 + timedelta(minutes=5), session_id="S-1"))
    store.append(entry((T0 - timedelta(minutes=5)).astimezone(plus3)))
    store.append(entry(T0 + timedelta(microseconds=1)))
    store.close()

    reopened = SqliteLedgerStore(path)
    got = reopened.since(T0 - timedelta(hours=1))
    assert [e.at for e in got] == [
        T0 - timedelta(minutes=5),
        T0 + timedelta(microseconds=1),
        T0 + timedelta(minutes=5),
    ]
    assert got[2] == entry(T0 + timedelta(minutes=5), session_id="S-1")
    plain = got[0].fingerprint  # the two entries without params share a fingerprint
    assert len(reopened.by_fingerprint(plain, T0)) == 1
    assert len(reopened.by_fingerprint(plain, T0 + timedelta(microseconds=1))) == 0  # exclusive
    assert reopened.bump_suppressed(got[2].fingerprint, Mode.ACT, T0)
    assert reopened.since(T0 + timedelta(minutes=1))[0].suppressed == 1
    assert not reopened.bump_suppressed(got[2].fingerprint, Mode.OBSERVE, T0)  # other mode
    assert not reopened.bump_suppressed(got[2].fingerprint, Mode.ACT, T0 + timedelta(hours=1))
    assert not reopened.bump_suppressed("missing", Mode.ACT, T0)
    assert len(reopened.by_subject("session:S-1", T0)) == 1
    reopened.close()


def test_sqlite_rejects_naive_and_newer_schema(tmp_path: Path) -> None:
    store = SqliteLedgerStore(tmp_path / "a.db")
    with pytest.raises(ValueError, match="timezone"):
        store.since(datetime(2026, 3, 2))
    store.close()
    path = tmp_path / "b.db"
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        SqliteLedgerStore(path)


def test_sqlite_is_wal_and_thread_safe(tmp_path: Path) -> None:
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    threads = [
        threading.Thread(target=store.append, args=(entry(T0 + timedelta(seconds=i)),))
        for i in range(20)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.since(T0 - timedelta(seconds=1))) == 20
    mode = sqlite3.connect(tmp_path / "kelpie.db").execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    store.close()


def test_engine_persists_ledger_in_sqlite(tmp_path: Path) -> None:
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    engine = Engine([Emit([action()])], adapters=[FakeAdapter()], ledger_store=store)
    tick(engine, T0)  # defaults: dry-run on, observe
    assert [(e.mode, e.outcome) for e in store.since(T0 - timedelta(seconds=1))] == [
        (Mode.OBSERVE, Outcome.DRY_RUN)
    ]
    store.close()


def test_memory_store_bump_without_entry() -> None:
    assert not MemoryLedgerStore().bump_suppressed("missing", Mode.ACT, T0)


def test_dry_run_never_rate_limits_or_alerts() -> None:
    store, alerter = MemoryLedgerStore(), FakeAlerter()
    ledger = ActionLedger(store, cooldown=timedelta(0), max_per_subject_per_hour=1)
    ledger.record(done(action(n=0)), T0)  # subject already saturated
    config = EngineConfig(default_mode=Mode.ACT, dry_run=True, max_actions_per_task_per_hour=1)
    engine = Engine(
        [Emit([action(n=1)])],
        adapters=[FakeAdapter()],
        alerter=alerter,
        config=config,
        ledger_store=store,
    )
    assert tick(engine, T0) == [Outcome.DRY_RUN]
    assert alerter.sent == []


def test_notice_alerter_failure_does_not_escape_tick() -> None:
    config = EngineConfig(
        dry_run=False, default_mode=Mode.ACT, cooldown=timedelta(0), max_actions_per_task_per_hour=1
    )
    adapter = FakeAdapter()
    engine = Engine(
        [Emit([action(n=1), action(n=2), action("T-2")])],
        adapters=[adapter],
        alerter=RaisingAlerter(),
        config=config,
    )
    assert tick(engine, T0) == [Outcome.EXECUTED, Outcome.SUPPRESSED, Outcome.EXECUTED]
    assert adapter.calls == [("requeue", "T-1"), ("requeue", "T-2")]


def test_mixed_param_keys_are_fingerprinted_and_stored(tmp_path: Path) -> None:
    weird = action(**{"a": {1: "x", "b": "y"}, "s": {3, 1}})
    assert fingerprint(weird) == fingerprint(weird)
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    ledger = ActionLedger(store)
    ledger.record(done(weird), T0)
    [saved] = store.since(T0 - timedelta(seconds=1))
    assert saved.params == {"a": {"1": "x", "b": "y"}, "s": [1, 3]}
    store.close()


def test_failed_migration_leaves_database_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.core.storage as storage

    path = tmp_path / "kelpie.db"
    broken = storage.MIGRATIONS[1] + "\nCREATE TABLE oops (;"
    monkeypatch.setitem(storage.MIGRATIONS, 1, broken)
    with pytest.raises(sqlite3.Error):
        SqliteLedgerStore(path)
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert db.execute("SELECT name FROM sqlite_master WHERE name = 'actions'").fetchone() is None
    db.close()
    monkeypatch.undo()
    SqliteLedgerStore(path).close()  # a later start migrates cleanly


def test_unsaved_entries_expire_from_the_rate_window() -> None:
    store = BrokenStore()
    ledger = ActionLedger(store, cooldown=timedelta(0), max_per_subject_per_hour=1)
    with pytest.raises(sqlite3.OperationalError):
        ledger.record(done(action(n=1)), T0)
    assert ledger.check(action(n=2), Mode.ACT, T0) == (Suppression.RATE_LIMIT, True)
    assert ledger.check(action(n=2), Mode.ACT, T0 + timedelta(hours=2)) is None


# --- once per key -----------------------------------------------------------------------


def test_once_per_key_holds_across_tasks_and_kinds() -> None:
    """A request that moves to whichever task is current (or alerts without one) is still
    made once: the key alone is the fingerprint."""
    adapter, store = FakeAdapter(), MemoryLedgerStore()

    def ask(task: str | None) -> Action:
        kind = ActionKind.COMMENT if task else ActionKind.ALERT
        return Action(kind, "emit", "add a filter", task_id=task, key="k", once=True,
                      once_per_key=True)  # fmt: skip

    for hour, task in enumerate(["T-1", "T-2", None, "T-3"]):
        engine = Engine([Emit([ask(task)])], adapters=[adapter], config=ACT, ledger_store=store)
        tick(engine, T0 + timedelta(hours=hour))
    assert adapter.calls == [("comment", "T-1")]
    assert fingerprint(ask("T-1")) == fingerprint(ask(None))


def test_plain_once_fingerprints_are_unchanged() -> None:
    a = Action(ActionKind.COMMENT, "emit", "r", task_id="T-1", key="k", once=True)
    assert fingerprint(a) != fingerprint(replace(a, task_id="T-2"))
    assert fingerprint(a) == fingerprint(replace(a, once_per_key=False))
    assert fingerprint(replace(a, key=None, once_per_key=True)) == fingerprint(replace(a, key=None))
