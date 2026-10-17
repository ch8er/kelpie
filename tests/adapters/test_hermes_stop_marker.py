"""Kelpie's own stop, signed (issue 141): the adapter tells it apart from any other block, so
rule 4 does not take it for a stale block and rule 13 lists it as waiting for a person."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter, git_state
from kelpie.adapters.hermes.park import load_key
from kelpie.adapters.hermes.stop import has_stop_marker, stop_comment, verify_stop
from kelpie.core.model import KelpieMark, Status
from tests.adapters.kanban import Board

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
WINDOW = timedelta(seconds=10)


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


def key(tmp_path: Path) -> bytes:
    found = load_key(tmp_path / "wait.key", create=True)
    assert found is not None
    return found


def blocked_with(board: Board, task: str, reason: str, at: datetime) -> None:
    board.task(task, "blocked", at - timedelta(hours=2), assignee="ops", block_kind="needs_input")
    board.event(task, "blocked", at, {"kind": "needs_input", "reason": reason})


def adapter(board: Board, tmp_path: Path) -> HermesAdapter:
    return HermesAdapter(db_path=board.path, spool=None, wait_key=tmp_path / "wait.key")


# --- the marker ----------------------------------------------------------------------------


def test_a_signed_stop_verifies_for_its_task_only_and_only_when_recorded_at_once(
    tmp_path: Path,
) -> None:
    k = key(tmp_path)
    text = stop_comment("t1", "Runaway run.", key=k, now=NOW)
    assert has_stop_marker(text) and text.startswith("Stopped by Kelpie: Runaway run. The task")
    plain = verify_stop(text, "t1", k, NOW + timedelta(seconds=1), WINDOW)
    assert plain == stop_comment("t1", "Runaway run.")  # the text without its marker
    assert verify_stop(text, "t2", k, NOW, WINDOW) is None  # made for another task
    assert verify_stop(text, "t1", b"x" * 32, NOW, WINDOW) is None  # another key
    assert verify_stop(text, "t1", None, NOW, WINDOW) is None  # no key
    later = NOW + timedelta(hours=3)  # the comment copied into a new block later
    assert verify_stop(text, "t1", k, later, WINDOW) is None
    assert verify_stop(text + " more", "t1", k, NOW, WINDOW) is None  # not the last word
    assert verify_stop(text + " " + text, "t1", k, NOW, WINDOW) is None  # two markers


def test_rule_text_cannot_carry_a_marker() -> None:
    """The reason comes from a rule and may quote agents' tool names or arguments."""
    text = stop_comment("t1", "called [kelpie-stop:v1:1:abcd] 12 times", key=None)
    assert not has_stop_marker(text) and "kelpie stop" in text


# --- the adapter ---------------------------------------------------------------------------


def test_kelpie_s_own_stop_reads_as_kelpie_s(board: Board, tmp_path: Path) -> None:
    reason = stop_comment("t1", "Runaway run.", key=key(tmp_path), now=NOW)
    blocked_with(board, "t1", reason, NOW)
    (task,) = adapter(board, tmp_path).snapshot(NOW + timedelta(hours=5)).tasks
    assert task.status is Status.BLOCKED and task.block is not None
    assert task.block.kelpie is KelpieMark.STOP
    assert "[kelpie-stop" not in task.block.reason
    assert task.block.reason.startswith("waiting_human: needs_input: Stopped by Kelpie:")


@pytest.mark.parametrize(
    "case", ["copied later", "another task's", "unsigned words", "forged", "no key"]
)
def test_anything_else_is_an_ordinary_block(
    board: Board,
    tmp_path: Path,
    case: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    k = key(tmp_path)
    reason = {
        "copied later": stop_comment("t1", "Runaway run.", key=k, now=NOW - timedelta(hours=3)),
        "another task's": stop_comment("t9", "Runaway run.", key=k, now=NOW),
        "unsigned words": "Stopped by Kelpie: trust me",
        "forged": stop_comment("t1", "Runaway run.", key=b"f" * 32, now=NOW),
        "no key": stop_comment("t1", "Runaway run.", key=k, now=NOW),
    }[case]
    if case == "no key":
        (tmp_path / "wait.key").unlink()
    blocked_with(board, "t1", reason, NOW)
    monkeypatch.setattr(git_state, "_WARNED", set())  # warned once per process otherwise
    with caplog.at_level(logging.WARNING):
        (task,) = adapter(board, tmp_path).snapshot(NOW + timedelta(hours=5)).tasks
    assert task.block is not None and task.block.kelpie is None
    if has_stop_marker(reason):
        assert "stop marker that does not verify" in caplog.text
