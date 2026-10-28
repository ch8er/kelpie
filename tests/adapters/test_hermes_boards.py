"""Several boards of one Hermes home as one adapter (``[[adapters.hermes.boards]]``, #198)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes import adapter as hermes_adapter
from kelpie.adapters.hermes.adapter import HermesError
from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.adapters.hermes.processes import HostTools, PoolUse, ToolProcesses, ToolRun
from kelpie.core.model import Condition, ConditionKind, OwnerKind, ResourceKind, Status
from kelpie.core.ports import PlatformAdapter, SnapshotSource, UnsupportedAction
from tests.adapters.kanban import Board
from tests.adapters.test_hermes_park import FAKE_CLI

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture
def boards(tmp_path: Path) -> Iterator[tuple[Board, Board]]:
    found = []
    for name in ("qr", "sign"):
        (tmp_path / name).mkdir()
        found.append(Board(tmp_path / name / "kanban.db"))
    qr, sign = found
    # Run ids restart on every board: both boards have a run 1.
    qr.task("t_q1", "running", ago(hours=2), assignee="dev-1", started=ago(hours=1))
    qr.task("t_q2", "todo", ago(hours=2), assignee="alice")
    qr.link("t_q2", "t_q1")
    rid = qr.run("t_q1", "dev-1", ago(hours=1), heartbeat=ago(hours=1))
    qr.update("t_q1", current_run_id=rid)
    sign.task("t_s1", "running", ago(hours=2), assignee="dev-1", started=ago(hours=1))
    rid = sign.run("t_s1", "dev-1", ago(hours=1), heartbeat=ago(hours=1))
    sign.update("t_s1", current_run_id=rid)
    yield qr, sign
    for b in found:
        b.close()


def make(qr: Board, sign: Board, spool: Path | None = None, **kw: object) -> HermesBoards:
    kids = [
        HermesAdapter(
            db_path=b.path,
            spool=None,
            git_state=False,
            human_owners=("alice",),
            board=name,
            name=f"hermes:{name}",
            wait_key=b.path.parent / "wait.key",
            **kw,  # type: ignore[arg-type]
        )
        for name, b in (("qr", qr), ("sign", sign))
    ]
    return HermesBoards(kids, spool=spool)


def test_it_is_a_platform_adapter(boards: tuple[Board, Board]) -> None:
    a = make(*boards)
    assert isinstance(a, PlatformAdapter) and isinstance(a, SnapshotSource)
    assert a.name == "hermes" and a.actions == HermesAdapter.actions
    with pytest.raises(ValueError, match="distinct names"):
        HermesBoards([a.boards[0], a.boards[0]])


def test_boards_merge_into_one_snapshot_with_board_ids(boards: tuple[Board, Board]) -> None:
    snap = make(*boards).snapshot(NOW)
    tasks = {t.id: t for t in snap.tasks}
    assert set(tasks) == {"qr:t_q1", "qr:t_q2", "sign:t_s1"}
    assert {t.board for t in snap.tasks} == {"qr", "sign"}
    assert {t.source for t in snap.tasks} == {"hermes"}
    assert tasks["qr:t_q1"].local_id == "t_q1"
    assert {(d.task_id, d.depends_on) for d in snap.dependencies} == {("qr:t_q1", "qr:t_q2")}
    sessions = {s.id: s for s in snap.sessions}
    assert set(sessions) == {"qr:hermes-run-1", "sign:hermes-run-1"}  # one run id, two boards
    assert sessions["sign:hermes-run-1"].task_id == "sign:t_s1"
    assert {s.source for s in snap.sessions} == {"hermes"}
    # A profile of the home is one agent on every board; a person stays a person.
    owners = {o.id: o for o in snap.owners}
    assert sorted(owners) == ["alice", "dev-1"] and owners["alice"].kind is OwnerKind.HUMAN
    assert snap.unobserved == ()


def line(task: str, run: int, at: datetime, **kw: object) -> str:
    rec = {"type": "tool", "at": at.timestamp(), "task_id": task, "run_id": str(run)}
    return json.dumps({**rec, "tool": "terminal", "args_hash": "abcd", "duration_ms": 0, **kw})


def test_a_shared_spool_goes_to_the_board_of_each_line(
    boards: tuple[Board, Board], tmp_path: Path
) -> None:
    spool = tmp_path / "spool.jsonl"
    spool.write_text(
        "\n".join(
            [
                line("t_q1", 1, ago(minutes=5), tool_call_id="c1"),
                line("t_s1", 1, ago(minutes=50), tool_call_id="c2"),
                line("t_s1", 7, ago(minutes=4), tool_call_id="c3"),  # no such run of t_s1
                line("t_x9", 1, ago(minutes=3), tool_call_id="c4"),  # on no board
                json.dumps({"type": "tool", "at": ago(minutes=2).timestamp(), "tool": "x"}),
            ]
        )
        + "\n"
    )
    snap = make(*boards, spool=spool).snapshot(NOW)
    tools = {
        s.attributes.get("gen_ai.tool.call.id"): s for s in snap.spans if s.kind.name == "TOOL"
    }
    assert set(tools) == {"c1", "c2"}
    assert (tools["c1"].task_id, tools["c1"].session_id) == ("qr:t_q1", "qr:hermes-run-1")
    assert (tools["c2"].task_id, tools["c2"].session_id) == ("sign:t_s1", "sign:hermes-run-1")
    assert all(s.task_id is not None for s in snap.spans)
    # Each run's heartbeat is its own board's line, never the other board's run 1.
    beats = {s.id: s.last_heartbeat for s in snap.sessions}
    assert beats == {"qr:hermes-run-1": ago(minutes=5), "sign:hermes-run-1": ago(minutes=50)}


def test_a_board_that_cannot_be_read_is_unobserved(
    boards: tuple[Board, Board], tmp_path: Path
) -> None:
    a = make(*boards)
    a.boards[1].db_path = tmp_path / "gone.db"
    snap = a.snapshot(NOW)
    assert {t.board for t in snap.tasks} == {"qr"} and snap.unobserved == ("sign",)
    assert a.check() is not None and "sign:" in (a.check() or "")
    a.boards[0].db_path = tmp_path / "gone.db"
    with pytest.raises(HermesError, match="no Hermes board can be read"):
        a.snapshot(NOW)


def test_status_lists_every_board_and_the_spool(boards: tuple[Board, Board]) -> None:
    a = make(*boards)
    assert a.check() is None
    lines = a.status_detail(NOW)
    assert lines[0].startswith("board qr: ") and lines[1].startswith("board sign: ")
    assert lines[-1] == "spool: disabled"


@pytest.fixture
def cli(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "cli.jsonl"
    exe = tmp_path / "bin" / "hermes"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLI.format(python=sys.executable, log=str(log)))
    exe.chmod(0o755)
    return str(exe), log


def test_park_and_wake_go_to_the_tasks_board(
    boards: tuple[Board, Board], cli: tuple[str, Path]
) -> None:
    qr, sign = boards
    a = make(qr, sign, cli=cli[0])
    a.snapshot(datetime.now(UTC))  # the statuses a park is checked against
    gate = Condition(
        "dispatch_gate:qr:t_q1:qr:t_q2",
        ConditionKind.TASK_STATUS,
        "qr:t_q1",
        {"task": "qr:t_q2", "status": "done"},
    )
    a.wait("qr:t_q1", [gate], "waits for t_q2")
    calls = [json.loads(x) for x in cli[1].read_text().splitlines()]
    assert calls[-1]["env"]["HERMES_KANBAN_DB"] == str(qr.path)
    assert "schedule" in calls[-1]["argv"] and calls[-1]["argv"][-2] == "t_q1"
    snap = a.snapshot(datetime.now(UTC))
    parked = next(t for t in snap.tasks if t.id == "qr:t_q1")
    assert parked.status is Status.WAITING
    (cond,) = snap.conditions
    assert (cond.id, cond.task_id, cond.params["task"]) == (gate.id, "qr:t_q1", "qr:t_q2")
    a.requeue("qr:t_q1", "t_q2 is done")
    assert qr.conn.execute("SELECT status FROM tasks WHERE id='t_q1'").fetchone()[0] == "ready"
    assert sign.conn.execute("SELECT status FROM tasks WHERE id='t_s1'").fetchone()[0] == "running"


def test_actions_without_a_known_board_or_across_boards_are_refused(
    boards: tuple[Board, Board], cli: tuple[str, Path]
) -> None:
    a = make(*boards, cli=cli[0])
    a.snapshot(datetime.now(UTC))
    for call in (
        lambda: a.comment("t_q1", "x"),
        lambda: a.comment("other:t_q1", "x"),
        lambda: a.stop("hermes-run-1", "loop"),
        lambda: a.requeue("qr:", "x"),
    ):
        with pytest.raises(UnsupportedAction):
            call()
    across = Condition(
        "dispatch_gate:qr:t_q1:sign:t_s1",
        ConditionKind.TASK_STATUS,
        "qr:t_q1",
        {"task": "sign:t_s1", "status": "done"},
    )
    with pytest.raises(UnsupportedAction, match="not a task of this board"):
        a.wait("qr:t_q1", [across], "waits for another board")
    assert not cli[1].exists()  # nothing reached Hermes


def test_stop_and_comment_reach_the_board_in_the_prefix(
    boards: tuple[Board, Board], monkeypatch: pytest.MonkeyPatch
) -> None:
    a = make(*boards)
    seen: list[tuple[str | None, str]] = []
    monkeypatch.setattr(HermesAdapter, "stop", lambda k, sid, why: seen.append((k.board, sid)))
    monkeypatch.setattr(HermesAdapter, "comment", lambda k, tid, b: seen.append((k.board, tid)))
    a.stop("sign:hermes-run-1", "loop")
    a.comment("qr:t_q2", "note")
    assert seen == [("sign", "hermes-run-1"), ("qr", "t_q2")]


def test_tool_processes_are_read_once_over_every_boards_tasks(
    boards: tuple[Board, Board], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    qr, sign = boards
    for b, tid in ((qr, "t_q1"), (sign, "t_s1")):
        (tmp_path / tid).mkdir()
        b.update(tid, workspace_kind="worktree", workspace_path=str(tmp_path / tid))
    reads: list[dict[str, object]] = []

    def host(config, now, tasks, workspaces, aliases=None):  # type: ignore[no-untyped-def]
        reads.append({"tasks": set(tasks), "workspaces": dict(workspaces)})
        return HostTools(
            {"sign:t_s1": ToolRun("xcodebuild", ago(minutes=3))}, {"sim": PoolUse(2, 1)}
        )

    monkeypatch.setattr(hermes_adapter, "read_host", host)
    a = make(qr, sign)
    a.host = HermesAdapter(db_path=qr.path, spool=None, tools=ToolProcesses())
    snap = a.snapshot(NOW)
    assert len(reads) == 1 and reads[0]["tasks"] == {"qr:t_q1", "qr:t_q2", "sign:t_s1"}
    assert reads[0]["workspaces"] == {
        os.path.realpath(tmp_path / "t_q1"): "qr:t_q1",
        os.path.realpath(tmp_path / "t_s1"): "sign:t_s1",
    }
    running = {s.id: s.running_tool for s in snap.sessions}
    assert running == {"qr:hermes-run-1": None, "sign:hermes-run-1": "xcodebuild"}
    slots = sorted(r.name for r in snap.resources if r.kind is ResourceKind.SLOTS)
    assert slots == ["sim:agent", "sim:ci"]  # once for the host, not once per board


def test_a_workspace_pattern_names_a_task_by_its_id_on_its_board() -> None:
    tools = ToolProcesses(workspace_patterns=("work/{task}",))
    aliases = {"t_q1": "qr:t_q1", "t_3": None}
    tasks = {"qr:t_q1", "sign:t_3", "qr:t_3"}
    assert tools.task_of("/home/a/work/t_q1/src", tasks, {}, aliases) == "qr:t_q1"
    assert tools.task_of("/home/a/work/t_3", tasks, {}, aliases) is None  # on two boards
    assert tools.task_of("/home/a/work/t_q1", {"t_q1"}, {}) == "t_q1"  # one board: as before


def test_a_spool_line_two_boards_claim_goes_to_neither(
    boards: tuple[Board, Board], tmp_path: Path
) -> None:
    qr, sign = boards
    sign.task("t_q1", "running", ago(hours=2), assignee="dev-2")  # the same id on both boards
    sign.run("t_q1", "dev-2", ago(hours=1))  # run 2 on sign
    qr.run("t_q1", "dev-1", ago(hours=1))  # run 2 on qr too
    spool = tmp_path / "spool.jsonl"
    spool.write_text(line("t_q1", 2, ago(minutes=5), tool_call_id="c1") + "\n")
    snap = make(qr, sign, spool=spool).snapshot(NOW)
    assert snap.spans == ()
