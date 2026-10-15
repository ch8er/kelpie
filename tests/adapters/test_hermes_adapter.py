from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.adapter import HermesError
from kelpie.core.model import OwnerKind, SpanKind, Status
from kelpie.core.ports import PlatformAdapter, SnapshotSource
from tests.adapters.kanban import Board

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


def adapter(board: Board, spool: Path | None = None, **kw: timedelta) -> HermesAdapter:
    return HermesAdapter(db_path=board.path, spool=spool, **kw)


def test_implements_ports(board: Board) -> None:
    a = adapter(board)
    assert isinstance(a, PlatformAdapter)
    assert isinstance(a, SnapshotSource)


@pytest.mark.parametrize(
    ("hermes", "expected"),
    [
        ("triage", Status.BACKLOG),
        ("todo", Status.BACKLOG),
        ("scheduled", Status.BACKLOG),
        ("ready", Status.READY),
        ("running", Status.IN_PROGRESS),
        ("blocked", Status.BLOCKED),
        ("review", Status.REVIEW),
        ("done", Status.DONE),
    ],
)
def test_status_mapping(board: Board, hermes: str, expected: Status) -> None:
    board.task("t_1", hermes, ago(days=1), assignee="dev")
    task = adapter(board).snapshot(NOW).tasks[0]
    assert task.status is expected
    assert task.owner_id == "dev" and task.role == "dev"
    assert task.source == "hermes"
    assert (task.block is not None) == (expected is Status.BLOCKED)


def test_archived_and_unknown_status_skipped(board: Board) -> None:
    board.task("t_1", "archived", ago(days=1))
    board.task("t_2", "weird", ago(days=1))
    board.task("t_3", "ready", ago(days=1))
    assert [t.id for t in adapter(board).snapshot(NOW).tasks] == ["t_3"]


def test_unassigned_task_has_no_owner(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="")
    snap = adapter(board).snapshot(NOW)
    assert snap.tasks[0].owner_id is None and snap.owners == ()


def test_status_since_prefers_latest_status_event(board: Board) -> None:
    board.task("t_1", "running", ago(days=2), assignee="dev", started=ago(hours=5))
    board.event("t_1", "created", ago(days=2))
    board.event("t_1", "claimed", ago(hours=3))
    board.event("t_1", "commented", ago(minutes=1))  # not a status change
    task = adapter(board).snapshot(NOW).tasks[0]
    assert task.status_since == ago(hours=3)
    assert task.status_since.tzinfo is not None


def test_status_since_fallbacks(board: Board) -> None:
    board.task("t_run", "running", ago(days=2), started=ago(hours=5))
    board.task("t_done", "done", ago(days=2), completed=ago(hours=1))
    board.task("t_new", "ready", ago(days=2))
    since = {t.id: t.status_since for t in adapter(board).snapshot(NOW).tasks}
    assert since == {"t_run": ago(hours=5), "t_done": ago(hours=1), "t_new": ago(days=2)}


def test_block_reason_and_since_from_event(board: Board) -> None:
    board.task("t_1", "blocked", ago(days=1), assignee="dev", block_kind="needs_input")
    board.event("t_1", "claimed", ago(hours=4))
    board.event(
        "t_1", "blocked", ago(hours=2), {"kind": "needs_input", "reason": "which API version?"}
    )
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None
    assert block.reason == "needs_input: which API version?"
    assert block.since == ago(hours=2)


def test_block_without_event_uses_kind(board: Board) -> None:
    board.task("t_1", "blocked", ago(hours=6), block_kind="capability")
    board.task("t_2", "blocked", ago(hours=6))
    board.event("t_3", "blocked", ago(hours=1), {"reason": "initial_status"})  # no such task
    blocks = {t.id: t.block for t in adapter(board).snapshot(NOW).tasks}
    assert blocks["t_1"] is not None and blocks["t_1"].reason == "capability"
    assert blocks["t_1"].since == ago(hours=6)
    assert blocks["t_2"] is not None and blocks["t_2"].reason == ""


def test_stale_block_event_is_ignored(board: Board) -> None:
    # An older block event, then a later status change (e.g. a circuit breaker parked it).
    board.task("t_1", "blocked", ago(days=1))
    board.event("t_1", "blocked", ago(hours=5), {"kind": "transient", "reason": "old"})
    board.event("t_1", "crashed", ago(hours=1))
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None and block.reason == "" and block.since == ago(hours=1)


