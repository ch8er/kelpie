"""Observed task owners, blocks and conditions (kelpie.core.facts): recording, restarts,
storage, the schema 13 migration and retention."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core import storage
from kelpie.core.config import EngineConfig
from kelpie.core.engine import Engine
from kelpie.core.facts import (
    MAX_REASON,
    FactChange,
    FactKind,
    FactRecorder,
    MemoryFactStore,
    history,
    observe,
)
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    KelpieMark,
    Snapshot,
    Status,
    Task,
)
from kelpie.core.storage import SqliteLedgerPruner, SqliteLedgerStore

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
TICK = timedelta(seconds=30)
DAY = timedelta(days=1)
OWNER, BLOCK, COND = FactKind.OWNER, FactKind.BLOCK, FactKind.CONDITION


def task(
    id_: str = "t-1", owner: str | None = None, block: Block | None = None, **kw: object
) -> Task:
    return Task(
        id=id_,
        title="a task",
        status=Status.BLOCKED if block else Status.READY,
        status_since=T0 - timedelta(hours=2),
        owner_id=owner,
        block=block,
        **kw,  # type: ignore[arg-type]
    )


def snap(now: datetime, *tasks: Task, conditions: tuple[Condition, ...] = ()) -> Snapshot:
    return Snapshot(now=now, tasks=tasks, conditions=conditions)


def blk(reason: str = "waiting for CI", **kw: object) -> Block:
    return Block(reason, T0 - timedelta(hours=1), **kw)  # type: ignore[arg-type]


def cond(satisfied: bool | None = None, *waiters: str) -> Condition:
    return Condition("pr:1", ConditionKind.PR_MERGED, "t-1", satisfied=satisfied, task_ids=waiters)


def last(changes: list[FactChange]) -> dict[tuple[FactKind, str], FactChange]:
    return {c.key: c for c in changes}


# --- observe: pure ---------------------------------------------------------------------


def test_nothing_set_records_nothing() -> None:
    assert observe({}, snap(T0, task())) == []


def test_first_sighting_records_what_is_set() -> None:
    found = observe({}, snap(T0, task(owner="dev-1", block=blk()), conditions=(cond(),)))
    assert [(c.kind, c.subject, c.after) for c in found] == [
        (OWNER, "t-1", None),
        (BLOCK, "t-1", None),
        (COND, "pr:1", None),
    ]
    owner, block, condition = (c.decoded() for c in found)
    assert owner == {"owner": "dev-1"}
    assert (
        block["reason"] == "waiting for CI"
        and block["since"] == (T0 - timedelta(hours=1)).isoformat()
    )
    assert condition == {"kind": "pr_merged", "satisfied": None, "waiters": ["t-1"]}


def test_changes_and_clearing() -> None:
    first = observe({}, snap(T0, task(owner="dev-1", block=blk())))
    seen = last(first)
    assert observe(seen, snap(T0 + TICK, task(owner="dev-1", block=blk()))) == []
    moved = observe(seen, snap(T0 + 2 * TICK, task(owner="dev-2")), {k: T0 + TICK for k in seen})
    assert [(c.kind, c.value is None, c.after) for c in moved] == [
        (OWNER, False, T0 + TICK),
        (BLOCK, True, T0 + TICK),  # cleared: a row with no value
    ]
    assert moved[0].decoded() == {"owner": "dev-2"}


def test_a_condition_change_is_its_outcome_or_waiters_not_its_polling() -> None:
    seen = last(observe({}, snap(T0, task(), conditions=(cond(),))))
    polled = Condition("pr:1", ConditionKind.PR_MERGED, "t-1", checked_at=T0 + TICK)
    assert observe(seen, snap(T0 + TICK, task(), conditions=(polled,))) == []
    [met] = observe(seen, snap(T0 + TICK, task(), conditions=(cond(True),)))
    assert met.decoded()["satisfied"] is True
    [joined] = observe(seen, snap(T0 + TICK, task(), conditions=(cond(None, "t-2"),)))
    assert joined.decoded()["waiters"] == ["t-1", "t-2"]


def test_absence_is_not_a_value() -> None:
    seen = last(observe({}, snap(T0, task(owner="dev-1"), conditions=(cond(),))))
    assert observe(seen, snap(T0 + TICK)) == []  # board not read, condition gone


def test_block_reason_is_cut_and_marks_kept() -> None:
    long = blk(
        "x" * (MAX_REASON + 50), category="guard", condition_id="pr:1", kelpie=KelpieMark.STOP
    )
    [block] = [c for c in observe({}, snap(T0, task(block=long))) if c.kind is BLOCK]
    value = block.decoded()
    assert len(value["reason"]) == MAX_REASON
    assert (value["category"], value["condition_id"], value["kelpie"]) == ("guard", "pr:1", "stop")


def test_board_and_source_and_duplicates() -> None:
    twice = (task("a:1", owner="dev-1", board="a", source="hermes"), task("a:1", owner="x"))
    [c] = observe({}, snap(T0, *twice))
    assert (c.board, c.source, c.decoded()) == ("a", "hermes", {"owner": "dev-1"})


def test_history_groups_per_fact_in_time_order() -> None:
    rows = observe({}, snap(T0, task(owner="dev-1")))
    rows += observe(last(rows), snap(T0 + DAY, task(owner="dev-2")))
    assert [c.decoded()["owner"] for c in history(reversed(rows))[(OWNER, "t-1")]] == [
        "dev-1",
        "dev-2",
    ]


# --- the recorder and the engine ------------------------------------------------------


def test_recorder_in_the_engine_tick() -> None:
    store = MemoryFactStore()
    engine = Engine([], config=EngineConfig(), facts=FactRecorder(store))
    engine.tick(lambda: snap(T0, task(owner="dev-1")))
    engine.tick(lambda: snap(T0 + TICK, task(owner="dev-1")))
    engine.tick(lambda: snap(T0 + 2 * TICK, task(owner="dev-1", block=blk())))
    assert [(r.kind, r.at) for r in store.rows] == [(OWNER, T0), (BLOCK, T0 + 2 * TICK)]


class _Broken(MemoryFactStore):
    fail = True

    def record_facts(self, changes: object) -> None:
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        super().record_facts(changes)  # type: ignore[arg-type]


def test_a_failing_store_never_stops_the_tick_and_catches_up() -> None:
    store = _Broken()
    rec = FactRecorder(store)
    assert rec.record(snap(T0, task(owner="dev-1"))) == [] and rec.failures == 1
    store.fail = False
    assert len(rec.record(snap(T0 + TICK, task(owner="dev-1")))) == 1


def _db(tmp_path: Path) -> Path:
    return tmp_path / "kelpie.db"


def test_restart_invents_no_change(tmp_path: Path) -> None:
    path = _db(tmp_path)
    first = SqliteLedgerStore(path)
    FactRecorder(first).record(snap(T0, task(owner="dev-1", block=blk()), conditions=(cond(),)))
    first.close()
    again = SqliteLedgerStore(path)
    rec = FactRecorder(again)
    same = snap(T0 + DAY, task(owner="dev-1", block=blk()), conditions=(cond(),))
    assert rec.record(same) == []
    [cleared] = rec.record(snap(T0 + DAY + TICK, task(owner="dev-1"), conditions=(cond(),)))
    assert cleared.kind is BLOCK and cleared.value is None and cleared.after == T0 + DAY
    assert len(again.facts(T0 - DAY, T0 + 2 * DAY)) == 4
    again.close()


def test_sqlite_round_trip_and_range(tmp_path: Path) -> None:
    store = SqliteLedgerStore(_db(tmp_path))
    rec = FactRecorder(store)
    rec.record(snap(T0, task(owner="dev-1", board="alpha")))
    rec.record(snap(T0 + DAY, task(owner="dev-2", board="alpha")))
    rec.record(snap(T0 + 2 * DAY, task(owner="dev-3", board="alpha")))
    got = store.facts(T0 + DAY + timedelta(hours=1), T0 + 3 * DAY)
    # the value when the range began, then what changed inside it
    assert [(c.decoded()["owner"], c.board) for c in got] == [
        ("dev-2", "alpha"),
        ("dev-3", "alpha"),
    ]
    assert store.last_facts()[(OWNER, "t-1")].decoded() == {"owner": "dev-3"}
    store.close()


# --- schema ----------------------------------------------------------------------------


def test_fresh_database_has_the_table(tmp_path: Path) -> None:
    SqliteLedgerStore(_db(tmp_path)).close()
    db = sqlite3.connect(_db(tmp_path))
    assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION == 13
    cols = {r[1] for r in db.execute("PRAGMA table_info(fact_changes)")}
    assert cols == {
        "id",
        "at",
        "kind",
        "subject",
        "value",
        "after_at",
        "board",
        "source",
    }
    db.close()


def test_migrate_from_12_keeps_the_ledger_and_the_status_history(tmp_path: Path) -> None:
    path = _db(tmp_path)
    db = sqlite3.connect(path)
    db.executescript(
        "".join(storage.MIGRATIONS[v] for v in range(1, 13)) + "PRAGMA user_version = 12;"
    )
    db.execute(
        "INSERT INTO actions (at, fingerprint, subject, rule, kind, mode, outcome, reason,"
        " params) VALUES ('2026-01-01T00:00:00.000000+00:00', 'f', 's', 'r', 'alert',"
        " 'observe', 'observed', 'why', '{}')"
    )
    db.execute(
        "INSERT INTO task_transitions (at, task_id, status, since) VALUES"
        " ('2026-01-01T00:00:00.000000+00:00', 't-1', 'ready', '2026-01-01T00:00:00+00:00')"
    )
    db.commit()
    db.close()
    store = SqliteLedgerStore(path)
    assert store.last_facts() == {}
    assert len(store.since(datetime(2025, 12, 31, tzinfo=UTC))) == 1
    assert list(store.last_statuses()) == ["t-1"]
    FactRecorder(store).record(snap(T0, task(owner="dev-1")))
    assert len(store.last_facts()) == 1
    store.close()
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 13
    check.close()


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = _db(tmp_path)
    SqliteLedgerStore(path).close()
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {storage.SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        SqliteLedgerStore(path)
    with pytest.raises(RuntimeError, match="newer"):
        storage.open_readonly(path, 1000)


def test_a_reader_asks_for_the_migration(tmp_path: Path) -> None:
    path = _db(tmp_path)
    db = sqlite3.connect(path)
    db.executescript(
        "".join(storage.MIGRATIONS[v] for v in range(1, 13)) + "PRAGMA user_version = 12;"
    )
    db.close()
    with pytest.raises(storage.SchemaOutdated, match="run kelpie serve"):
        storage.open_readonly(path, 1000)


# --- retention -------------------------------------------------------------------------


def _rows(path: Path) -> list[tuple[str, str, str | None]]:
    db = sqlite3.connect(path)
    out = db.execute("SELECT kind, subject, value FROM fact_changes ORDER BY id").fetchall()
    db.close()
    return out


def test_retention_keeps_each_facts_value_at_the_cut(tmp_path: Path) -> None:
    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = FactRecorder(store)
    for i, who in enumerate(("dev-1", "dev-2", "dev-3")):  # days 0, 1, 2
        rec.record(snap(T0 + i * DAY, task(owner=who), task("old", owner="ops-1")))
    store.close()
    pruner = SqliteLedgerPruner(path, batch=1)
    now = T0 + 40 * DAY
    assert pruner.prune_facts(now, 30 * DAY, 1_000_000, DAY) == 2
    assert [(s, json.loads(v or "null")["owner"]) for _, s, v in _rows(path)] == [
        ("old", "ops-1"),
        ("t-1", "dev-3"),
    ]
    assert pruner.prune_facts(now, 30 * DAY, 1_000_000, DAY) == 0
    pruner.close()


def test_row_cap_drops_the_oldest_outside_keep(tmp_path: Path) -> None:
    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = FactRecorder(store)
    for i in range(5):
        rec.record(snap(T0 + i * TICK, task(f"t{i}", owner="dev-1")))
    rec.record(snap(T0 + 3 * DAY, task("new", owner="dev-1")))
    store.close()
    pruner = SqliteLedgerPruner(path, batch=2)
    assert pruner.prune_facts(T0 + 3 * DAY, 30 * DAY, 3, DAY) == 3
    assert [r[1] for r in _rows(path)] == ["t3", "t4", "new"]
    assert pruner.prune_facts(T0 + 3 * DAY, 30 * DAY, 0, DAY) == 2
    assert [r[1] for r in _rows(path)] == ["new"]  # never inside ``keep``
    pruner.close()


def test_row_cap_drops_history_before_a_facts_last_row_first(tmp_path: Path) -> None:
    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = FactRecorder(store)
    rec.record(snap(T0, task(owner="dev-1"), task("b", owner="ops-1")))
    rec.record(snap(T0 + TICK, task(owner="dev-2"), task("b", owner="ops-1")))
    store.close()
    pruner = SqliteLedgerPruner(path)
    assert pruner.prune_facts(T0 + 3 * DAY, 30 * DAY, 2, DAY) == 1
    assert sorted(r[1] for r in _rows(path)) == ["b", "t-1"]
    pruner.close()


def test_memory_store_prune_keeps_the_newest() -> None:
    store = MemoryFactStore()
    rec = FactRecorder(store)
    for i, who in enumerate(("dev-1", "dev-2", "dev-3")):
        rec.record(snap(T0 + i * DAY, task(owner=who)))
    assert store.prune(T0 + 40 * DAY) == 2
    assert [r.decoded()["owner"] for r in store.rows] == ["dev-3"]
    rec.record(snap(T0 + 41 * DAY, task("b", owner="x"), task("c", owner="y")))
    assert store.prune(T0, max_rows=1) == 2
    assert len(store.facts(T0, T0 + 50 * DAY)) == 1


# --- wired into kelpie serve's retention ------------------------------------------------


def test_serve_retention_prunes_the_facts(tmp_path: Path) -> None:
    from dataclasses import replace

    from kelpie.app import Settings, build_ledger_retention
    from kelpie.core.ledger import MemoryLedgerStore
    from kelpie.core.states import MemoryStateStore

    now = T0 + 40 * DAY
    memory = MemoryFactStore()
    rec = FactRecorder(memory)
    for i, who in enumerate(("dev-1", "dev-2")):
        rec.record(snap(T0 + i * DAY, task(owner=who)))
    in_memory = build_ledger_retention(
        replace(Settings(), db_path=None),
        MemoryLedgerStore(),
        MemoryStateStore(),
        lambda: now,
        facts=memory,
    )
    in_memory.run_once()
    assert [r.decoded()["owner"] for r in memory.rows] == ["dev-2"]

    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = FactRecorder(store)
    for i, who in enumerate(("dev-1", "dev-2")):
        rec.record(snap(T0 + i * DAY, task(owner=who)))
    on_disk = build_ledger_retention(
        replace(Settings(), db_path=path), store, store, lambda: now, store, store
    )
    on_disk.run_once()
    on_disk.stop()
    assert len(_rows(path)) == 1
    store.close()


def test_a_first_value_is_placed_after_the_last_tick_that_saw_it_unset() -> None:
    rec = FactRecorder(MemoryFactStore())
    rec.record(snap(T0, task()))
    rec.record(snap(T0 + TICK, task()))
    [owner] = rec.record(snap(T0 + 2 * TICK, task(owner="dev-1")))
    assert owner.after == T0 + TICK


def test_the_reason_text_is_compared_by_its_hash_and_retention_drops_it(tmp_path: Path) -> None:
    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = FactRecorder(store)
    rec.record(snap(T0, task(block=blk("@arch-1 v3 or v4?"))))
    [changed] = rec.record(snap(T0 + TICK, task(block=blk("@arch-1 v4 then?"))))
    assert changed.decoded()["reason"] == "@arch-1 v4 then?"
    store.close()
    pruner = SqliteLedgerPruner(path, batch=1)
    assert pruner.prune_facts(T0 + 40 * DAY, 30 * DAY, 1_000_000, DAY) == 1
    pruner.close()
    [(_, _, kept)] = _rows(path)
    assert "reason" not in json.loads(kept or "") and "reason_sha256" in json.loads(kept or "")
    # a restart compares without the text: the same block is no change
    again = SqliteLedgerStore(path)
    assert (
        FactRecorder(again).record(snap(T0 + 41 * DAY, task(block=blk("@arch-1 v4 then?")))) == []
    )
    again.close()


def test_memory_retention_drops_the_reason_text() -> None:
    store = MemoryFactStore()
    FactRecorder(store).record(snap(T0, task(block=blk("secret-ish words"))))
    store.prune(T0 + 40 * DAY)
    assert "reason" not in store.rows[0].decoded()
