"""Stop markers dated anywhere: the Claude Code and OpenClaw adapters never raise (#388).

Agents can write the stop directory. A marker dated in year 9999 used to raise
``OverflowError`` out of ``snapshot()`` (``marked + ttl``), taking the whole adapter offline
while the marker stayed. A marker is now used only inside the range in which it can still say
something about the spool's tool calls, and exactly as before there; outside it it is
ignored with one warning per session. Malformed markers were already ignored and still are.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters import _spool
from kelpie.adapters.claude_code import ClaudeCodeAdapter, hook
from kelpie.adapters.claude_code import adapter as claude_code
from kelpie.adapters.hermes import adapter as hermes
from kelpie.adapters.openclaw import OpenClawAdapter
from kelpie.adapters.openclaw import adapter as openclaw
from kelpie.adapters.openclaw.workboard import read_board
from kelpie.core.model import Status
from tests.adapters.test_openclaw_adapter import Spool
from tests.sim.workboard import Workboard

S1 = "0b5c6f7e-0000-4000-8000-000000000001"  # a Claude Code session id
SID = "1ce1fbe0-c1fc-4572-91c4-394ad62e4a40"  # an OpenClaw session id
YEAR_9999 = 253402300799  # 9999-12-31T23:59:59Z, the latest time fromtimestamp accepts
YEAR_1 = -62135596800  # 0001-01-01T00:00:00Z, the earliest
NOT_ENFORCED = "stop not enforced"

# What a marker file may hold that is not a usable time.
MALFORMED: list[bytes] = [
    b"",
    b"not json",
    b"[]",
    b"{}",
    b'{"at": null}',
    b'{"at": "1792191669"}',
    b'{"at": true}',
    b'{"at": NaN}',
    b'{"at": Infinity}',
    b'{"at": -Infinity}',
    b'{"at": 1e300}',
    b'{"at": -1e300}',
    b'{"at": 99999999999999999999999}',
    b'{"at": [1792191669]}',
    b'{"at": 1}' + b" " * 5000,  # over MAX_MARKER
    b"[" * 100_000,  # deep nesting
]


# --- the range check --------------------------------------------------------------------


def test_within_never_overflows() -> None:
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    back, ahead = timedelta(days=9), timedelta(minutes=5)
    assert _spool.within(now, now, back, ahead)
    assert _spool.within(now + ahead, now, back, ahead)
    assert _spool.within(now - back, now, back, ahead)
    assert not _spool.within(now + ahead + timedelta(microseconds=1), now, back, ahead)
    assert not _spool.within(now - back - timedelta(microseconds=1), now, back, ahead)
    assert not _spool.within(datetime.max.replace(tzinfo=UTC), now, back, ahead)
    assert not _spool.within(datetime.min.replace(tzinfo=UTC), now, back, ahead)


# --- Claude Code --------------------------------------------------------------------------


def cc_adapter(tmp_path: Path, **kw: Any) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(
        tasks_dir=tmp_path / "tasks",
        spool=tmp_path / "spool.jsonl",
        stop_dir=tmp_path / "stop",
        process_check=lambda pid, recorded: None,
        **kw,
    )


def cc_session(tmp_path: Path, now: float) -> None:
    """A live session whose newest tool call is a minute old."""
    lines = [
        {"type": "session_start", "at": now - 900, "session_id": S1, "v": 1},
        {"type": "tool", "at": now - 60, "session_id": S1, "v": 1, "tool": "Bash"},
    ]
    (tmp_path / "spool.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


def cc_marker(tmp_path: Path, body: bytes) -> None:
    stop = tmp_path / "stop"
    stop.mkdir(exist_ok=True, mode=0o700)
    (stop / f"{S1}.json").write_bytes(body)


def cc_reason(tmp_path: Path, now: float, **kw: Any) -> str | None:
    (session,) = cc_adapter(tmp_path, **kw).snapshot(datetime.fromtimestamp(now, UTC)).sessions
    return session.exit_reason


def test_claude_code_valid_marker_still_flags_an_unenforced_stop(tmp_path: Path) -> None:
    now = time.time()
    cc_session(tmp_path, now)
    claude_code.write_marker(tmp_path / "stop", S1, "loop", now - 600)
    assert cc_reason(tmp_path, now) == NOT_ENFORCED
    # Within the future skew (another host's clock) a marker is still read, as before.
    claude_code.write_marker(tmp_path / "stop", S1, "loop", now + 240)
    at = datetime.fromtimestamp(now, UTC)
    event = claude_code.SpoolEvent(type="tool", at=at, key="k", session_id=S1)
    assert S1 in cc_adapter(tmp_path)._markers([event], at)


def test_claude_code_expired_marker_is_no_miss(tmp_path: Path) -> None:
    now = time.time()
    cc_session(tmp_path, now)
    claude_code.write_marker(tmp_path / "stop", S1, "loop", now - hook.DEFAULT_STOP_TTL - 3600)
    assert cc_reason(tmp_path, now, lookback=timedelta(days=3)) is None


@pytest.mark.parametrize("at", [YEAR_9999, YEAR_1, time.time() + 3600, 946684800])
def test_claude_code_out_of_range_marker_is_ignored_and_logged_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, at: float
) -> None:
    now = time.time()
    cc_session(tmp_path, now)
    cc_marker(tmp_path, json.dumps({"at": at, "reason": "x"}).encode())
    a = cc_adapter(tmp_path)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):  # used to raise OverflowError on every tick
            (s,) = a.snapshot(datetime.fromtimestamp(now, UTC)).sessions
    assert s.exit_reason is None
    assert sum("outside the range" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("body", MALFORMED)
def test_claude_code_malformed_marker_is_ignored(tmp_path: Path, body: bytes) -> None:
    now = time.time()
    cc_session(tmp_path, now)
    cc_marker(tmp_path, body)
    assert cc_reason(tmp_path, now) is None


def test_claude_code_marker_range_is_exact(tmp_path: Path) -> None:
    """The oldest marker kept is one whose TTL had not yet run out when the oldest possible
    call in the lookback began: one older could never flag anything."""
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    a = cc_adapter(tmp_path)
    back = a.lookback + timedelta(milliseconds=claude_code.MAX_DURATION_MS)
    back += claude_code.MARKER_TTL
    event = claude_code.SpoolEvent(type="tool", at=now, key="k", session_id=S1)
    for offset, kept in ((-back, True), (-back - timedelta(seconds=1), False)):
        claude_code.write_marker(tmp_path / "stop", S1, "x", (now + offset).timestamp())
        assert bool(a._markers([event], now)) is kept
    # A call begun just inside the TTL of the oldest marker kept is still a miss.
    marked = now - back
    begun = marked + claude_code.MARKER_TTL - timedelta(seconds=1)
    call = claude_code.SpoolEvent(
        type="tool",
        at=now - a.lookback,
        key="k",
        session_id=S1,
        duration_ms=int((now - a.lookback - begun) / timedelta(milliseconds=1)),
    )
    assert claude_code._ignored_stop([call], marked)


# --- OpenClaw -----------------------------------------------------------------------------


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    return Spool(tmp_path / "kelpie" / "spool.jsonl")


def oc_adapter(tmp_path: Path, spool: Spool, **kw: Any) -> OpenClawAdapter:
    return OpenClawAdapter(
        state_dir=tmp_path,
        workboard_db=None,
        spool=spool.path,
        stop_dir=tmp_path / "kelpie" / "stop",
        **kw,
    )


def oc_marker(tmp_path: Path, body: bytes) -> None:
    stop = tmp_path / "kelpie" / "stop"
    stop.mkdir(parents=True, exist_ok=True, mode=0o700)
    (stop / f"{SID}.json").write_bytes(body)


def test_openclaw_valid_marker_still_flags_an_unenforced_stop(tmp_path: Path, spool: Spool) -> None:
    now = datetime.now(UTC)
    spool.tool(SID, now - timedelta(minutes=1))
    oc_marker(tmp_path, json.dumps({"at": (now - timedelta(minutes=10)).timestamp()}).encode())
    (s,) = oc_adapter(tmp_path, spool).snapshot(now).sessions
    assert s.exit_reason == NOT_ENFORCED


def test_openclaw_expired_marker_is_no_miss(tmp_path: Path, spool: Spool) -> None:
    now = datetime.now(UTC)
    spool.tool(SID, now - timedelta(minutes=1))
    oc_marker(tmp_path, json.dumps({"at": (now - timedelta(hours=3)).timestamp()}).encode())
    (s,) = oc_adapter(tmp_path, spool, stop_ttl=timedelta(hours=2)).snapshot(now).sessions
    assert s.exit_reason is None


@pytest.mark.parametrize("at", [YEAR_9999, YEAR_1, time.time() + 3600, 946684800])
def test_openclaw_out_of_range_marker_is_ignored_and_logged_once(
    tmp_path: Path, spool: Spool, caplog: pytest.LogCaptureFixture, at: float
) -> None:
    now = datetime.now(UTC)
    spool.tool(SID, now - timedelta(minutes=1))
    oc_marker(tmp_path, json.dumps({"at": at}).encode())
    a = oc_adapter(tmp_path, spool)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            (s,) = a.snapshot(now).sessions
    assert s.exit_reason is None
    assert sum("outside the range" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("body", MALFORMED)
def test_openclaw_malformed_marker_is_ignored(tmp_path: Path, spool: Spool, body: bytes) -> None:
    now = datetime.now(UTC)
    spool.tool(SID, now - timedelta(minutes=1))
    oc_marker(tmp_path, body)
    (s,) = oc_adapter(tmp_path, spool).snapshot(now).sessions
    assert s.exit_reason is None


def test_openclaw_ignored_stop_takes_any_marker_date() -> None:
    """Defence in depth: the check itself no longer adds to the marker's date."""
    now = datetime(2026, 11, 11, 12, tzinfo=UTC)
    call = openclaw.SpoolEvent(type="tool", at=now, key="k", session_id=SID)
    for marked in (datetime.max.replace(tzinfo=UTC), datetime.min.replace(tzinfo=UTC)):
        assert not openclaw._ignored_stop([call], marked, timedelta(days=1))
        assert not claude_code._ignored_stop(
            [claude_code.SpoolEvent(type="tool", at=now, key="k", session_id=S1)], marked
        )


