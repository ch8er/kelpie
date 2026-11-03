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


def test_titles_and_comments_are_capped() -> None:
    from kelpie.core.model import Comment, Status, Task

    assert len(Task("t", HUGE, Status.READY, NOW).title) == MAX_AGENT_TEXT
    assert len(Comment("a", HUGE, NOW).text) == MAX_AGENT_TEXT


def test_a_cut_cannot_turn_a_quoted_marker_into_a_live_one() -> None:
    """The cut may drop a closing quote; what follows the opening one stays quoted."""
    from kelpie.core.snooze import parse_marker

    raw = 'waiting on build. "copied error: until:2026-11-20 ' + "x" * 5000 + '"'
    assert parse_marker(raw) == (None, ())
    assert parse_marker(Block(raw, NOW).reason) == (None, ())
    assert parse_marker("blocked until:2026-11-20")[0] is not None  # unquoted still counts


def test_the_ledger_cuts_agent_text_in_params(tmp_path: Path) -> None:
    """A ping-pong escalation copies every review return into its params."""
    from kelpie.core.storage import MAX_PARAM_TEXT, MAX_STORED_PARAMS

    store = SqliteLedgerStore(tmp_path / "kelpie.db")

    def entry(fp: str, params: dict[str, object]) -> LedgerEntry:
        return LedgerEntry(
            NOW,
            fp,
            "t",
            "ping_pong",
            ActionKind.ESCALATE,
            Mode.SUGGEST,
            Outcome.EXECUTED,
            "r",
            params=params,
        )

    returns = [{"reason": "y" * 4000, "at": "2026-11-03"} for _ in range(40)]
    store.append(entry("a", {"returns": returns, "rounds": 40}))
    store.append(entry("b", {"returns": [{"reason": "z"}] * 5000}))
    store.append(entry("c", {"rounds": 3}))
    got = {e.fingerprint: e.params for e in store.since(NOW - timedelta(hours=1))}
    assert got["a"]["rounds"] == 40 and len(got["a"]["returns"]) == 40
    assert all(len(r["reason"]) == MAX_PARAM_TEXT for r in got["a"]["returns"])
    assert got["b"] == {"truncated": True}  # still too large once every text is cut
    assert got["c"] == {"rounds": 3}
    assert MAX_STORED_PARAMS > 1000