def test_block_payload_garbage(board: Board) -> None:
    board.task("t_1", "blocked", ago(days=1))
    board.conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        ("t_1", "blocked", "{not json", int(ago(hours=1).timestamp())),
    )
    board.conn.commit()
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None and block.reason == "" and block.since == ago(hours=1)


def test_reason_only_block(board: Board) -> None:
    board.task("t_1", "blocked", ago(days=1))
    board.event("t_1", "blocked", ago(hours=1), {"reason": "waiting_ci"})
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None and block.reason == "waiting_ci"


def test_dependencies(board: Board) -> None:
    board.task("t_1", "done", ago(days=1))
    board.task("t_2", "todo", ago(days=1))
    board.task("t_3", "archived", ago(days=1))
    board.link("t_1", "t_2")
    board.link("t_3", "t_2")  # archived parent: satisfied, dropped
    deps = adapter(board).snapshot(NOW).dependencies
    assert [(d.task_id, d.depends_on) for d in deps] == [("t_2", "t_1")]


def test_owners_from_assignees_and_run_profiles(board: Board) -> None:
    board.task("t_1", "review", ago(days=1), assignee="reviewer")
    board.run("t_1", "dev", ago(hours=3), status="done", ended=ago(hours=2), outcome="completed")
    owners = adapter(board).snapshot(NOW).owners
    assert [(o.id, o.role, o.kind) for o in owners] == [
        ("dev", "dev", OwnerKind.AGENT),
        ("reviewer", "reviewer", OwnerKind.AGENT),
    ]


@pytest.mark.parametrize(
    ("outcome", "error", "final"),
    [
        ("completed", None, Status.DONE),
        ("review_requested", None, Status.REVIEW),
        ("blocked", None, Status.BLOCKED),
        ("crashed", "worker pid gone", None),
        ("timed_out", None, None),
        ("spawn_failed", "no such profile", None),
        ("gave_up", None, None),
        ("reclaimed", None, None),
        (None, None, None),
    ],
)
def test_session_final_status(
    board: Board, outcome: str | None, error: str | None, final: Status | None
) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    rid = board.run(
        "t_1",
        "dev",
        ago(hours=3),
        status="crashed",
        ended=ago(hours=1),
        heartbeat=ago(hours=2),
        outcome=outcome,
        error=error,
    )
    (s,) = adapter(board).snapshot(NOW).sessions
    assert s.id == f"hermes-run-{rid}" and s.task_id == "t_1" and s.owner_id == "dev"
    assert s.final_status is final
    assert s.started_at == ago(hours=3) and s.ended_at == ago(hours=1)
    assert s.last_heartbeat == ago(hours=2)
    expected_reason = " ".join(x for x in (outcome, error) if x) or None
    assert s.exit_reason == expected_reason


