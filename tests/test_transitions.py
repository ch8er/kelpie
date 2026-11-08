"""Observed task status transitions (#157): recording, restarts, storage, retention."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core import storage
from kelpie.core.config import EngineConfig
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot, Status, Task
from kelpie.core.storage import SqliteLedgerPruner, SqliteLedgerStore
from kelpie.core.transitions import (
    MemoryTransitionStore,
    StatusChange,
    TransitionRecorder,
    changed_at,
    history,
    observe,
)

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
TICK = timedelta(seconds=30)
DAY = timedelta(days=1)


def task(
    id_: str = "t-1",
    status: Status = Status.READY,
    since: datetime = T0 - timedelta(hours=2),
    **kw: object,
) -> Task:
    return Task(id=id_, title="a task", status=status, status_since=since, **kw)  # type: ignore[arg-type]


def snap(now: datetime, *tasks: Task) -> Snapshot:
    return Snapshot(now=now, tasks=tasks)


# --- observe: pure ---------------------------------------------------------------------


def test_first_sighting_is_a_row_with_the_boards_start() -> None:
    [c] = observe({}, [task()], T0)
    assert c.first and c.previous is None and c.after is None
    assert c.status is Status.READY and c.since == T0 - timedelta(hours=2) and c.at == T0
    assert changed_at(c) == T0 - timedelta(hours=2)


def test_no_change_no_row() -> None:
    [c] = observe({}, [task()], T0)
    assert observe({"t-1": c}, [task()], T0 + TICK) == []


def test_status_change_records_from_and_to() -> None:
    [c] = observe({}, [task()], T0)
    moved = task(status=Status.IN_PROGRESS, since=T0 + TICK / 2)
    [d] = observe({"t-1": c}, [moved], T0 + TICK, {"t-1": T0})
    assert (d.previous, d.status, d.after, d.at) == (
        Status.READY,
        Status.IN_PROGRESS,
        T0,
        T0 + TICK,
    )
    assert changed_at(d) == T0 + TICK / 2  # the board's own time, inside the tick


def test_platform_status_alone_is_a_change() -> None:
    [c] = observe({}, [task(platform_status="triage")], T0)
    [d] = observe({"t-1": c}, [task(platform_status="todo")], T0 + TICK)
    assert d.status is d.previous is Status.READY
    assert (d.previous_platform, d.platform_status) == ("triage", "todo")


def test_a_board_time_outside_the_window_is_not_trusted() -> None:
    [c] = observe({}, [task()], T0)
    # status_since older than the last sighting of the old status: the board reset it
    stale = task(status=Status.REVIEW, since=T0 - DAY)
    [d] = observe({"t-1": c}, [stale], T0 + TICK, {"t-1": T0})
    assert changed_at(d) == T0 + TICK


def test_a_task_listed_twice_counts_once_and_absence_is_not_a_status() -> None:
    assert len(observe({}, [task(), task()], T0)) == 1
    [c] = observe({}, [task()], T0)
    assert observe({"t-1": c}, [], T0 + TICK) == []


def test_multi_board_ids_are_kept_apart() -> None:
    a = task("alpha:7", board="alpha", source="hermes")
    b = task("beta:7", board="beta", source="hermes", status=Status.REVIEW)
    rows = observe({}, [a, b], T0)
    assert {(r.task_id, r.board, r.source) for r in rows} == {
        ("alpha:7", "alpha", "hermes"),
        ("beta:7", "beta", "hermes"),
    }
    last = {r.task_id: r for r in rows}
    moved = task("beta:7", board="beta", source="hermes", status=Status.DONE)
    [d] = observe(last, [a, moved], T0 + TICK)
    assert d.task_id == "beta:7" and d.previous is Status.REVIEW


def test_issue_tracker_tasks_without_a_board() -> None:
    t = task("acme/widgets#12", source="github_issues", platform_status="open")
    [c] = observe({}, [t], T0)
    assert c.board is None and c.source == "github_issues"
    closed = task("acme/widgets#12", source="github_issues", status=Status.DONE)
    [d] = observe({c.task_id: c}, [closed], T0 + TICK)
    assert d.platform_status is None and d.previous_platform == "open"


def test_history_groups_per_task_in_time_order() -> None:
    a, b = observe({}, [task("a"), task("b")], T0)
    later = StatusChange(T0 + TICK, "a", Status.DONE, T0 + TICK, previous=Status.READY)
    assert history([later, a, b]) == {"a": (a, later), "b": (b,)}


# --- the recorder and the engine ------------------------------------------------------


def test_recorder_in_the_engine_tick() -> None:
    store = MemoryTransitionStore()
    engine = Engine([], config=EngineConfig(), transitions=TransitionRecorder(store))
    engine.tick(lambda: snap(T0, task()))
    engine.tick(lambda: snap(T0 + TICK, task()))
    engine.tick(lambda: snap(T0 + 2 * TICK, task(status=Status.IN_PROGRESS, since=T0)))
    assert [(r.previous, r.status) for r in store.rows] == [
        (None, Status.READY),
        (Status.READY, Status.IN_PROGRESS),
    ]
    assert store.rows[1].after == T0 + TICK  # the last tick that saw it ready


class _Broken(MemoryTransitionStore):
    fail = True

    def record_transitions(self, changes: object) -> None:
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        super().record_transitions(changes)  # type: ignore[arg-type]


def test_a_failing_store_never_stops_the_tick_and_catches_up() -> None:
    store = _Broken()
    rec = TransitionRecorder(store)
    assert rec.record(snap(T0, task())) == [] and rec.failures == 1
    store.fail = False
    assert len(rec.record(snap(T0 + TICK, task()))) == 1  # compared again, stored now


def _db(tmp_path: Path) -> Path:
    return tmp_path / "kelpie.db"


def test_restart_invents_no_transition(tmp_path: Path) -> None:
    path = _db(tmp_path)
    first = SqliteLedgerStore(path)
    TransitionRecorder(first).record(snap(T0, task(), task("b", Status.REVIEW)))
    first.close()
    again = SqliteLedgerStore(path)  # a restart, hours later, nothing changed
    rec = TransitionRecorder(again)
    assert rec.record(snap(T0 + timedelta(hours=5), task(), task("b", Status.REVIEW))) == []
    # the next tick after it saw the old status bounds the next change
    [d] = rec.record(snap(T0 + timedelta(hours=6), task(status=Status.DONE, since=T0 - DAY)))
    assert d.after == T0 + timedelta(hours=5) and d.previous is Status.READY
    assert changed_at(d) == T0 + timedelta(hours=6)
    assert len(again.transitions(T0 - DAY, T0 + DAY)) == 3
    again.close()
    # a change during the gap is known only since the last record before it
    third = SqliteLedgerStore(path)
    [g] = TransitionRecorder(third).record(snap(T0 + DAY, task("b", Status.DONE, T0 + DAY / 2)))
    assert g.after == T0 and changed_at(g) == T0 + DAY / 2
    third.close()


def test_sqlite_round_trip_and_range(tmp_path: Path) -> None:
    store = SqliteLedgerStore(_db(tmp_path))
    rec = TransitionRecorder(store)
    rec.record(snap(T0, task(platform_status="todo", board="alpha", id_="alpha:1")))
    rec.record(snap(T0 + DAY, task("alpha:1", Status.REVIEW, T0 + DAY, board="alpha")))
    rec.record(snap(T0 + 2 * DAY, task("alpha:1", Status.DONE, T0 + 2 * DAY, board="alpha")))
    got = store.transitions(T0 + DAY + timedelta(hours=1), T0 + 3 * DAY)
    # the status when the range began, then what changed inside it
    assert [(c.status, c.board) for c in got] == [(Status.REVIEW, "alpha"), (Status.DONE, "alpha")]
    assert store.last_statuses()["alpha:1"].status is Status.DONE
    first = store.transitions(T0 - DAY, T0 + TICK)[0]
    assert first.platform_status == "todo" and first.first and first.after is None
    store.close()


# --- schema ----------------------------------------------------------------------------


def test_fresh_database_has_the_table(tmp_path: Path) -> None:
    SqliteLedgerStore(_db(tmp_path)).close()
    db = sqlite3.connect(_db(tmp_path))
    assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION == 12
    cols = {r[1] for r in db.execute("PRAGMA table_info(task_transitions)")}
    assert {"at", "task_id", "status", "previous", "since", "after_at", "board"} <= cols
    db.close()


def test_migrate_from_11_keeps_the_ledger(tmp_path: Path) -> None:
    path = _db(tmp_path)
    db = sqlite3.connect(path)
    db.executescript(
        "".join(storage.MIGRATIONS[v] for v in range(1, 12)) + "PRAGMA user_version = 11;"
    )
    db.execute(
        "INSERT INTO actions (at, fingerprint, subject, rule, kind, mode, outcome, reason,"
        " params) VALUES ('2026-01-01T00:00:00.000000+00:00', 'f', 's', 'r', 'alert',"
        " 'observe', 'observed', 'why', '{}')"
    )
    db.commit()
    db.close()
    store = SqliteLedgerStore(path)
    assert store.last_statuses() == {}
    assert len(store.since(datetime(2025, 12, 31, tzinfo=UTC))) == 1
    TransitionRecorder(store).record(snap(T0, task()))
    assert len(store.last_statuses()) == 1
    store.close()
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 12
    check.close()


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = _db(tmp_path)
    SqliteLedgerStore(path).close()
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {storage.SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        SqliteLedgerStore(path)


# --- retention -------------------------------------------------------------------------


def _rows(path: Path) -> list[tuple[str, str]]:
    db = sqlite3.connect(path)
    out = db.execute("SELECT task_id, status FROM task_transitions ORDER BY id").fetchall()
    db.close()
    return out


def test_retention_keeps_each_tasks_status_at_the_cut(tmp_path: Path) -> None:
    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = TransitionRecorder(store)
    statuses = (Status.READY, Status.IN_PROGRESS, Status.REVIEW)
    for i, st in enumerate(statuses):  # days 0, 1, 2
        rec.record(snap(T0 + i * DAY, task(status=st, since=T0 + i * DAY)))
    rec.record(snap(T0, task("old", Status.BACKLOG)))  # never changes again
    store.close()
    pruner = SqliteLedgerPruner(path, batch=1)
    now = T0 + 40 * DAY
    assert pruner.prune_transitions(now, 30 * DAY, 1_000_000, DAY) == 2
    # t-1 keeps its newest row before the cut; "old" keeps its only one
    assert _rows(path) == [("t-1", "review"), ("old", "backlog")]
    assert pruner.prune_transitions(now, 30 * DAY, 1_000_000, DAY) == 0
    pruner.close()


def test_row_cap_drops_the_oldest_outside_keep(tmp_path: Path) -> None:
    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = TransitionRecorder(store)
    for i in range(5):
        rec.record(snap(T0 + i * TICK, task(f"t{i}")))
    rec.record(snap(T0 + 3 * DAY, task("new")))
    store.close()
    pruner = SqliteLedgerPruner(path, batch=2)
    # within retention; the cap leaves 3 rows, never touching the one inside ``keep``
    assert pruner.prune_transitions(T0 + 3 * DAY, 30 * DAY, 3, DAY) == 3
    assert [r[0] for r in _rows(path)] == ["t3", "t4", "new"]
    assert pruner.prune_transitions(T0 + 3 * DAY, 30 * DAY, 0, DAY) == 2
    assert [r[0] for r in _rows(path)] == ["new"]
    pruner.close()


def test_memory_store_prune_keeps_the_newest() -> None:
    store = MemoryTransitionStore()
    rec = TransitionRecorder(store)
    rec.record(snap(T0, task()))
    rec.record(snap(T0 + DAY, task(status=Status.DONE)))
    assert store.prune(T0 + 2 * DAY) == 1 and [r.status for r in store.rows] == [Status.DONE]
    assert [r.status for r in store.transitions(T0 + 3 * DAY, T0 + 4 * DAY)] == [Status.DONE]


def test_the_report_store_reads_them(tmp_path: Path) -> None:
    from kelpie.report.store import ReportStore

    path = _db(tmp_path)
    store = SqliteLedgerStore(path)
    rec = TransitionRecorder(store)
    rec.record(snap(T0 - 2 * DAY, task("b1:1", board="b1"), task("b2:1", board="b2")))
    rec.record(snap(T0 + TICK, task("b1:1", Status.BLOCKED, T0, board="b1")))
    store.close()
    with ReportStore(path) as report:
        got = report.transitions(T0, T0 + DAY)
        assert report.transitions_since() == T0 - 2 * DAY
    # each board's task once before the day, then b1's change inside it
    assert [(c.task_id, c.status) for c in got] == [
        ("b1:1", Status.READY),
        ("b2:1", Status.READY),
        ("b1:1", Status.BLOCKED),
    ]
