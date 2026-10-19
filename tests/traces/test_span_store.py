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
from kelpie.core.spans import MemorySpanStore, SpanStore, TokenUsage, totals, usage
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


def llm(trace: str, span_id: str, parent: str | None, tokens: int, *, model: str = "m") -> Span:
    t0 = NOW - timedelta(minutes=10)
    return Span(
        trace_id=trace,
        span_id=span_id,
        name="chat",
        kind=SpanKind.LLM,
        start=t0,
        end=t0 + timedelta(seconds=5),
        parent_id=parent,
        task_id="T-1",
        session_id="s-1",
        model=model,
        input_tokens=tokens,
        output_tokens=tokens // 10,
        cached_tokens=tokens // 2,
    )


def nested_trace() -> list[Span]:
    """One trace: an agent span, a framework chat span wrapping a provider chat span
    (one call instrumented twice), a tool span with a provider chat span inside the
    framework span, and a sibling chat call that counts on its own."""
    agent = replace(llm("a" * 32, "agent", None, 0), kind=SpanKind.SESSION, model=None)
    return [
        agent,
        llm("a" * 32, "framework", "agent", 1000, model="fw"),
        llm("a" * 32, "provider", "framework", 1000),
        replace(llm("a" * 32, "tool", "framework", 0), kind=SpanKind.TOOL, model=None),
        llm("a" * 32, "in-tool", "tool", 400),  # LLM ancestor through a tool span
        llm("a" * 32, "sibling", "agent", 300),
        llm("b" * 32, "other-trace", "framework", 50),  # same parent id, another trace
    ]


def test_nested_llm_spans_count_once(store: SpanStore) -> None:
    store.append_spans(nested_trace(), NOW)
    (got,) = store.totals_since(NOW - timedelta(hours=1))
    # framework (1000) + sibling (300) + the other trace's call (its parent is not stored)
    assert (got.llm_calls, got.input_tokens, got.output_tokens) == (3, 1350, 135)
    assert got.cached_tokens == 500 + 150 + 25
    assert got.spans == 7
    assert list(store.usage_since(NOW, [timedelta(hours=1)])[0]) == [
        TokenUsage("s-1", "T-1", "fw", 1, 1000, 100, 500),
        TokenUsage("s-1", "T-1", "m", 2, 350, 35, 175),
    ]


def test_nested_llm_spans_count_once_when_the_child_arrives_first(store: SpanStore) -> None:
    spans = nested_trace()
    children, parents = spans[2:], spans[:2]  # the framework span arrives last
    store.append_spans(children, NOW)
    before = store.totals_since(NOW - timedelta(hours=1))
    # until the parent arrives, both of its LLM descendants are roots: nothing is lost
    assert before[0].input_tokens == 1000 + 400 + 300 + 50
    store.append_spans(parents, NOW + timedelta(seconds=1))
    (after,) = store.totals_since(NOW - timedelta(hours=1))
    assert after.input_tokens == 1350
    assert sum(u.input_tokens for u in store.usage_since(NOW, [timedelta(hours=1)])[0]) == 1350


def test_nested_llm_parent_outside_the_window_still_counts_once(store: SpanStore) -> None:
    parent = llm("c" * 32, "framework", None, 500)
    old = replace(parent, start=NOW - timedelta(hours=3), end=NOW - timedelta(hours=3))
    child = llm("c" * 32, "provider", "framework", 500)
    store.append_spans([old, child], NOW)
    # the parent left the window but is still stored: the child is its duplicate
    assert store.totals_since(NOW - timedelta(hours=1))[0].input_tokens == 0
    assert list(store.usage_since(NOW, [timedelta(hours=1)])[0]) == []


def usage_of(store: SpanStore, *spans: Span) -> list[tuple[str | None, int]]:
    store.append_spans(spans, NOW)
    return [(u.model, u.input_tokens) for u in store.usage_since(NOW, [timedelta(hours=1)])[0]]


def test_a_wrapper_without_usage_hides_nothing(store: SpanStore) -> None:
    wrapper = replace(llm("w" * 32, "W", None, 0), model=None)
    assert usage_of(store, wrapper, llm("w" * 32, "P", "W", 100)) == [("m", 100)]


