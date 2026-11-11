"""An answer from the chat on a Hermes board (#300): the adapter re-reads the task right
before writing and refuses when the question changed; written, the answer is a comment by
Kelpie quoting the agent's option, then an unblock of the block the press referred to."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.alerts.answers import AnswerPress
from kelpie.answering import AnswerBoards, AnswerRunner
from kelpie.core.actions import ActionKind
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.offers import Expect, Offer, find_offer, parse_offer, question_key
from kelpie.core.ports import StaleAnswer
from kelpie.core.storage import SqliteLedgerStore
from kelpie.sim import hermes_cli
from tests.adapters.kanban import Board

NOW = datetime.now(UTC).replace(microsecond=0)
BLOCKED_AT = NOW - timedelta(hours=1)
QUESTION = "Which schema?\nOptions:\nA) target schema v3\nB) keep v2 and add a shim"


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    b.task("t1", "blocked", NOW - timedelta(hours=3), assignee="dev-1")
    b.event("t1", "blocked", BLOCKED_AT, {"kind": "needs_input", "reason": "which schema?"})
    b.comment("t1", "dev-1", QUESTION, BLOCKED_AT - timedelta(minutes=1))
    yield b
    b.close()


@pytest.fixture
def cli(tmp_path: Path) -> str:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return str(hermes_cli.install(bindir, tmp_path / "calls.jsonl"))


def make(board: Board, cli: str) -> HermesAdapter:
    return HermesAdapter(
        db_path=board.path, spool=None, git_state=False, cli=cli,
        wait_key=board.path.parent / "wait.key", human_owners=("alice",),
        clock=lambda: NOW,
    )  # fmt: skip


def expect_of(adapter: HermesAdapter) -> tuple[Expect, Offer]:
    task = adapter.snapshot(NOW).task("t1")
    assert task is not None and task.block is not None
    found = find_offer(task)
    assert found is not None and found.offer is not None
    return Expect("dev-1", task.block.since, found.offer.digest), found.offer


def comments(board: Board) -> list[tuple[str, str]]:
    return board.conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = 't1' ORDER BY id"
    ).fetchall()


def test_the_offer_holds_on_the_board(board: Board, cli: str) -> None:
    adapter = make(board, cli)
    expect, _ = expect_of(adapter)
    adapter.check_answer("t1", expect)  # nothing changed


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda b: b.event("t1", "blocked", NOW, {"kind": None, "reason": "again"}), "again"),
        (lambda b: b.update("t1", status="ready"), "not blocked"),
        (lambda b: b.update("t1", assignee="dev-2"), "another agent"),
        (lambda b: b.comment("t1", "dev-1", QUESTION.replace("v3", "v4"), NOW), "options"),
        (lambda b: b.comment("t1", "alice", "go with A", NOW), "options"),
        (lambda b: b.conn.execute("DELETE FROM tasks") and b.conn.commit(), "gone"),
    ],
)
def test_a_changed_question_is_refused_before_any_write(
    board: Board, cli: str, change: object, why: str
) -> None:
    adapter = make(board, cli)
    expect, _ = expect_of(adapter)
    marks = dict(adapter._blocks)
    change(board)  # type: ignore[operator]
    with pytest.raises(StaleAnswer, match=why):
        adapter.check_answer("t1", expect)
    assert adapter._blocks == marks  # the re-read leaves the snapshot's state alone


def test_an_answer_is_commented_then_unblocked(board: Board, cli: str, tmp_path: Path) -> None:
    adapter = make(board, cli)
    expect, offer = expect_of(adapter)
    snapshot = adapter.snapshot(NOW)
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    runner = AnswerRunner(
        store, Engine(rules=[], adapters=[adapter]), AnswerBoards(frozenset({"hermes"})),
        False, lambda: NOW,
    )  # fmt: skip
    press = AnswerPress(
        channel="telegram", chat_id="-100", task_id="t1", board=None,
        question=question_key("t1", "dev-1", NOW, offer.digest), offer=offer, index=1,
        expect=expect, user_id=123456789, who="Telegram user 123456789", name="Alice",
        ref_id=1, message_id=2, digest_message_id=1, item=1,
    )  # fmt: skip
    runner.submit(press, NOW)
    runner.drain(snapshot)
    (status,) = board.conn.execute("SELECT status FROM tasks WHERE id = 't1'").fetchone()
    assert status != "blocked"
    author, body = comments(board)[1]
    assert author == "kelpie"
    assert body.startswith("Answer from a person (via Kelpie, Telegram user 123456789)")
    assert "    B) keep v2 and add a shim" in body
    (entry,) = [e for e in store.answers(NOW - timedelta(days=1)) if e.kind is ActionKind.ANSWER]
    assert entry.outcome is Outcome.EXECUTED and entry.params["unblock"] == "executed"


def test_several_boards_route_the_re_check(tmp_path: Path, cli: str) -> None:
    b = Board(tmp_path / "qa.db")
    b.task("t1", "blocked", NOW - timedelta(hours=3), assignee="dev-1")
    b.event("t1", "blocked", BLOCKED_AT, {"kind": "needs_input", "reason": "which schema?"})
    b.comment("t1", "dev-1", QUESTION, BLOCKED_AT)
    qa = make(b, cli)
    qa.board, qa.name = "qa", "hermes:qa"
    boards = HermesBoards([qa])
    task = boards.snapshot(NOW).task("qa:t1")
    assert task is not None and task.board == "qa" and task.block is not None
    found = find_offer(task)
    assert found is not None and found.offer is not None
    boards.check_answer("qa:t1", Expect("dev-1", task.block.since, found.offer.digest))
    with pytest.raises(StaleAnswer):
        boards.check_answer("qa:t1", Expect("dev-1", task.block.since, "0" * 32))
    b.close()


def test_the_offer_parser_reads_hermes_comments(board: Board, cli: str) -> None:
    _, offer = expect_of(make(board, cli))
    assert offer == parse_offer(QUESTION)


def test_an_unreadable_board_refuses_before_any_write(board: Board, cli: str) -> None:
    adapter = make(board, cli)
    expect, _ = expect_of(adapter)
    adapter.db_path = board.path.parent / "missing" / "kanban.db"
    with pytest.raises(StaleAnswer, match="could not be read"):
        adapter.check_answer("t1", expect)
