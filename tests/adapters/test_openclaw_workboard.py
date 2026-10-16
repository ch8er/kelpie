"""OpenClaw Workboard cards as Kelpie tasks: status mapping, owners, blocks, dependencies."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.openclaw import workboard
from kelpie.adapters.openclaw.workboard import WorkboardError, read_board
from kelpie.core.model import Dependency, Status
from tests.sim.workboard import Workboard

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture
def board(tmp_path: Path) -> Workboard:
    return Workboard(tmp_path / "plugins" / "workboard" / "workboard.sqlite")


def tasks(board: Workboard) -> dict[str, workboard.Card]:
    return {c.task.id: c for c in read_board(board.path, CUTOFF, NOW, "openclaw").cards}


@pytest.mark.parametrize(
    ("raw", "status", "prework"),
    [
        ("triage", Status.BACKLOG, True),
        ("backlog", Status.BACKLOG, False),
        ("todo", Status.BACKLOG, False),
        ("scheduled", Status.BACKLOG, True),
        ("ready", Status.READY, False),
        ("running", Status.IN_PROGRESS, False),
        ("review", Status.REVIEW, False),
        ("blocked", Status.BLOCKED, False),
        ("done", Status.DONE, False),
    ],
)
def test_status_mapping(board: Workboard, raw: str, status: Status, prework: bool) -> None:
    board.card("c1", raw, ago(hours=1), title="Fix stale heartbeat", agent="ops")
    task = tasks(board)["c1"].task
    assert (task.status, task.platform_status, task.prework) == (status, raw, prework)
    assert (task.title, task.assignee, task.source) == ("Fix stale heartbeat", "ops", "openclaw")


def test_unknown_archived_and_old_done_cards_are_left_out(board: Workboard) -> None:
    board.card("weird", "parked", ago(hours=1))
    board.card("gone", "todo", ago(hours=1), archived_at=ago(minutes=5))
    board.card("old", "done", ago(days=3), completed_at=ago(days=2))
    board.card("recent", "done", ago(days=3), completed_at=ago(hours=2))
    assert set(tasks(board)) == {"recent"}


def test_status_since_is_the_latest_move_to_the_status(board: Workboard) -> None:
    board.card("c1", "todo", ago(hours=5))
    board.move("c1", "running", ago(hours=4))
    board.move("c1", "review", ago(hours=3))
    board.move("c1", "running", ago(hours=2))
    board.event("c1", "heartbeat", ago(minutes=1))
    assert tasks(board)["c1"].task.status_since == ago(hours=2)


def test_status_since_falls_back_to_the_card_times(board: Workboard) -> None:
    board.card("run", "running", ago(hours=5), started_at=ago(hours=1), event=False)
    board.card("todo", "todo", ago(hours=5), event=False)
    found = tasks(board)
    assert found["run"].task.status_since == ago(hours=1)
    assert found["todo"].task.status_since == ago(hours=5)


def test_owner_is_the_live_claim_holder(board: Workboard) -> None:
    claim = {"ownerId": "dev-2", "token": "SECRET-TOKEN", "expiresAt": NOW.timestamp() * 1000 + 6e5}
    board.card("claimed", "running", ago(hours=1), agent="dev", claim=claim)
    expired = {**claim, "expiresAt": ago(minutes=1).timestamp() * 1000}
    board.card("expired", "ready", ago(hours=1), agent="dev", claim=expired)
    board.card("unclaimed", "running", ago(hours=1), agent="dev")
    board.card("queued", "ready", ago(hours=1), agent="dev")
    found = tasks(board)
    assert (found["claimed"].task.owner_id, found["claimed"].task.assignee) == ("dev-2", "dev")
    assert found["expired"].task.owner_id is None
    assert found["unclaimed"].task.owner_id == "dev"
    assert (found["queued"].task.owner_id, found["queued"].task.role) == (None, "dev")
    assert "SECRET-TOKEN" not in repr(found)


def test_block_reason_from_the_blocked_attempt_or_comment(board: Workboard) -> None:
    board.card("a", "running", ago(hours=3))
    board.attempt("a", "failed", ago(hours=3), ago(hours=2), error="crashed")
    board.attempt("a", "blocked", ago(hours=2), ago(hours=1), error="Needs the API key")
    board.move("a", "blocked", ago(hours=1))
    board.card("b", "running", ago(hours=3))
    board.comment("b", "an old note", ago(hours=2))
    board.move("b", "blocked", ago(hours=1))
    board.comment("b", "Waiting for CI on PR #12", ago(hours=1))
    board.card("c", "blocked", ago(hours=1))
    found = tasks(board)
    assert found["a"].task.block is not None
    assert (found["a"].task.block.reason, found["a"].task.block.since) == (
        "Needs the API key",
        ago(hours=1),
    )
    assert found["b"].task.block is not None
    assert found["b"].task.block.reason == "Waiting for CI on PR #12"
    assert found["c"].task.block is not None and found["c"].task.block.reason == ""
    assert found["a"].task.status is Status.BLOCKED


def test_links(board: Workboard) -> None:
    for c in ("parent", "child", "blocker", "other", "self"):
        board.card(c, "todo", ago(hours=1))
    board.link("child", "parent", "parent", ago(hours=1))
    board.link("parent", "child", "child", ago(hours=1))
    board.link("other", "blocked_by", "blocker", ago(hours=1))
    board.link("blocker", "blocks", "other", ago(hours=1))
    board.link("other", "relates_to", "parent", ago(hours=1))
    board.link("self", "parent", "self", ago(hours=1))
    board.link("self", "parent", "missing", ago(hours=1))
    found = read_board(board.path, CUTOFF, NOW, "openclaw")
    assert set(found.dependencies) == {
        Dependency(task_id="child", depends_on="parent"),
        Dependency(task_id="other", depends_on="blocker"),
    }
    assert {c.task.id: c.task.subtasks for c in found.cards}["parent"] == ("child",)


def test_labels_body_and_session(board: Workboard) -> None:
    key = "agent:dev:subagent:workboard-default-c1"
    board.card("c1", "running", ago(hours=1), notes="Do the thing", session_key=key, run_id="r1")
    board.label("c1", "bug", "  ", "workboard")
    card = tasks(board)["c1"]
    assert (card.task.labels, card.task.body) == (("bug", "workboard"), "Do the thing")
    assert (card.session_key, card.run_id) == (key, "r1")


def test_a_changed_schema_is_an_error(board: Workboard) -> None:
    with sqlite3.connect(board.path) as db:
        db.execute("ALTER TABLE workboard_cards DROP COLUMN claim_json")
    with pytest.raises(WorkboardError, match=r"workboard_cards\.claim_json"):
        read_board(board.path, CUTOFF, NOW, "openclaw")


def test_missing_or_broken_database(tmp_path: Path) -> None:
    with pytest.raises(WorkboardError, match="not found"):
        read_board(tmp_path / "nope.sqlite", CUTOFF, NOW, "openclaw")
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"not a database" * 100)
    with pytest.raises(WorkboardError):
        read_board(bad, CUTOFF, NOW, "openclaw")


def test_read_only(board: Workboard) -> None:
    board.card("c1", "todo", ago(hours=1))
    before = board.path.read_bytes()
    read_board(board.path, CUTOFF, NOW, "openclaw")
    assert board.path.read_bytes() == before