# --- other agent-written times next to a duration ---------------------------------------


def test_workboard_block_moved_in_year_1_does_not_raise(tmp_path: Path) -> None:
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    board = Workboard(tmp_path / "workboard.sqlite")
    board.card("c1", "todo", now - timedelta(hours=1))
    board.move("c1", "blocked", datetime.fromtimestamp(YEAR_1, UTC))
    board.attempt("c1", "blocked", now - timedelta(minutes=30), now - timedelta(minutes=20))
    (card,) = read_board(board.path, now - timedelta(days=1), now, "openclaw").cards
    assert card.task.status is Status.BLOCKED


@pytest.mark.parametrize("at", [YEAR_9999, YEAR_1])
def test_hermes_spool_line_dated_anywhere_is_dropped(tmp_path: Path, at: float) -> None:
    """Hermes' stop and park markers are HMAC-signed by Kelpie; its spool lines are bounded
    by the lookback and ``FUTURE_SKEW`` before any duration is applied."""
    now = datetime.now(UTC)
    spool = tmp_path / "spool.jsonl"
    line = {"type": "tool", "at": at, "run_id": "1", "task_id": "t_1", "duration_ms": 10**9}
    spool.write_text(json.dumps(line) + "\n")
    a = hermes.HermesAdapter(db_path=tmp_path / "kanban.db", spool=spool)
    assert a._spans(now - timedelta(days=1), now + hermes.FUTURE_SKEW) == ()
