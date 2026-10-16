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
    # A Workboard parent is a prerequisite of its children, not a split: no subtasks.
    assert all(c.task.subtasks == () for c in found.cards)


def test_a_block_reason_is_from_this_block(board: Workboard) -> None:
    board.card("c", "running", ago(days=4))
    board.attempt("c", "blocked", ago(days=4), ago(days=3), error="old: no creds")
    board.move("c", "blocked", ago(days=3))
    board.move("c", "ready", ago(days=2))
    board.move("c", "blocked", ago(minutes=30))
    board.comment("c", "Needs a decision on the schema", ago(minutes=30))
    block = tasks(board)["c"].task.block
    assert block is not None and block.reason == "Needs a decision on the schema"


def test_the_claim_token_never_leaves_sqlite(
    board: Workboard, monkeypatch: pytest.MonkeyPatch
) -> None:
    claim = {"ownerId": "dev", "token": "SECRET-TOKEN", "expiresAt": NOW.timestamp() * 1000 + 1}
    board.card("c", "running", ago(hours=1), claim=claim)
    seen: list[object] = []
    real = workboard.connect

    def spying(path: Path, deadline: float = workboard.READ_DEADLINE) -> sqlite3.Connection:
        conn = real(path, deadline)
        conn.row_factory = lambda cur, row: seen.append(row) or sqlite3.Row(cur, row)
        return conn

    monkeypatch.setattr(workboard, "connect", spying)
    assert tasks(board)["c"].task.owner_id == "dev"
    assert seen and "SECRET-TOKEN" not in repr(seen)


def test_malformed_claims(board: Workboard) -> None:
    board.card("a", "running", ago(hours=1), agent="dev")
    with sqlite3.connect(board.path) as db:
        db.execute("UPDATE workboard_cards SET claim_json = ? WHERE id = 'a'", ("[" * 5000,))
    assert tasks(board)["a"].task.owner_id == "dev"


def test_cards_with_unusable_ids_are_left_out(board: Workboard) -> None:
    board.card("x" * 300, "todo", ago(hours=1))
    board.card("ok", "todo", ago(hours=1))
    board.card("weird", "parked", ago(hours=1))
    board.link("ok", "parent", "x" * 300, ago(hours=1))
    found = read_board(board.path, CUTOFF, NOW, "openclaw")
    assert [c.task.id for c in found.cards] == ["ok"]
    assert found.dependencies == ()
    assert found.unknown == ("parked",)


def test_text_is_cut_in_the_query(board: Workboard) -> None:
    board.card("c", "blocked", ago(hours=1), title="t" * 5000, notes="n" * 20000)
    board.comment("c", "c" * 9000, ago(minutes=1))
    task = tasks(board)["c"].task
    assert len(task.title) == workboard.MAX_TITLE and len(task.body or "") == workboard.MAX_BODY
    assert task.block is not None and len(task.block.reason) == workboard.MAX_REASON


def test_a_view_in_place_of_a_table_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "workboard.sqlite"
    Workboard(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE workboard_cards RENAME TO real_cards")
        db.execute("CREATE VIEW workboard_cards AS SELECT * FROM real_cards")
    with pytest.raises(WorkboardError, match="lacks workboard_cards"):
        read_board(path, CUTOFF, NOW, "openclaw")


def test_a_slow_read_is_interrupted(board: Workboard) -> None:
    conn = workboard.connect(board.path, deadline=-1)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupt"):
            conn.execute(
                "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
                "SELECT count(*) FROM n"
            ).fetchone()
    finally:
        conn.close()


def test_session_keys_are_reduced_like_the_hooks(board: Workboard) -> None:
    board.card("dm", "running", ago(hours=1), session_key="agent:main:whatsapp:direct:+15550100")
    key = tasks(board)["dm"].session_key
    assert key is not None and "+1555" not in key and key.startswith("agent:main:whatsapp:direct:~")


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
