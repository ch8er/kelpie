"""Agent text is capped where it enters Kelpie (audit L9)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.core.actions import ActionKind
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import MAX_AGENT_TEXT, Block, Return, Session, capped
from kelpie.core.storage import MAX_STORED_TEXT, SqliteLedgerStore

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
HUGE = "x" * 200_000


def test_agent_reasons_are_capped_in_the_model() -> None:
    """A 200 KB block reason became a 200 KB report line and `kelpie why` output."""
    assert len(Block(HUGE, NOW).reason) == MAX_AGENT_TEXT
    assert Block(HUGE, NOW).reason.endswith("…")
    assert len(Return(NOW, HUGE).reason) == MAX_AGENT_TEXT
    session = Session("s", "t", "o", NOW, exit_reason=HUGE, summary=HUGE)
    assert len(session.exit_reason or "") == MAX_AGENT_TEXT
    assert len(session.summary or "") == MAX_AGENT_TEXT
    assert Block("short", NOW).reason == "short"  # untouched below the cap
    assert Session("s", "t", "o", NOW).exit_reason is None


def test_capped_keeps_the_limit() -> None:
    assert capped("abc", 3) == "abc"
    assert capped("abcd", 3) == "ab…"


def test_the_ledger_stores_at_most_the_cap(tmp_path: Path) -> None:
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    store.append(
        LedgerEntry(
            NOW,
            "fp",
            "t",
            "stale_blocks",
            ActionKind.ALERT,
            Mode.SUGGEST,
            Outcome.EXECUTED,
            HUGE,
            detail=HUGE,
        )
    )
    (entry,) = store.since(NOW - timedelta(hours=1))
    assert len(entry.reason) == MAX_STORED_TEXT
    assert len(entry.detail or "") == MAX_STORED_TEXT
