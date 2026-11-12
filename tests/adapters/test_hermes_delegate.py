"""Hermes ``delegate`` (#347): a card on the operator's queue through the simulated CLI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.adapter import HermesError, _created_id
from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import Mode
from kelpie.core.delegation import event_of
from kelpie.core.executors import PlatformExecutor
from kelpie.core.ledger import Dispatched, LedgerEntry, Outcome
from kelpie.core.ports import SupportsDelegate, UnsupportedAction, declared_actions
from kelpie.sim import hermes_cli
from tests.adapters.kanban import Board

NOW = datetime.now(UTC).replace(microsecond=0)
BODY = "Kelpie found a problem on task t1.\n\n> quoted\n\nkelpie-delegation: k-1"


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    b.task("t1", "blocked", NOW - timedelta(hours=3), assignee="dev-1")
    yield b
    b.close()


@pytest.fixture
def cli(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "calls.jsonl"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return str(hermes_cli.install(bindir, log)), log


def make(b: Board, cli: str, **kw: object) -> HermesAdapter:
    return HermesAdapter(
        db_path=b.path,
        spool=None,
        git_state=False,
        cli=cli,
        wait_key=b.path.parent / "wait.key",
        human_owners=("alice",),
        **kw,  # type: ignore[arg-type]
    )


def cards(board: Board) -> list[tuple[str, str, str | None, str]]:
    return board.conn.execute(
        "SELECT id, title, assignee, status FROM tasks WHERE id != 't1' ORDER BY id"
    ).fetchall()


def test_declared() -> None:
    kinds = declared_actions(HermesAdapter())
    assert kinds is not None and ActionKind.DELEGATE in kinds
    assert isinstance(HermesAdapter(), SupportsDelegate)
    assert HermesBoards.actions == kinds


def test_a_card_is_created_for_the_operator(board: Board, cli: tuple[str, Path]) -> None:
    a = make(board, cli[0])
    card = a.delegate("t1", "OPS-1", "Kelpie: stale_blocks on t1", BODY, "k-1")
    ((cid, title, assignee, status),) = cards(board)
    assert card == cid and title == "Kelpie: stale_blocks on t1"
    assert assignee == "ops-1" and status == "ready"
    body = board.conn.execute("SELECT body FROM tasks WHERE id = ?", (cid,)).fetchone()[0]
    assert body == BODY
    (call,) = [c for c in hermes_cli.calls(cli[1]) if "create" in c["argv"]]  # type: ignore[operator]
    assert "--idempotency-key=k-1" in call["argv"]  # type: ignore[operator]
    assert "--json" in call["argv"]  # type: ignore[operator]


def test_a_retried_create_returns_the_same_card(board: Board, cli: tuple[str, Path]) -> None:
    a = make(board, cli[0])
    first = a.delegate("t1", "ops-1", "T", BODY, "k-1")
    assert a.delegate("t1", "ops-1", "T", BODY, "k-1") == first
    assert len(cards(board)) == 1
    assert a.delegate("t1", "ops-1", "T", BODY, "k-2") != first


@pytest.mark.parametrize("who", ["alice", "kelpie", "none", "Bad Name", ""])
def test_not_an_agent_is_refused(board: Board, cli: tuple[str, Path], who: str) -> None:
    with pytest.raises(UnsupportedAction):
        make(board, cli[0]).delegate("t1", who, "T", BODY, "k-1")
    assert cards(board) == []


def test_an_unknown_task_is_an_error(board: Board, cli: tuple[str, Path]) -> None:
    with pytest.raises(HermesError, match="unknown"):
        make(board, cli[0]).delegate("t9", "ops-1", "T", BODY, "k-1")


def test_a_failing_cli_is_an_error(board: Board, cli: tuple[str, Path]) -> None:
    (board.path.parent / hermes_cli.FAIL).write_text("")
    with pytest.raises(HermesError):
        make(board, cli[0]).delegate("t1", "ops-1", "T", BODY, "k-1")


@pytest.mark.parametrize(
    ("out", "found"),
    [
        ('{"id": "t_1a"}', "t_1a"),
        ('warning: no gateway running\n{"task_id": "t_2"}', "t_2"),
        ('{"task": {"id": "t_3", "status": "ready"}}', "t_3"),
        ('{"id": "bad id"}', None),
        ('{"id": 5}', None),
        ("[1, 2]", None),
        ("created t_4", None),
        ("", None),
    ],
)
def test_the_card_id_is_read_from_json(out: str, found: str | None) -> None:
    assert _created_id(out) == found


def test_output_without_an_id_is_an_error(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.adapters.hermes.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "run_kanban", lambda *a, **k: "done\n")
    with pytest.raises(HermesError) as caught:
        make(board, cli[0]).delegate("t1", "ops-1", "T", BODY, "k-1")
    assert caught.value.public_reason == "the card's id could not be read from Hermes"


def test_the_executor_records_the_card_for_the_rule(board: Board, cli: tuple[str, Path]) -> None:
    params = {
        "problem": "stale:t1",
        "incident": "task:t1",
        "attempt": 1,
        "operator": "ops-1",
        "card": {"title": "T", "body": BODY, "key": "k-1"},
    }
    action = Action(
        ActionKind.DELEGATE, "delegation", "Handed to ops-1", task_id="t1", params=params
    )
    detail = PlatformExecutor(make(board, cli[0])).execute(action)
    ((cid, *_),) = cards(board)
    assert detail == f"card {cid} for ops-1"
    entry = LedgerEntry.of(Dispatched(action, Mode.ACT, Outcome.EXECUTED, detail=detail), NOW)
    event = event_of(entry)
    assert event is not None and event.card_id == cid


@pytest.mark.parametrize(
    "bad", [{}, {"card": "x"}, {"card": {"title": "T", "body": "", "key": "k"}}]
)
def test_the_executor_refuses_a_malformed_card(
    board: Board, cli: tuple[str, Path], bad: dict[str, object]
) -> None:
    action = Action(
        ActionKind.DELEGATE, "delegation", "r", task_id="t1", params={"operator": "ops-1", **bad}
    )
    assert PlatformExecutor(make(board, cli[0])).execute(action) is False
    assert cards(board) == []


def test_several_boards_name_the_card_on_its_board(board: Board, cli: tuple[str, Path]) -> None:
    boards = HermesBoards([make(board, cli[0], board=n) for n in ("alpha", "beta")])
    card = boards.delegate("alpha:t1", "ops-1", "T", BODY, "k-1")
    ((cid, *_),) = cards(board)
    assert card == f"alpha:{cid}"


def test_the_sim_cli_records_the_key(board: Board, cli: tuple[str, Path]) -> None:
    make(board, cli[0]).delegate("t1", "ops-1", "T", BODY, "k-7")
    (payload,) = board.conn.execute(
        "SELECT payload FROM task_events WHERE kind = 'created'"
    ).fetchone()
    assert json.loads(payload)["idempotency_key"] == "k-7"
