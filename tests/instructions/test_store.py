"""The instruction size history: the size at the window start, and pruning."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kelpie.core.storage import SizeRow, SqliteInstructionStore

T0 = datetime(2026, 3, 1, tzinfo=UTC)


def row(path: str, day: int, chars: int) -> SizeRow:
    return SizeRow(path, T0 + timedelta(days=day), chars, chars, f"h{chars}", 1, day > 0 or None)


def test_history_starts_at_the_last_row_at_or_before_the_cutoff() -> None:
    store = SqliteInstructionStore(":memory:")
    store.record([row("a", 0, 10), row("a", 2, 20), row("a", 5, 30), row("b", 6, 5)])
    hist = store.history(T0 + timedelta(days=2))
    assert [r.chars for r in hist["a"]] == [20, 30]  # the row exactly at the cutoff counts
    assert [r.chars for r in store.history(T0 + timedelta(days=3))["a"]] == [20, 30]
    assert [r.chars for r in hist["b"]] == [5]
    assert hist["a"][1].appended is True and store.history(T0)["a"][0].appended is None


def test_prune_keeps_the_size_at_the_cutoff_and_forgets_files_gone() -> None:
    store = SqliteInstructionStore(":memory:")
    store.record([row("a", 0, 10), row("a", 2, 20), row("a", 5, 30), row("gone", 1, 7)])
    assert store.prune(T0 + timedelta(days=3)) == 1  # a@0; gone@1 is its last row: kept
    assert [r.chars for r in store.history(T0)["a"]] == [20, 30]
    assert store.prune(T0 + timedelta(days=3), keep={"a"}) == 1
    assert set(store.history(T0)) == {"a"}
