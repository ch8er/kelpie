"""Hermes and rule 6's holds: a pause read from Hermes' block text, and ``lift_hold``
clearing a forgotten pause through ``hermes kanban unblock`` (opt-in, the simulated CLI)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.adapter import MAX_LIFTS, HermesError
from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.adapters.hermes.stop import HermesCliError
from kelpie.app import ConfigError, act_blockers, build_adapters, load_settings
from kelpie.core.actions import Action, ActionKind
from kelpie.core.executors import PlatformExecutor
from kelpie.core.ports import NothingToDo, UnsupportedAction, lifts_holds
from kelpie.rules.bottlenecks import is_paused
from tests.adapters.kanban import Board
from tests.adapters.test_hermes_actions import NOW, argv, board, cli, comments, row  # noqa: F401


def paused(board: Board, task: str = "t1", reason: str = "paused by the lead") -> None:  # noqa: F811
    board.task(task, "blocked", NOW - timedelta(hours=9), assignee="dev-1")
    board.event(task, "blocked", NOW - timedelta(hours=8), {"kind": None, "reason": reason})


def make(board: Board, exe: str, *, lift: bool = True) -> HermesAdapter:  # noqa: F811
    a = HermesAdapter(
        db_path=board.path,
        spool=None,
        git_state=False,
        cli=exe,
        wait_key=board.path.parent / "wait.key",
        lift_holds=lift,
    )
    a.snapshot(NOW)  # the board the rule decided on
    return a


def lift(held: list[str], kind: str = "pause") -> Action:
    return Action(
        ActionKind.LIFT_HOLD,
        "bottlenecks",
        "Hold 'pause' is past its expected 4h: lift it.",
        executor="hermes",
        params={"hold": "pause", "hold_kind": kind, "lift_prs": [], "held_tasks": held},
    )


def test_an_untyped_paused_block_is_a_pause(board: Board, cli: tuple[str, Path]) -> None:  # noqa: F811
    paused(board)
    paused(board, "t2", "waiting for ci on #4")
    tasks = {t.id: t for t in make(board, cli[0]).snapshot(NOW).tasks}
    assert is_paused(tasks["t1"]) and not is_paused(tasks["t2"])
    assert tasks["t1"].flags == ()  # Hermes records no flags


def test_a_forgotten_pause_is_cleared_through_the_cli(
    board: Board,  # noqa: F811
    cli: tuple[str, Path],  # noqa: F811
) -> None:
    exe, log = cli
    paused(board)
    paused(board, "t2")
    a = make(board, exe)
    assert lifts_holds(a)
    done = PlatformExecutor(a).execute(lift(["t1", "t2", "example/repo#3"]))
    assert done == "cleared the blocks of t1, t2"
    assert row(board) == ("ready", "dev-1") and row(board, "t2")[0] == "ready"
    assert [c[:2] for c in argv(log)] == [["kanban", "unblock"]] * 2
    assert comments(board) == [
        ("kelpie", "UNBLOCK: Hold 'pause' is past its expected 4h: lift it.")
    ]


def test_lifting_is_off_by_default(board: Board, cli: tuple[str, Path]) -> None:  # noqa: F811
    exe, log = cli
    paused(board)
    a = make(board, exe, lift=False)
    assert not lifts_holds(a) and HermesAdapter().lift_holds is False
    with pytest.raises(UnsupportedAction, match="allow_lift_holds is off"):
        a.lift_hold("pause", "pause", ["t1"], "r")
    assert PlatformExecutor(a).execute(lift(["t1"])) is False  # a person is told
    assert argv(log) == [] and row(board)[0] == "blocked"


@pytest.mark.parametrize("kind", ["label", "flag"])
def test_only_pauses_are_lifted(board: Board, cli: tuple[str, Path], kind: str) -> None:  # noqa: F811
    paused(board)
    with pytest.raises(UnsupportedAction, match="pause holds only"):
        make(board, cli[0]).lift_hold("pause", kind, ["t1"], "r")
    assert argv(cli[1]) == []


def test_a_hold_on_no_task_of_this_board_is_refused(board: Board, cli: tuple[str, Path]) -> None:  # noqa: F811
    paused(board)
    with pytest.raises(UnsupportedAction, match="no task of hermes"):
        make(board, cli[0]).lift_hold("pause", "pause", ["example/repo#1"], "r")


def test_a_pause_lifted_meanwhile_is_nothing_to_do(board: Board, cli: tuple[str, Path]) -> None:  # noqa: F811
    exe, log = cli
    paused(board)
    a = make(board, exe)
    board.update("t1", status="ready")  # a person lifted it
    with pytest.raises(NothingToDo, match="blocked any more"):
        a.lift_hold("pause", "pause", ["t1"], "r")
    assert argv(log) == []


def test_a_task_no_longer_blocked_is_left_to_a_person(
    board: Board,  # noqa: F811
    cli: tuple[str, Path],  # noqa: F811
) -> None:
    exe, log = cli
    paused(board)
    board.task("t2", "ready", NOW - timedelta(hours=1))
    a = make(board, exe)
    with pytest.raises(UnsupportedAction, match="can be cleared here"):
        a.lift_hold("pause", "pause", ["t2"], "r")  # not blocked: a person decides
    assert a.lift_hold("pause", "pause", ["t1", "t2"], "r") == (
        "cleared the blocks of t1; t2 left to a person"
    )
    assert len(argv(log)) == 1


def test_kelpies_stop_and_the_circuit_breaker_are_never_cleared(
    board: Board,  # noqa: F811
    cli: tuple[str, Path],  # noqa: F811
) -> None:
    exe, log = cli
    # A hold whose pattern matches a stop's text: the stop still waits for a person.
    paused(board, "t1", "paused: loop [kelpie-stop:v1:123:0123456789abcdef." + "a" * 64 + "]")
    board.task("t2", "blocked", NOW - timedelta(hours=9), assignee="dev-1")
    board.event("t2", "gave_up", NOW - timedelta(hours=8), {"failures": 3, "error": "paused"})
    a = make(board, exe)
    with pytest.raises(UnsupportedAction, match="can be cleared here"):
        a.lift_hold("pause", "pause", ["t1", "t2"], "r")
    assert argv(log) == [] and row(board)[0] == "blocked" and row(board, "t2")[0] == "blocked"


def test_a_block_changed_since_the_snapshot_stops_the_lift(
    board: Board,  # noqa: F811
    cli: tuple[str, Path],  # noqa: F811
) -> None:
    exe, _ = cli
    for t in ("t1", "t2", "t3"):
        paused(board, t)
    a = make(board, exe)
    board.event("t1", "blocked", NOW, {"kind": None, "reason": "a real problem"})
    with pytest.raises(HermesError, match="an unblock failed") as caught:
        a.lift_hold("pause", "pause", ["t2", "t1", "t3"], "r")
    assert caught.value.public_reason == (
        "a block of hold 'pause' was not cleared; cleared the blocks of t2"
    )
    assert [row(board, t)[0] for t in ("t1", "t2", "t3")] == ["blocked", "ready", "blocked"]


def test_a_failing_cli_is_called_once_per_lift(
    board: Board,  # noqa: F811
    cli: tuple[str, Path],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def fail(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise HermesCliError("timed out")

    ids = [f"t{n}" for n in range(MAX_LIFTS * 3)]
    for t in ids:
        paused(board, t)
    a = make(board, cli[0])
    monkeypatch.setattr("kelpie.adapters.hermes.adapter.run_kanban", fail)
    with pytest.raises(HermesError, match="cleared no block"):
        a.lift_hold("pause", "pause", ids, "r")
    assert len(calls) == 1


def test_at_most_a_few_blocks_are_cleared_and_the_rest_are_named(
    board: Board,  # noqa: F811
    cli: tuple[str, Path],  # noqa: F811
) -> None:
    exe, log = cli
    ids = [f"t{n}" for n in range(MAX_LIFTS + 2)]
    for t in ids:
        paused(board, t)
    done = make(board, exe).lift_hold("pause", "pause", ids, "r")
    assert done == (
        f"cleared the blocks of {', '.join(ids[:MAX_LIFTS])}; "
        f"{', '.join(ids[MAX_LIFTS:])} left to a person"
    )
    assert len(argv(log)) == MAX_LIFTS


def test_named_boards_lift_their_own_tasks(board: Board, cli: tuple[str, Path]) -> None:  # noqa: F811
    exe, _ = cli
    paused(board)
    a = make(board, exe)
    a.board = "main"
    boards = HermesBoards(boards=(a,))
    assert lifts_holds(boards)
    assert boards.lift_hold("pause", "pause", ["main:t1", "other:t9", "x"], "r") == (
        "main: cleared the blocks of t1"
    )
    with pytest.raises(NothingToDo):
        boards.lift_hold("pause", "pause", ["main:t1"], "r")
    with pytest.raises(UnsupportedAction, match="no task of a watched Hermes board"):
        boards.lift_hold("pause", "pause", ["other:t1"], "r")
    with pytest.raises(UnsupportedAction, match="pause holds only"):
        boards.lift_hold("pause", "flag", ["main:t1"], "r")
    a.lift_holds = False
    with pytest.raises(UnsupportedAction, match="allow_lift_holds is off"):
        boards.lift_hold("pause", "pause", ["main:t1"], "r")


def test_config_and_the_act_gate(tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    db = tmp_path / "kanban.db"
    Board(db).close()
    base = f'[adapters.hermes]\ndb_path = "{db}"\nspool = false\nstate_home = false\n'
    config.write_text(base)
    settings = load_settings(config)
    (adapter,) = build_adapters(settings)
    assert isinstance(adapter, HermesAdapter) and adapter.lift_holds is False
    assert (
        "allow_lift_holds is off for Hermes"
        in act_blockers(settings, [adapter])["bottlenecks"].reason
    )
    config.write_text(base + "allow_lift_holds = true\n")
    settings = load_settings(config)
    (adapter,) = build_adapters(settings)
    assert adapter.lift_holds is True
    assert "bottlenecks" not in act_blockers(settings, [adapter])
    config.write_text(base + 'allow_lift_holds = "yes"\n')
    with pytest.raises(ConfigError, match="allow_lift_holds must be true or false"):
        load_settings(config)