def test_a_call_without_a_model_is_priced_by_its_wrapper(store: SpanStore) -> None:
    wrapper = llm("w" * 32, "W", None, 0, model="fw")
    inner = replace(llm("w" * 32, "P", "W", 100), model=None)
    assert usage_of(store, inner, wrapper) == [("fw", 100)]


def test_a_wrapper_without_usage_around_two_calls_counts_both(store: SpanStore) -> None:
    wrapper = llm("w" * 32, "W", None, 0)
    assert usage_of(store, wrapper, llm("w" * 32, "P1", "W", 60), llm("w" * 32, "P2", "W", 40)) == [
        ("m", 100)
    ]
    (t,) = store.totals_since(NOW - timedelta(hours=1))
    assert (t.llm_calls, t.input_tokens) == (2, 100)


def test_a_wrapper_with_usage_around_two_calls_counts_once(store: SpanStore) -> None:
    wrapper = llm("w" * 32, "W", None, 100)
    assert usage_of(store, llm("w" * 32, "P1", "W", 60), llm("w" * 32, "P2", "W", 40), wrapper) == [
        ("m", 100)
    ]


def test_parent_loops_end_and_the_span_counts(store: SpanStore) -> None:
    looped = llm("l" * 32, "A", "A", 100)
    assert usage_of(store, looped) == [("m", 100)]
    tool = replace(llm("k" * 32, "B", "C", 0), kind=SpanKind.TOOL, model=None)
    assert usage_of(store, llm("k" * 32, "C", "B", 50), tool) == [("m", 150)]


def test_usage_for_several_windows(store: SpanStore) -> None:
    old = llm("o" * 32, "O", None, 70)
    old = replace(old, start=NOW - timedelta(hours=5), end=NOW - timedelta(hours=5))
    store.append_spans([old, llm("n" * 32, "N", None, 30)], NOW)
    day, hour, never = store.usage_since(
        NOW, [timedelta(hours=24), timedelta(hours=1), timedelta(hours=-1)]
    )
    assert [u.input_tokens for u in day] == [100] and day[0].calls == 2
    assert [u.input_tokens for u in hour] == [30]
    assert never == ()
    assert store.usage_since(NOW, []) == []


def test_attribution_is_remembered_and_pruned(store: SpanStore) -> None:
    sessions = [("run-1", "agent-1", "T-1"), ("run-2", "agent-2", None)]
    assert store.record_attribution(sessions, {"T-1": ("web",)}, NOW - timedelta(days=2)) == 3
    # seen again unchanged within the hour: nothing is written
    assert store.record_attribution(sessions, {"T-1": ("web",)}, NOW - timedelta(days=2)) == 0
    assert store.record_attribution([("run-1", "agent-1", "T-1")], {}, NOW) == 1  # refreshed
    got = store.attribution()
    assert dict(got.owners) == {"run-1": "agent-1", "run-2": "agent-2"}
    assert dict(got.session_tasks) == {"run-1": "T-1"}
    assert dict(got.labels) == {"T-1": ("web",)}
    store.prune(NOW - timedelta(days=1), max_rows=100)
    got = store.attribution()
    assert dict(got.owners) == {"run-1": "agent-1"} and dict(got.labels) == {}
    assert store.record_attribution(sessions[1:], {"T-1": ("web",)}, NOW) == 2


def test_pure_helpers_resolve_ancestry_among_the_spans_given() -> None:
    spans = nested_trace()
    assert totals(reversed(spans))[0].input_tokens == 1350
    assert sum(u.input_tokens for u in usage(spans)) == 1350


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
    assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION == 8
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


def test_v3_database_is_upgraded_and_its_spans_decided(tmp_path: Path) -> None:
    path = tmp_path / "v3.db"
    db = sqlite3.connect(path)
    db.executescript("".join(storage.MIGRATIONS[v] for v in (1, 2, 3)) + "PRAGMA user_version = 3;")
    columns = (
        "trace_id, span_id, parent_id, name, kind, start, last_at, session_id, model,"
        " input_tokens, attributes, received_at"
    )
    at = storage._ts(NOW - timedelta(minutes=5))
    for sid, parent, tokens in (("P", "W", 100), ("W", None, 100), ("S", None, 7)):
        db.execute(
            f"INSERT INTO spans ({columns}) VALUES (?, ?, ?, 'chat', 'llm', ?, ?, 's', 'm', ?,"
            " '{}', ?)",
            ("t" * 32, sid, parent, at, at, tokens, at),
        )
    db.commit()
    db.close()
    s = SqliteSpanStore(path)
    assert [u.input_tokens for u in s.usage_since(NOW, [timedelta(hours=1)])[0]] == [107]
    s.close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
    assert db.execute("SELECT COUNT(*) FROM spans WHERE counted IS NULL").fetchone()[0] == 0


