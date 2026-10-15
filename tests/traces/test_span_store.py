"""Span stores (memory and SQLite): roundtrip, idempotent append, window, totals, retention."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core import storage
from kelpie.core.model import Span, SpanKind, SpanTotals
from kelpie.core.spans import MemorySpanStore, SpanStore, totals
from kelpie.core.storage import SqliteLedgerStore, SqliteSpanStore
from kelpie.traces.store import SpanRetention, SpanWindow, merge_spans

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)


def span(
    n: int,
    start: datetime,
    *,
    kind: SpanKind = SpanKind.LLM,
    end: datetime | None = None,
    session: str | None = "s-1",
    task: str | None = "T-1",
    tokens: int = 0,
    outcome: str = "ok",
) -> Span:
    return Span(
        trace_id="t" * 32,
        span_id=f"{n:016x}",
        name=f"span {n}",
        kind=kind,
        start=start,
        end=end,
        task_id=task,
        session_id=session,
        model="m" if kind is SpanKind.LLM else None,
        tool="shell" if kind is SpanKind.TOOL else None,
        args_hash="abcd" if kind is SpanKind.TOOL else None,
        input_tokens=tokens,
        output_tokens=tokens // 10,
        cached_tokens=tokens // 2,
        outcome=outcome,
        attributes={"gen_ai.operation.name": "chat", "n": n, "list": [1, "a"]},
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SpanStore]:
    if request.param == "memory":
        yield MemorySpanStore()
        return
    s = SqliteSpanStore(tmp_path / "kelpie.db", batch=3)
    yield s
    s.close()


def test_roundtrip_and_idempotent_append(store: SpanStore) -> None:
    spans = [
        span(1, NOW - timedelta(minutes=3), end=NOW - timedelta(minutes=2), tokens=100),
        span(2, NOW - timedelta(minutes=1), kind=SpanKind.TOOL, session=None, task=None),
    ]
    assert store.append_spans(spans, NOW) == 2
    assert store.append_spans(spans, NOW) == 0  # exporter retry
    assert store.count() == 2
    assert list(store.spans_since(NOW - timedelta(hours=1))) == spans


def test_window_uses_end_or_start(store: SpanStore) -> None:
    long_running = span(1, NOW - timedelta(hours=5), end=NOW - timedelta(minutes=10))
    open_old = span(2, NOW - timedelta(hours=5))
    recent = span(3, NOW - timedelta(minutes=30))
    store.append_spans([long_running, open_old, recent], NOW)
    found = store.spans_since(NOW - timedelta(hours=2))
    assert [s.span_id for s in found] == [long_running.span_id, recent.span_id]


def test_totals_per_session(store: SpanStore) -> None:
    t0 = NOW - timedelta(minutes=30)
    store.append_spans(
        [
            span(1, t0, end=t0 + timedelta(seconds=5), tokens=1000),
            span(2, t0 + timedelta(minutes=1), tokens=500),
            span(3, t0 + timedelta(minutes=2), kind=SpanKind.TOOL, outcome="error"),
            span(4, t0 + timedelta(minutes=3), kind=SpanKind.TOOL),
            # an agent span that repeats its children's usage: not counted twice
            span(5, t0, kind=SpanKind.SESSION, tokens=1500, end=t0 + timedelta(minutes=4)),
            span(6, t0, session="s-2", task=None, tokens=7),
            span(7, t0, session=None, tokens=99),
            span(8, NOW - timedelta(days=2), tokens=10_000),  # outside the window
        ],
        NOW,
    )
    got = list(store.totals_since(NOW - timedelta(hours=1)))
    assert got == [
        SpanTotals(
            session_id="s-1",
            task_id="T-1",
            spans=5,
            llm_calls=2,
            tool_calls=2,
            tool_errors=1,
            input_tokens=1500,
            output_tokens=150,
            cached_tokens=750,
            first_start=t0,
            last_end=t0 + timedelta(minutes=4),
        ),
        SpanTotals(
            session_id="s-2",
            spans=1,
            llm_calls=1,
            input_tokens=7,
            cached_tokens=3,
            first_start=t0,
            last_end=t0,
        ),
    ]
    assert tuple(got) == totals(store.spans_since(NOW - timedelta(hours=1)))


def test_prune_by_age_then_row_cap(store: SpanStore) -> None:
    spans = [span(i, NOW - timedelta(days=10 - i)) for i in range(10)]
    store.append_spans(spans, NOW)
    # spans 10, 9 and 8 days old go; the one exactly 7 days old stays
    assert store.prune(NOW - timedelta(days=7), max_rows=100) == 3
    assert store.count() == 7
    assert store.prune(NOW - timedelta(days=7), max_rows=4) == 3
    assert [s.span_id for s in store.spans_since(NOW - timedelta(days=30))] == [
        f"{i:016x}" for i in range(6, 10)
    ]
    assert store.prune(NOW - timedelta(days=7), max_rows=4) == 0


def test_sqlite_spans_share_the_ledger_file_and_survive_reopen(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.db"
    ledger = SqliteLedgerStore(path)
    spans = SqliteSpanStore(path)
    spans.append_spans([span(1, NOW)], NOW)
    spans.close()
    ledger.close()
    reopened = SqliteSpanStore(path)
    assert [s.span_id for s in reopened.spans_since(NOW)] == [f"{1:016x}"]
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION == 3
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    (received,) = db.execute("SELECT received_at FROM spans").fetchone()
    assert received == "2026-03-02T12:00:00.000000+00:00"


def test_v2_database_is_upgraded(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(storage.MIGRATIONS[1] + storage.MIGRATIONS[2] + "PRAGMA user_version = 2;")
    db.close()
    s = SqliteSpanStore(path)
    assert s.count() == 0
    s.close()


def test_sqlite_concurrent_appends(tmp_path: Path) -> None:
    store = SqliteSpanStore(tmp_path / "kelpie.db", batch=7)
    errors: list[Exception] = []

    def worker(base: int) -> None:
        try:
            store.append_spans([span(base + i, NOW) for i in range(50)], NOW)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(k * 1000,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and store.count() == 200


def test_bad_attributes_json_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.db"
    store = SqliteSpanStore(path)
    store.append_spans([span(1, NOW)], NOW)
    db = sqlite3.connect(path)
    db.execute("UPDATE spans SET attributes = '[1]'")
    db.commit()
    (s,) = store.spans_since(NOW)
    assert s.attributes == {}


# --- window, merge, retention -----------------------------------------------------------


def test_window_and_merge() -> None:
    store = MemorySpanStore()
    a, b = span(1, NOW - timedelta(minutes=5)), span(2, NOW - timedelta(hours=3))
    store.append_spans([a, b], NOW)
    window = SpanWindow(store, timedelta(hours=2), timedelta(hours=24))
    assert window.spans(NOW) == (a,)
    assert [t.spans for t in window.totals(NOW)] == [2]
    other = span(3, NOW)
    assert merge_spans(window.spans(NOW), [a, other]) == (a, other)


class Observer:
    def __init__(self) -> None:
        self.failures = 0

    def span_store_failed(self) -> None:
        self.failures += 1


def test_retention_runs_at_most_once_per_interval() -> None:
    store = MemorySpanStore()
    store.append_spans([span(1, NOW - timedelta(days=8)), span(2, NOW)], NOW)
    observer = Observer()
    retention = SpanRetention(store, lambda: NOW, timedelta(days=7), 100, observer)
    assert retention.maybe_start()
    retention.join(5)
    assert store.count() == 1
    assert not retention.maybe_start()  # within the hour
    retention.every = timedelta(0)
    assert retention.maybe_start()
    retention.join(5)
    assert store.count() == 1 and observer.failures == 0


class BrokenStore(MemorySpanStore):
    def prune(self, before: datetime, max_rows: int) -> int:
        raise sqlite3.OperationalError("disk I/O error")


def test_retention_failure_is_counted() -> None:
    observer = Observer()
    retention = SpanRetention(BrokenStore(), lambda: NOW, timedelta(days=7), 10, observer)
    assert retention.run_once() == 0 and observer.failures == 1


def linked(n: int, parent: int | None, task: str | None, session: str | None) -> Span:
    parent_id = None if parent is None else f"{parent:016x}"
    return replace(span(n, NOW, task=task, session=session), parent_id=parent_id)


def test_links_are_inherited_across_requests(store: SpanStore) -> None:
    # Children are exported first (they end first); the agent span arrives later.
    store.append_spans(
        [
            linked(3, 2, None, None),  # grandchild
            linked(2, 1, None, "own-session"),
            linked(9, 8, None, None),  # parent never arrives
        ],
        NOW,
    )
    assert store.inherit_links({"t" * 32}) == 1  # the grandchild takes its parent's session
    store.append_spans([linked(1, None, "T-1", "run-1")], NOW)
    assert store.inherit_links({"t" * 32}) > 0
    links = {s.span_id[-1]: (s.task_id, s.session_id) for s in store.spans_since(NOW)}
    assert links == {
        "1": ("T-1", "run-1"),
        "2": ("T-1", "own-session"),
        "3": ("T-1", "own-session"),
        "9": (None, None),
    }
    assert store.inherit_links({"t" * 32}) == 0
    assert store.inherit_links({"other"}) == 0
    assert [t.session_id for t in store.totals_since(NOW)] == ["own-session", "run-1"]
