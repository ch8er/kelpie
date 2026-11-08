"""Hermes ``unblock`` and ``reassign``, carried out through the simulated Hermes CLI
(:mod:`kelpie.sim.hermes_cli`) on a synthetic board."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.adapter import HermesError
from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.adapters.hermes.stop import HermesCliError, stop_comment
from kelpie.core.actions import Action, ActionKind
from kelpie.core.executors import PlatformExecutor
from kelpie.core.ports import (
    NothingToDo,
    SupportsReassign,
    UnsupportedAction,
    declared_actions,
)
from kelpie.sim import hermes_cli
from tests.adapters.kanban import Board

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


@pytest.fixture
def cli(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "calls.jsonl"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return str(hermes_cli.install(bindir, log)), log


def make(board: Board, cli: str) -> HermesAdapter:
    a = HermesAdapter(
        db_path=board.path,
        spool=None,
        git_state=False,
        cli=cli,
        wait_key=board.path.parent / "wait.key",
        human_owners=("alice",),
    )
    a.snapshot(NOW)  # the board the rule decided on
    return a


def blocked(board: Board, task: str = "t1", reason: str = "waiting for ci on #4") -> None:
    board.task(task, "blocked", NOW - timedelta(hours=3), assignee="dev-1")
    board.event(task, "blocked", NOW - timedelta(hours=1), {"kind": None, "reason": reason})


def row(board: Board, task: str = "t1") -> tuple[str, str | None]:
    status, assignee = board.conn.execute(
        "SELECT status, assignee FROM tasks WHERE id = ?", (task,)
    ).fetchone()
    return status, assignee


def comments(board: Board, task: str = "t1") -> list[tuple[str, str]]:
    return board.conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ? ORDER BY id", (task,)
    ).fetchall()


def argv(log: Path) -> list[list[str]]:
    return [list(c["argv"]) for c in hermes_cli.calls(log)]  # type: ignore[call-overload]


# --- declared capabilities ---------------------------------------------------------------


def test_unblock_and_reassign_are_declared_and_wip_commit_is_not() -> None:
    kinds = declared_actions(HermesAdapter())
    assert kinds is not None
    assert {ActionKind.UNBLOCK, ActionKind.REASSIGN} <= kinds
    assert ActionKind.WIP_COMMIT not in kinds
    assert isinstance(HermesAdapter(), SupportsReassign)
    assert HermesBoards.actions == kinds


# --- unblock -----------------------------------------------------------------------------


def test_a_block_the_snapshot_showed_is_cleared_with_a_signed_reason(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    blocked(board)
    make(board, exe).unblock("t1", "Stale: PR https://github.com/example/repo/pull/4 merged")
    assert row(board) == ("ready", "dev-1")
    (call,) = argv(log)
    assert call[:2] == ["kanban", "unblock"] and call[-2:] == ["--", "t1"]
    assert comments(board) == [("kelpie", "UNBLOCK: Stale: PR example/repo#4 merged")]


def test_unblock_through_the_executor(board: Board, cli: tuple[str, Path]) -> None:
    exe, _ = cli
    blocked(board)
    executor = PlatformExecutor(make(board, exe))
    assert executor.execute(Action(ActionKind.UNBLOCK, "stale_blocks", "met", task_id="t1"))
    assert row(board)[0] == "ready"


def test_a_task_unblocked_since_the_snapshot_is_left_alone(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    blocked(board)
    a = make(board, exe)
    board.update("t1", status="ready")  # a person unblocked it in the dashboard
    with pytest.raises(NothingToDo, match="no longer blocked"):
        a.unblock("t1", "met")
    assert argv(log) == []


def test_a_task_blocked_again_since_the_snapshot_is_not_unblocked(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    blocked(board)
    a = make(board, exe)
    board.event("t1", "blocked", NOW, {"kind": None, "reason": "a new problem"})
    with pytest.raises(HermesError, match="blocked again"):
        a.unblock("t1", "met")
    assert argv(log) == [] and row(board)[0] == "blocked"


def test_a_block_missing_from_the_snapshot_is_not_unblocked(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=3), assignee="dev-1")
    a = make(board, exe)
    blocked(board, "t2")
    board.update("t1", status="blocked")
    board.event("t1", "blocked", NOW, {"reason": "blocked after the snapshot"})
    with pytest.raises(HermesError, match="not blocked in the last snapshot"):
        a.unblock("t1", "met")
    with pytest.raises(HermesError, match="not blocked in the last snapshot"):
        a.unblock("t2", "met")
    with pytest.raises(HermesError, match="unknown"):
        a.unblock("t9", "met")
    assert argv(log) == []


@pytest.mark.parametrize("status", ["ready", "scheduled", "review", "done", "running"])
def test_only_blocked_tasks_are_unblocked(board: Board, cli: tuple[str, Path], status: str) -> None:
    exe, log = cli
    board.task("t1", status, NOW - timedelta(hours=3), assignee="dev-1")
    with pytest.raises(UnsupportedAction):
        make(board, exe).unblock("t1", "met")
    assert argv(log) == []


@pytest.mark.parametrize("signed", [True, False])
def test_kelpies_own_stop_is_never_unblocked(
    board: Board, cli: tuple[str, Path], tmp_path: Path, signed: bool
) -> None:
    exe, log = cli
    a = make(board, exe)
    key = a._key(create=True) if signed else b"x" * 32  # unsigned: a forged or stale key
    reason = stop_comment("t1", "Runaway run.", key=key, now=NOW - timedelta(hours=1))
    blocked(board, reason=reason)
    a.snapshot(NOW)
    with pytest.raises(UnsupportedAction, match="stopped by Kelpie"):
        a.unblock("t1", "met")
    assert argv(log) == [] and row(board)[0] == "blocked"


def test_a_refused_unblock_is_an_error(board: Board, cli: tuple[str, Path]) -> None:
    exe, _ = cli
    blocked(board)
    a = make(board, exe)
    (board.path.parent / hermes_cli.FAIL).write_text("")
    with pytest.raises(HermesError, match="exit 1"):
        a.unblock("t1", "met")
    assert row(board)[0] == "blocked"


def test_an_unblock_that_failed_after_a_person_unblocked_is_nothing_to_do(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, _ = cli
    blocked(board)
    a = make(board, exe)

    def unblocked_but_failed(*args: object, **kw: object) -> None:
        board.update("t1", status="ready")
        raise HermesCliError("hermes kanban unblock failed (exit 1)")

    monkeypatch.setattr("kelpie.adapters.hermes.adapter.run_kanban", unblocked_but_failed)
    with pytest.raises(NothingToDo, match="unblocked meanwhile"):
        a.unblock("t1", "met")


def test_an_unblock_that_did_not_take_is_an_error(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, _ = cli
    blocked(board)
    a = make(board, exe)
    monkeypatch.setattr("kelpie.adapters.hermes.adapter.run_kanban", lambda *a, **k: None)
    with pytest.raises(HermesError, match="still blocked"):
        a.unblock("t1", "met")


def test_unblock_on_a_named_board_goes_to_that_board(
    board: Board, cli: tuple[str, Path], tmp_path: Path
) -> None:
    exe, _ = cli
    blocked(board)
    a = make(board, exe)
    a.board = "main"
    boards = HermesBoards(boards=(a,))
    boards.unblock("main:t1", "met")
    assert row(board)[0] == "ready"
    with pytest.raises(UnsupportedAction):
        boards.unblock("other:t1", "met")


# --- reassign ----------------------------------------------------------------------------


def test_a_review_task_nobody_runs_is_handed_to_the_proposed_agent(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "review", NOW - timedelta(hours=4), assignee="reviewer-1")
    make(board, exe).reassign("t1", "Reviewer-2", "No owner for 3.0h; reviewer-2 is idle.")
    assert row(board) == ("review", "reviewer-2")
    assert argv(log)[0] == ["kanban", "assign", "--", "t1", "reviewer-2"]
    assert comments(board) == [
        (
            "kelpie",
            "Kelpie assigned this task to reviewer-2: No owner for 3.0h; reviewer-2 is idle.",
        )
    ]


def test_reassign_through_the_executor(board: Board, cli: tuple[str, Path]) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=4), assignee="dev-1")
    action = Action(
        ActionKind.REASSIGN, "orphan_status", "why", task_id="t1", params={"owner_id": "dev-2"}
    )
    assert PlatformExecutor(make(board, exe)).execute(action)
    assert row(board) == ("ready", "dev-2")


def test_reassign_to_the_current_assignee_is_a_no_op(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "todo", NOW - timedelta(hours=4), assignee="dev-2")
    with pytest.raises(NothingToDo, match="already assigned to dev-2"):
        make(board, exe).reassign("t1", "DEV-2", "again")
    assert argv(log) == [] and comments(board) == []


def test_a_running_task_is_not_reassigned(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "running", NOW - timedelta(hours=4), assignee="dev-1")
    rid = board.run("t1", "dev-1", NOW - timedelta(hours=1))
    board.update("t1", current_run_id=rid)
    with pytest.raises(UnsupportedAction, match="no run claims"):
        make(board, exe).reassign("t1", "dev-2", "orphan")
    assert argv(log) == [] and row(board) == ("running", "dev-1")


@pytest.mark.parametrize("status", ["blocked", "scheduled", "done", "triage", "running"])
def test_other_statuses_are_not_reassigned(
    board: Board, cli: tuple[str, Path], status: str
) -> None:
    exe, log = cli
    board.task("t1", status, NOW - timedelta(hours=4), assignee="dev-1")
    with pytest.raises(UnsupportedAction):
        make(board, exe).reassign("t1", "dev-2", "orphan")
    assert argv(log) == []


@pytest.mark.parametrize(
    "owner", ["alice", "kelpie", "none", "-", "", "--force", "dev 2", "a/b", "dev.2"]
)
def test_people_kelpie_and_non_profile_names_are_refused(
    board: Board, cli: tuple[str, Path], owner: str
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=4), assignee="dev-1")
    with pytest.raises(UnsupportedAction):
        make(board, exe).reassign("t1", owner, "orphan")
    assert argv(log) == []


def test_a_task_that_moved_since_the_snapshot_is_not_reassigned(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=4), assignee="dev-1")
    a = make(board, exe)
    rid = board.run("t1", "dev-1", NOW)
    board.update("t1", status="running", current_run_id=rid)  # dispatched meanwhile
    with pytest.raises(HermesError, match="moved since the snapshot"):
        a.reassign("t1", "dev-2", "orphan")
    board.task("t2", "ready", NOW - timedelta(hours=4), assignee="dev-1")
    with pytest.raises(HermesError, match="not in the last snapshot"):
        a.reassign("t2", "dev-2", "orphan")
    with pytest.raises(HermesError, match="unknown"):
        a.reassign("t9", "dev-2", "orphan")
    assert argv(log) == []


def test_a_refused_assign_is_an_error_and_a_failed_comment_is_not(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=4), assignee="dev-1")
    board.task("t2", "ready", NOW - timedelta(hours=4), assignee="dev-1")
    a = make(board, exe)
    fail = board.path.parent / hermes_cli.FAIL
    fail.write_text("")
    with pytest.raises(HermesError, match="exit 1"):
        a.reassign("t1", "dev-2", "orphan")
    assert row(board) == ("ready", "dev-1")
    fail.unlink()

    def no_comment(*args: object, **kw: object) -> None:
        raise HermesError("comment refused")

    monkeypatch.setattr(HermesAdapter, "comment", no_comment)
    a.reassign("t2", "dev-2", "orphan")  # the assignment stands
    assert row(board, "t2") == ("ready", "dev-2")


def test_an_assign_that_did_not_take_is_an_error(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=4), assignee="dev-1")
    a = make(board, exe)
    monkeypatch.setattr("kelpie.adapters.hermes.adapter.run_kanban", lambda *a, **k: None)
    with pytest.raises(HermesError, match="not assigned to dev-2"):
        a.reassign("t1", "dev-2", "orphan")


def test_reassign_on_a_named_board(board: Board, cli: tuple[str, Path]) -> None:
    exe, _ = cli
    board.task("t1", "review", NOW - timedelta(hours=4), assignee="reviewer-1")
    a = make(board, exe)
    a.board = "main"
    boards = HermesBoards(boards=(a,))
    boards.reassign("main:t1", "reviewer-2", "orphan")
    assert row(board) == ("review", "reviewer-2")


def test_an_owner_picked_since_the_snapshot_is_not_overridden(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "review", NOW - timedelta(hours=4), assignee=None)
    a = make(board, exe)
    board.update("t1", assignee="reviewer-3")  # a person picked a reviewer meanwhile
    with pytest.raises(HermesError, match="assigned to reviewer-3 since the snapshot"):
        a.reassign("t1", "reviewer-2", "orphan")
    assert argv(log) == [] and row(board) == ("review", "reviewer-3")


def test_a_circuit_breaker_park_is_not_unblocked(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "blocked", NOW - timedelta(hours=3), assignee="dev-1", failures=3)
    board.event("t1", "blocked", NOW - timedelta(hours=2), {"reason": "an older block"})
    board.event("t1", "unblocked", NOW - timedelta(hours=1, minutes=30))
    board.event("t1", "gave_up", NOW - timedelta(hours=1), {"failures": 3, "error": "ci #4"})
    a = make(board, exe)
    with pytest.raises(UnsupportedAction, match="circuit breaker"):
        a.unblock("t1", "met")
    assert argv(log) == [] and row(board)[0] == "blocked"