def test_v4_database_gets_cache_writes_from_kept_attributes(tmp_path: Path) -> None:
    """Spans stored before cache writes had a column kept them in ``attributes``: the
    upgrade fills the column from there, capped at the uncached input."""
    path = tmp_path / "v4.db"
    db = sqlite3.connect(path)
    db.executescript("".join(storage.MIGRATIONS[v] for v in (1, 2, 3, 4)))
    db.execute("PRAGMA user_version = 4")
    columns = (
        "trace_id, span_id, name, kind, start, last_at, session_id, model,"
        " input_tokens, cached_tokens, attributes, received_at, counted, price_model"
    )
    at = storage._ts(NOW - timedelta(minutes=5))
    for sid, kind, tokens, cached, attrs in (
        ("A", "llm", 1000, 200, '{"gen_ai.usage.cache_creation.input_tokens":300}'),
        ("B", "llm", 1000, 0, '{"cache_creation_tokens":"400"}'),
        ("C", "llm", 1000, 900, '{"gen_ai.usage.cache_write.input_tokens":500}'),  # capped
        ("D", "llm", 1000, 0, '{"gen_ai.usage.cache_creation_input_tokens":-5}'),
        ("E", "tool", 0, 0, '{"cache_creation_tokens":9}'),  # not an LLM call
        ("F", "llm", 1000, 0, "not json cache_creation"),
    ):
        db.execute(
            f"INSERT INTO spans ({columns}) VALUES (?, ?, 'chat', ?, ?, ?, 's', 'm', ?, ?, ?, ?,"
            " ?, 'm')",
            ("t" * 32, sid, kind, at, at, tokens, cached, attrs, at, int(kind == "llm")),
        )
    db.commit()
    db.close()
    s = SqliteSpanStore(path)
    (rows,) = s.usage_since(NOW, [timedelta(hours=1)])
    assert [(u.cache_write_tokens, u.cached_tokens) for u in rows] == [(300 + 400 + 100, 1100)]
    s.close()
    db = sqlite3.connect(path)
    found = dict(db.execute("SELECT span_id, cache_write_tokens FROM spans").fetchall())
    assert found == {"A": 300, "B": 400, "C": 100, "D": 0, "E": 0, "F": 0}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_cache_writes_are_summed_with_the_counted_calls(tmp_path: Path, kind: str) -> None:
    store: SpanStore = MemorySpanStore() if kind == "memory" else SqliteSpanStore(tmp_path / "k.db")
    outer = replace(llm("t" * 32, "W", None, 1000), cache_write_tokens=600)
    inner = replace(llm("t" * 32, "P", "W", 1000), cache_write_tokens=600)  # same call
    other = replace(llm("u" * 32, "S", None, 500), cache_write_tokens=50)
    store.append_spans([outer, inner, other], NOW)
    (rows,) = store.usage_since(NOW, [timedelta(hours=1)])
    assert sum(u.cache_write_tokens for u in rows) == 650
    assert [s.cache_write_tokens for s in store.spans_since(NOW - timedelta(hours=1))] == [
        600,
        600,
        50,
    ]


def test_read_only_open_never_writes(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(storage.MIGRATIONS[1] + storage.MIGRATIONS[2] + "PRAGMA user_version = 2;")
    db.close()
    with pytest.raises(storage.SchemaOutdated):
        SqliteSpanStore(path, readonly=True)
    SqliteSpanStore(path).close()  # serve upgrades it
    writer = SqliteSpanStore(path)
    writer.append_spans([llm("r" * 32, "A", None, 10)], NOW)
    writer.close()
    reader = SqliteSpanStore(path, readonly=True)
    assert [u.input_tokens for u in reader.usage_since(NOW, [timedelta(hours=1)])[0]] == [10]
    with pytest.raises(sqlite3.OperationalError):
        reader.append_spans([llm("r" * 32, "B", None, 10)], NOW)
    reader.close()
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {storage.SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        SqliteSpanStore(path, readonly=True)


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