def test_open_run_is_a_live_session(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    board.run("t_1", "dev", ago(days=3), heartbeat=ago(minutes=2))
    (s,) = adapter(board).snapshot(NOW).sessions
    assert s.ended_at is None and s.final_status is None and s.last_heartbeat == ago(minutes=2)


def test_lookback_filters_old_runs_but_keeps_open_ones(board: Board) -> None:
    board.task("t_1", "ready", ago(days=9))
    old = board.run("t_1", "dev", ago(days=3), ended=ago(days=2), outcome="crashed")
    recent = board.run("t_1", "dev", ago(hours=3), ended=ago(hours=2), outcome="crashed")
    open_ = board.run("t_1", "dev", ago(days=5))
    ids = {s.id for s in adapter(board).snapshot(NOW).sessions}
    assert ids == {f"hermes-run-{recent}", f"hermes-run-{open_}"}
    assert f"hermes-run-{old}" not in ids
    wide = adapter(board, lookback=timedelta(days=7)).snapshot(NOW).sessions
    assert len(wide) == 3


def test_runs_of_archived_tasks_skipped(board: Board) -> None:
    board.task("t_1", "archived", ago(days=1))
    board.run("t_1", "dev", ago(hours=1))
    assert adapter(board).snapshot(NOW).sessions == ()


def test_run_without_profile_falls_back_to_assignee(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    board.run("t_1", None, ago(hours=1))
    board.task("t_2", "running", ago(days=1))
    board.run("t_2", "", ago(hours=1))
    owners = {s.task_id: s.owner_id for s in adapter(board).snapshot(NOW).sessions}
    assert owners == {"t_1": "dev", "t_2": "unknown"}


def test_never_writes(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1))
    before = board.path.read_bytes()
    a = adapter(board)
    a.snapshot(NOW)
    assert board.path.read_bytes() == before
    # The connection is read-only even though the file is writable.
    with a._connect() as conn, pytest.raises(sqlite3.OperationalError, match=r"readonly|read-only"):
        conn.execute("DELETE FROM tasks")
    assert board.conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_missing_db_is_a_clear_error(tmp_path: Path) -> None:
    a = HermesAdapter(db_path=tmp_path / "nope.db", spool=None)
    with pytest.raises(HermesError, match="not found"):
        a.snapshot(NOW)
    problem = a.check()
    assert problem is not None and "not found" in problem


def test_not_a_database_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "kanban.db"
    path.write_text("this is not sqlite")
    a = HermesAdapter(db_path=path, spool=None)
    with pytest.raises(HermesError, match="cannot read"):
        a.snapshot(NOW)
    assert a.check() is not None


def test_check_ok(board: Board) -> None:
    assert adapter(board).check() is None


def test_naive_now_rejected(board: Board) -> None:
    with pytest.raises(ValueError, match="timezone"):
        adapter(board).snapshot(datetime(2026, 3, 2))


def test_tilde_is_expanded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    a = HermesAdapter(db_path=Path("~/k.db"), spool=Path("~/s.jsonl"))
    assert a.db_path == tmp_path / "k.db" and a.spool == tmp_path / "s.jsonl"


def test_list_tasks_and_get_task(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1))
    a = adapter(board)
    assert [t.id for t in a.list_tasks()] == ["t_1"]
    assert a.get_task("t_1") is not None and a.get_task("t_9") is None
    assert list(a.events(NOW)) == []


def test_actions_not_implemented(board: Board) -> None:
    a = adapter(board)
    calls = (
        lambda: a.requeue("t_1", "r"),
        lambda: a.unblock("t_1", "r"),
        lambda: a.comment("t_1", "b"),
        lambda: a.wip_commit("t_1", "m"),
    )
    for call in calls:
        with pytest.raises(NotImplementedError, match="observe or suggest"):
            call()


def tool_line(at: datetime, **kw: object) -> str:
    rec = {
        "type": "tool",
        "at": at.timestamp(),
        "task_id": "t_1",
        "run_id": "7",
        "session_id": "s",
        "tool": "terminal",
        "args_hash": "abcd",
        "command": "ls",
        "outcome": "ok",
        "duration_ms": 1500,
    }
    return json.dumps({**rec, **kw})


def test_spans_from_spool(board: Board, tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    spool.write_text(
        "\n".join(
            [
                tool_line(ago(minutes=5)),
                "not json",
                "[1, 2]",
                json.dumps({"type": "session_end", "at": ago(minutes=1).timestamp()}),
                json.dumps({"type": "tool", "at": "garbage"}),
                json.dumps({"type": "tool"}),
                tool_line(ago(days=3)),  # outside the lookback
                tool_line(ago(minutes=2), run_id=None, task_id=None, tool=None, command=None),
                "",
            ]
        )
    )
    spans = adapter(board, spool).snapshot(NOW).spans
    assert len(spans) == 2
    s = spans[0]
    assert s.kind is SpanKind.TOOL and s.tool == "terminal" and s.outcome == "ok"
    assert s.session_id == "hermes-run-7" and s.task_id == "t_1" and s.trace_id == "hermes-run-7"
    assert s.args_hash == "abcd" and s.attributes == {"command": "ls"}
    assert s.end == ago(minutes=5) and s.duration == timedelta(milliseconds=1500)
    bare = spans[1]
    assert bare.session_id is None and bare.task_id is None and bare.tool is None
    assert bare.attributes == {} and bare.trace_id == "hermes-task-none"
    assert s.span_id != bare.span_id


def test_spool_missing_or_disabled(board: Board, tmp_path: Path) -> None:
    assert adapter(board, tmp_path / "absent.jsonl").snapshot(NOW).spans == ()
    assert adapter(board, None).snapshot(NOW).spans == ()


def test_rotated_spool_is_read_too(board: Board, tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    spool.write_text(tool_line(ago(minutes=1), tool="b") + "\n")
    (tmp_path / "spool.jsonl.1").write_text(tool_line(ago(minutes=9), tool="a") + "\n")
    assert [s.tool for s in adapter(board, spool).snapshot(NOW).spans] == ["a", "b"]
