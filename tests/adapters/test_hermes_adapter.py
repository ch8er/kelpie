from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.adapter import MAX_BODY, HermesError
from kelpie.core.model import OwnerKind, Session, SpanKind, Status
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
    assert task.owner_id is None and task.role == "dev"  # assigned, not claimed by a run
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


def test_typed_block_is_a_wait_for_an_operator(board: Board) -> None:
    board.task("t_1", "blocked", ago(days=1), assignee="dev", block_kind="needs_input")
    board.event("t_1", "claimed", ago(hours=4))
    board.event(
        "t_1", "blocked", ago(hours=2), {"kind": "needs_input", "reason": "which API version?"}
    )
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None
    assert block.reason == "waiting_human: needs_input: which API version?"
    assert block.since == ago(hours=2)


@pytest.mark.parametrize("kind", ["needs_input", "capability", "transient"])
def test_every_typed_block_kind_is_allowed_by_exit_contract(board: Board, kind: str) -> None:
    board.task("t_1", "blocked", ago(days=1), block_kind=kind)
    board.event("t_1", "blocked", ago(hours=2), {"kind": kind, "reason": None})
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None and block.reason == f"waiting_human: {kind}"


def test_untyped_legacy_block_stays_raw(board: Board) -> None:
    board.task("t_1", "blocked", ago(days=1))
    board.event("t_1", "blocked", ago(hours=1), {"reason": "waiting_ci"})
    board.task("t_2", "blocked", ago(days=1))
    board.event("t_2", "blocked", ago(hours=1), {"reason": "initial_status"})
    blocks = {t.id: t.block for t in adapter(board).snapshot(NOW).tasks}
    assert blocks["t_1"] is not None and blocks["t_1"].reason == "waiting_ci"
    assert blocks["t_2"] is not None and blocks["t_2"].reason == "initial_status"


def test_block_without_any_event_has_no_reason(board: Board) -> None:
    # tasks.block_kind survives unblock, so it is not evidence without a block event.
    board.task("t_1", "blocked", ago(hours=6), block_kind="capability")
    board.task("t_2", "blocked", ago(hours=6))
    blocks = {t.id: t.block for t in adapter(board).snapshot(NOW).tasks}
    assert blocks["t_1"] is not None and blocks["t_1"].reason == ""
    assert blocks["t_1"].since == ago(hours=6)
    assert blocks["t_2"] is not None and blocks["t_2"].reason == ""


def test_circuit_breaker_park_ignores_leftover_block_kind(board: Board) -> None:
    board.task(
        "t_1",
        "blocked",
        ago(days=1),
        block_kind="needs_input",  # from a block that was unblocked long ago
        failures=3,
        last_error="worker crashed",
    )
    board.event("t_1", "blocked", ago(hours=9), {"kind": "needs_input", "reason": "old"})
    board.event("t_1", "unblocked", ago(hours=8))
    board.event("t_1", "claimed", ago(hours=2))
    board.event("t_1", "crashed", ago(hours=1, minutes=1), {"error": "worker crashed"})
    board.event(
        "t_1",
        "gave_up",
        ago(hours=1),
        {"failures": 3, "effective_limit": 3, "error": "worker crashed"},
    )
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None
    assert block.reason == "guard: hermes circuit breaker (3/3): worker crashed"
    assert block.since == ago(hours=1)


def test_circuit_breaker_falls_back_to_columns(board: Board) -> None:
    board.task("t_1", "blocked", ago(days=1), failures=2, last_error="boom")
    board.event("t_1", "gave_up", ago(hours=1), {"failures": 2})
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None and block.reason == "guard: hermes circuit breaker: boom"
    board.task("t_2", "blocked", ago(days=1))
    board.event("t_2", "gave_up", ago(hours=1))
    reason = next(t for t in adapter(board).snapshot(NOW).tasks if t.id == "t_2").block
    assert reason is not None and reason.reason == "guard: hermes circuit breaker"


def test_non_status_events_do_not_move_status_since(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), started=ago(hours=5))
    board.event("t_1", "claimed", ago(hours=3))
    for kind in ("heartbeat", "claim_extended", "spawned", "commented", "edited", "assigned"):
        board.event("t_1", kind, ago(minutes=1))
    board.event("t_1", "never_heard_of_it", ago(hours=1))  # unknown kinds count as changes
    assert adapter(board).snapshot(NOW).tasks[0].status_since == ago(hours=1)


def test_block_payload_garbage(board: Board) -> None:
    board.task("t_1", "blocked", ago(days=1))
    board.conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
        ("t_1", "blocked", "{not json", int(ago(hours=1).timestamp())),
    )
    board.conn.commit()
    block = adapter(board).snapshot(NOW).tasks[0].block
    assert block is not None and block.reason == "" and block.since == ago(hours=1)


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
    assert s.source == "hermes"  # session-scoped actions (stop) are routed back here


def test_a_live_tool_run_marks_the_session_and_slots_are_readings(
    board: Board, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kelpie.adapters.hermes import adapter as hermes_adapter
    from kelpie.adapters.hermes.processes import HostTools, PoolUse, ToolProcesses, ToolRun
    from kelpie.core.model import ResourceKind

    board.task("t_1", "running", ago(days=1), assignee="dev")
    board.run("t_1", "dev", ago(hours=1), heartbeat=ago(minutes=2))
    board.task("t_2", "running", ago(days=1), assignee="dev-2")
    board.run("t_2", "dev-2", ago(hours=1), ended=ago(minutes=30), outcome="crashed")
    board.task("t_3", "done", ago(hours=1), assignee="dev-3")
    seen: dict[str, object] = {}

    def host(config, now, tasks, workspaces, aliases=None):  # type: ignore[no-untyped-def]
        seen["tasks"] = set(tasks)
        seen["workspaces"] = dict(workspaces)
        runs = {"t_1": ToolRun("xcodebuild", ago(minutes=12)), "t_2": ToolRun("x", ago(hours=1))}
        return HostTools(runs, {"sim": PoolUse(4, agent=1, ci=2)})

    monkeypatch.setattr(hermes_adapter, "read_host", host)
    a = HermesAdapter(db_path=board.path, spool=None, tools=ToolProcesses())
    snap = a.snapshot(NOW)
    by_task = {s.task_id: s for s in snap.sessions}
    assert by_task["t_1"].running_tool == "xcodebuild"
    assert by_task["t_1"].running_tool_since == ago(minutes=12)
    assert by_task["t_2"].running_tool is None  # its run ended: no live session to hold
    assert seen["tasks"] == {"t_1", "t_2"}  # unfinished tasks only
    assert seen["workspaces"] == {}  # no worktree recorded on this board
    slots = {r.name: (r.used, r.limit) for r in snap.resources if r.kind is ResourceKind.SLOTS}
    assert slots == {"sim:agent": (1.0, 4.0), "sim:ci": (2.0, 4.0)}
    # One failed read reuses the last good one (a slow lsof must not remind mid-build)...
    monkeypatch.setattr(hermes_adapter, "read_host", lambda *a: None)
    snap = a.snapshot(NOW + timedelta(minutes=1))
    assert {s.task_id: s.running_tool for s in snap.sessions}["t_1"] == "xcodebuild"
    slots = [r for r in snap.resources if r.kind is ResourceKind.SLOTS]
    assert slots and all(r.at == NOW for r in slots)  # the read's own time, not now
    # ...for a few minutes; then no tool runs, no readings, and the snapshot goes on.
    snap = a.snapshot(NOW + timedelta(minutes=6))
    assert snap.resources == () and all(s.running_tool is None for s in snap.sessions)
    # Not configured: nothing is read.
    monkeypatch.setattr(hermes_adapter, "read_host", lambda *a: pytest.fail("read"))
    assert adapter(board).snapshot(NOW).resources == ()


def test_a_shared_workspace_is_no_task_workspace(
    board: Board, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kelpie.adapters.hermes import adapter as hermes_adapter
    from kelpie.adapters.hermes.processes import HostTools, ToolProcesses

    own, shared = tmp_path / "own", tmp_path / "shared"
    own.mkdir()
    shared.mkdir()
    for tid, path in (("t_1", own), ("t_2", shared), ("t_3", shared)):
        board.task(tid, "running", ago(days=1), assignee="dev")
        board.update(tid, workspace_kind="worktree", workspace_path=str(path))
    seen: dict[str, object] = {}

    def host(config, now, tasks, workspaces, aliases=None):  # type: ignore[no-untyped-def]
        seen.update(tasks=set(tasks), workspaces=dict(workspaces))
        return HostTools()

    monkeypatch.setattr(hermes_adapter, "read_host", host)
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, tools=ToolProcesses())
    a.snapshot(NOW)
    assert seen["workspaces"] == {os.path.realpath(own): "t_1", os.path.realpath(shared): None}
    assert seen["tasks"] == {"t_1"}  # t_2 and t_3 share theirs: their tool runs are no one's


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
        lambda: a.unblock("t_1", "r"),
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
    task, session, s, bare = spans
    assert s.kind is SpanKind.TOOL and s.tool == "terminal" and s.outcome == "ok"
    assert s.session_id == "hermes-run-7" and s.task_id == "t_1"
    assert s.args_hash == "abcd" and s.attributes["command"] == "ls"
    assert s.attributes["kelpie.source"] == "hooks" and s.name == "execute_tool terminal"
    assert s.end == ago(minutes=5) and s.duration == timedelta(milliseconds=1500)
    assert bare.session_id is None and bare.task_id is None and bare.tool is None
    assert "command" not in bare.attributes and bare.trace_id == "hermes-task-none"
    assert s.span_id != bare.span_id
    # task -> session -> tool, one trace per task
    assert task.kind is SpanKind.TASK and task.task_id == "t_1"
    assert session.kind is SpanKind.SESSION and session.parent_id == task.span_id
    assert s.parent_id == session.span_id
    assert s.trace_id == session.trace_id == task.trace_id == "hermes-task-t_1"


def test_tool_call_id_and_session_end(board: Board, tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    end = {"type": "session_end", "at": ago(minutes=1).timestamp(), "run_id": "7"}
    spool.write_text(
        tool_line(ago(minutes=5), tool_call_id="call_1") + "\n" + json.dumps(end) + "\n"
    )
    spans = adapter(board, spool).snapshot(NOW).spans
    (session,) = [s for s in spans if s.kind is SpanKind.SESSION]
    (tool,) = [s for s in spans if s.kind is SpanKind.TOOL]
    assert tool.attributes["gen_ai.tool.call.id"] == "call_1"
    assert session.end == ago(minutes=1)


def test_spool_missing_or_disabled(board: Board, tmp_path: Path) -> None:
    assert adapter(board, tmp_path / "absent.jsonl").snapshot(NOW).spans == ()
    assert adapter(board, None).snapshot(NOW).spans == ()


def test_rotated_spool_is_read_too(board: Board, tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    spool.write_text(tool_line(ago(minutes=1), tool="b") + "\n")
    (tmp_path / "spool.jsonl.1").write_text(tool_line(ago(minutes=9), tool="a") + "\n")
    spans = adapter(board, spool).snapshot(NOW).spans
    assert [s.tool for s in spans if s.kind is SpanKind.TOOL] == ["a", "b"]


# --- liveness ---------------------------------------------------------------------------


def only_session(board: Board, spool: Path | None = None) -> Session:
    (s,) = adapter(board, spool).snapshot(NOW).sessions
    return s


def test_heartbeat_is_the_latest_of_run_task_and_spool(board: Board, tmp_path: Path) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    rid = board.run("t_1", "dev", ago(hours=3), heartbeat=ago(minutes=50))
    assert only_session(board).last_heartbeat == ago(minutes=50)
    board.update("t_1", current_run_id=rid, last_heartbeat_at=ago(minutes=40))
    assert only_session(board).last_heartbeat == ago(minutes=40)
    spool = tmp_path / "spool.jsonl"
    spool.write_text(tool_line(ago(minutes=5), run_id=str(rid)) + "\n")
    assert only_session(board, spool).last_heartbeat == ago(minutes=5)


def test_task_heartbeat_of_another_run_does_not_count(board: Board) -> None:
    board.task("t_1", "running", ago(days=1))
    old = board.run("t_1", "dev", ago(hours=5), heartbeat=ago(hours=4))
    new = board.run("t_1", "dev", ago(hours=1), heartbeat=ago(minutes=50))
    board.update("t_1", current_run_id=new, last_heartbeat_at=ago(minutes=1))
    beats = {s.id: s.last_heartbeat for s in adapter(board).snapshot(NOW).sessions}
    assert beats[f"hermes-run-{old}"] == ago(hours=4)
    assert beats[f"hermes-run-{new}"] == ago(minutes=1)


def test_valid_claim_counts_as_a_heartbeat(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    rid = board.run("t_1", "dev", ago(hours=3), heartbeat=ago(minutes=45))
    board.update("t_1", current_run_id=rid, claim_expires=NOW + timedelta(minutes=10))
    assert only_session(board).last_heartbeat == NOW


def test_expired_claim_or_other_run_is_not_a_heartbeat(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    rid = board.run("t_1", "dev", ago(hours=3), heartbeat=ago(minutes=45))
    board.update("t_1", current_run_id=rid, claim_expires=ago(minutes=1))
    assert only_session(board).last_heartbeat == ago(minutes=45)
    board.update("t_1", current_run_id=rid + 1, claim_expires=NOW + timedelta(minutes=10))
    assert only_session(board).last_heartbeat == ago(minutes=45)


def test_claim_of_an_ended_run_is_not_a_heartbeat(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1))
    rid = board.run("t_1", "dev", ago(hours=3), ended=ago(hours=1), outcome="crashed")
    board.update("t_1", current_run_id=rid, claim_expires=NOW + timedelta(minutes=10))
    assert only_session(board).last_heartbeat is None


# --- lookback, spool files, snapshot isolation ------------------------------------------


def test_done_tasks_outside_the_lookback_are_left_out(board: Board) -> None:
    board.task("t_old", "done", ago(days=9), completed=ago(days=3))
    board.task("t_new", "done", ago(days=9), completed=ago(hours=2))
    board.task("t_nodate", "done", ago(days=9))
    assert {t.id for t in adapter(board).snapshot(NOW).tasks} == {"t_new", "t_nodate"}


def test_old_rotated_spool_is_not_read(board: Board, tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    rotated = tmp_path / "spool.jsonl.1"
    rotated.write_text(tool_line(ago(minutes=9), tool="a") + "\n")
    os.utime(rotated, (NOW.timestamp() - 3 * 86400, NOW.timestamp() - 3 * 86400))
    assert adapter(board, spool).snapshot(NOW).spans == ()


def test_status_detail_reports_spool_age(board: Board, tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    assert adapter(board, spool).status_detail(NOW) == [f"spool: missing ({spool})"]
    assert adapter(board, None).status_detail(NOW) == ["spool: disabled"]
    spool.write_text("garbage\n")
    assert "no readable lines" in adapter(board, spool).status_detail(NOW)[0]
    spool.write_text(tool_line(ago(hours=3)) + "\n" + tool_line(ago(minutes=7)) + "\nnot json\n")
    assert adapter(board, spool).status_detail(NOW) == [f"spool: last line 7m ago ({spool})"]


def test_snapshot_while_hermes_holds_an_open_write_transaction(board: Board) -> None:
    """Hermes writes concurrently (WAL): a snapshot neither blocks the writer nor sees
    its uncommitted rows, and the writer can still commit afterwards."""
    board.task("t_1", "ready", ago(days=1))
    assert board.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    writer = sqlite3.connect(board.path, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES ('t_2', 'x', 'ready', 1)"
        )
        writer.execute("UPDATE tasks SET status = 'done' WHERE id = 't_1'")
        snap = adapter(board).snapshot(NOW)
        assert [(t.id, t.status) for t in snap.tasks] == [("t_1", Status.READY)]
        writer.execute("COMMIT")  # not blocked by the reader
    finally:
        writer.close()
    after = adapter(board).snapshot(NOW)
    assert {t.id: t.status for t in after.tasks} == {"t_1": Status.DONE, "t_2": Status.READY}


@pytest.mark.parametrize(
    ("value", "kept"), [(40_000, 40_000), (0, 0), (-1, None), (True, None), ("9", None)]
)
def test_spool_output_size_becomes_a_span_attribute(
    board: Board, tmp_path: Path, value: object, kept: int | None
) -> None:
    from kelpie.traces.outputs import calls

    spool = tmp_path / "spool.jsonl"
    spool.write_text(tool_line(ago(minutes=5), output_chars=value) + "\n")
    spans = adapter(board, spool).snapshot(NOW).spans
    [tool] = [s for s in spans if s.kind is SpanKind.TOOL]
    assert tool.attributes.get("kelpie.tool.output_chars") == kept
    found = calls(spans)
    if kept is None:
        assert found == []
    else:
        [call] = found
        assert (call.key, call.tokens, call.estimated) == ("ls", kept // 4, True)


def test_body_is_the_description_trimmed_and_capped(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="dev", body="  Redo after #12 lands\n")
    board.task("t_2", "ready", ago(days=1), assignee="dev", body="   ")
    board.task("t_3", "ready", ago(days=1), assignee="dev")
    board.task("t_4", "ready", ago(days=1), assignee="dev", body="x" * (MAX_BODY + 50))
    tasks = {t.id: t for t in adapter(board).snapshot(NOW).tasks}
    assert tasks["t_1"].body == "Redo after #12 lands"
    assert tasks["t_2"].body is None and tasks["t_3"].body is None
    assert tasks["t_4"].body is not None and len(tasks["t_4"].body) == MAX_BODY


def test_older_schema_without_body(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, assignee TEXT,"
        " status TEXT NOT NULL, created_at INTEGER NOT NULL, started_at INTEGER,"
        " completed_at INTEGER);"
        "CREATE TABLE task_links (parent_id TEXT, child_id TEXT);"
        "CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER,"
        " kind TEXT, payload TEXT, created_at INTEGER);"
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, profile TEXT,"
        " status TEXT, last_heartbeat_at INTEGER, started_at INTEGER, ended_at INTEGER,"
        " outcome TEXT, summary TEXT, error TEXT);"
    )
    conn.execute(
        "INSERT INTO tasks VALUES ('t_1', 'T', 'dev', 'ready', ?, NULL, NULL)",
        (int(ago(days=1).timestamp()),),
    )
    conn.commit()
    conn.close()
    (task,) = HermesAdapter(db_path=db, spool=None, git_state=False).snapshot(NOW).tasks
    assert task.body is None


def test_bytes_body_is_decoded_and_finished_tasks_keep_theirs(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.task("t_2", "done", ago(days=1), assignee="dev", completed=ago(hours=1), body="old")
    board.conn.execute("UPDATE tasks SET body = ? WHERE id = 't_1'", (b"after #7 lands \xff",))
    board.conn.commit()
    tasks = {t.id: t for t in adapter(board).snapshot(NOW).tasks}
    assert tasks["t_1"].body == "after #7 lands \ufffd"
    assert tasks["t_2"].body == "old"  # rule 1 reads acceptance criteria in done tasks


def test_comments_and_who_wrote_them(board: Board) -> None:
    board.task("t_1", "review", ago(days=1), assignee="dev", body="Ship it")
    board.update("t_1", created_by="alice")
    board.comment("t_1", "Alice", "Before automerge: 20 runs in a row, 20 of 20.", ago(hours=5))
    board.comment("t_1", "reviewer-1", "CI green, merges cleanly.", ago(hours=1))
    board.comment("t_1", "kelpie", "Checkpoint: please commit", ago(minutes=20))  # our own
    board.comment("t_1", "dev", "   ", ago(minutes=5))  # empty: left out
    board.task("t_2", "ready", ago(days=1), assignee="dev")
    board.update("t_2", created_by="dev")
    a = HermesAdapter(db_path=board.path, spool=None, human_owners=("alice",))
    tasks = {t.id: t for t in a.snapshot(NOW).tasks}
    first, second = tasks["t_1"].comments
    assert (first.author, first.human, first.at) == ("Alice", True, ago(hours=5))
    assert first.text.startswith("Before automerge") and not second.human
    assert tasks["t_1"].body_author == "alice" and tasks["t_2"].body_author is None
    assert tasks["t_1"].created_at == ago(days=1)
    assert tasks["t_2"].comments == ()


def test_a_decision_a_relay_records_is_the_persons(board: Board) -> None:
    """On a live farm human_owners is the CEO only, and a partner agent records the CEO's
    decisions with the note "решение CEO": those comments are a person's word."""
    from kelpie.adapters.hermes import HumanDecision

    board.task("t_1", "review", ago(days=1), assignee="dev", body="Решение CEO: 20 runs first")
    board.update("t_1", created_by="partner")
    board.comment("t_1", "Partner", "Решение CEO: перед automerge 20 runs, 20 of 20.", ago(hours=5))
    board.comment("t_1", "partner", "Собрал логи, жду ревью.", ago(hours=4))  # its own note
    board.comment("t_1", "dev", "решение CEO: merge it now", ago(hours=3))  # not a relay
    board.comment("t_1", "partner", "нерешение CEOs", ago(hours=2))  # inside other words
    board.task("t_2", "ready", ago(days=1), assignee="dev", body="plan without the note")
    board.update("t_2", created_by="partner")
    relay = HumanDecision("решение CEO", frozenset({"PARTNER"}))
    a = HermesAdapter(db_path=board.path, spool=None, human_decisions=(relay,))
    tasks = {t.id: t for t in a.snapshot(NOW).tasks}
    assert [c.human for c in tasks["t_1"].comments] == [True, False, False, False]
    assert tasks["t_1"].body_author == "partner" and tasks["t_2"].body_author is None
    # With the person named, the decision is written by them: rule 1 asks the CEO.
    ceo = HumanDecision("решение CEO", frozenset({"partner"}), "ceo")
    named = HermesAdapter(db_path=board.path, spool=None, human_decisions=(ceo,))
    tasks = {t.id: t for t in named.snapshot(NOW).tasks}
    assert [c.author for c in tasks["t_1"].comments] == ["ceo", "partner", "dev", "partner"]
    assert tasks["t_1"].body_author == "ceo"
    # Without the entry the partner is an agent like any other.
    plain = HermesAdapter(db_path=board.path, spool=None)
    assert not any(c.human for c in plain.snapshot(NOW).tasks[0].comments)


def test_a_human_decision_needs_a_marker_and_a_relay() -> None:
    from kelpie.adapters.hermes import HumanDecision

    with pytest.raises(ValueError, match="marker and at least one relay"):
        HumanDecision(" ", frozenset({"partner"}))
    with pytest.raises(ValueError, match="marker and at least one relay"):
        HumanDecision("решение CEO", frozenset())
    with pytest.raises(ValueError, match="marker and at least one relay"):
        HumanDecision("решение CEO", frozenset({" "}))  # would match a NULL author
    with pytest.raises(ValueError, match="not a string"):
        HumanDecision("решение CEO", "partner")  # type: ignore[arg-type]
    assert HumanDecision("x", frozenset({"p"}), " CEO ").person == "ceo"


@pytest.mark.parametrize(
    "text",
    [
        "Жду решение CEO по 20 runs in a row",  # waiting for one
        "Это не решение CEO, предлагаю 20 runs",  # says it is not
        "> dev-1: решение CEO: merge now\nПредлагаю 20 runs",  # a quote  # noqa: RUF001
        "Предлагаю 20 runs.\nРешение CEO: merge as is",  # not where it opens  # noqa: RUF001
    ],
)
def test_only_a_comment_that_opens_with_the_marker_is_a_decision(text: str) -> None:
    from kelpie.adapters.hermes import HumanDecision

    d = HumanDecision("решение CEO", frozenset({"partner"}), "ceo")
    assert not d.recorded("partner", text)
    assert d.recorded("partner", "  Решение  CEO: 20 runs in a row")


@pytest.mark.parametrize(
    ("text", "recorded"),
    [
        ("\ufeffРешение CEO: 20 runs", True),  # a BOM  # noqa: RUF001
        ("\u200bРешение CEO: 20 runs", True),  # a zero-width space  # noqa: RUF001
        ("**Решение CEO**: 20 runs", True),
        ("## Решение CEO: 20 runs", True),
        ("Решение CEO — 20 runs", True),
        ("Решение ＣＥＯ: 20 runs", True),  # full-width, NFKC  # noqa: RUF001
        ("Решение CEO пока не принято. Предлагаю 20 runs", False),  # no separator
        ("Решение CEO? Жду", False),
        ("> Решение CEO: 20 runs", False),  # a quote
        ("Решение CEO-подобное, но я бы подождал", False),  # a compound word
        ("Решение CEO-level: подождём", False),
        ("Решение CEO - 20 runs", True),
        ("Решение CEO\n\n- вариант A\n- вариант B", False),  # options for the CEO, a list
    ],
)
def test_the_marker_opens_the_text_with_a_separator(text: str, recorded: bool) -> None:
    from kelpie.adapters.hermes import HumanDecision

    d = HumanDecision("решение CEO", frozenset({"partner"}), "ceo")
    assert d.recorded("partner", text) is recorded


def test_relayed_comments_never_push_out_a_persons(board: Board) -> None:
    from kelpie.adapters.hermes import HumanDecision
    from kelpie.adapters.hermes.adapter import MAX_COMMENTS

    board.task("t_1", "review", ago(days=3), assignee="dev")
    for i in range(MAX_COMMENTS + 5):
        board.comment(
            "t_1", "partner", f"Решение CEO: note {i}", ago(days=2) + timedelta(minutes=i)
        )
    board.comment("t_1", "ceo", "Before automerge: 20 runs in a row.", ago(hours=1))
    board.comment("t_1", "", "Решение CEO: from nobody", ago(minutes=30))  # no author
    first = HumanDecision("решение CEO", frozenset({"partner"}), "ceo")
    other = HumanDecision("решение CTO", frozenset({"partner"}), "cto")  # first match wins
    a = HermesAdapter(
        db_path=board.path, spool=None, human_owners=("ceo", "cto"), human_decisions=(first, other)
    )
    (task,) = a.snapshot(NOW).tasks
    own = [c for c in task.comments if c.text.startswith("Before automerge")]
    assert own and own[0].human and own[0].author == "ceo"
    relayed = [c for c in task.comments if c.text.startswith("Решение CEO: note")]
    assert len(relayed) == MAX_COMMENTS and all(c.human and c.author == "ceo" for c in relayed)
    assert not [c for c in task.comments if c.text.endswith("from nobody") and c.human]


def test_comments_signed_with_the_configured_author_are_kelpies(board: Board) -> None:
    board.task("t_1", "review", ago(days=1), assignee="dev")
    board.comment("t_1", "ops-bot", "BLOCKED: Stopped by Kelpie", ago(hours=1))
    board.comment("t_1", "kelpie", "a profile named kelpie", ago(minutes=30))
    (task,) = HermesAdapter(db_path=board.path, spool=None, author="ops-bot").snapshot(NOW).tasks
    assert [c.author for c in task.comments] == ["kelpie"]


def test_a_persons_early_comment_is_never_pushed_out(board: Board) -> None:
    from kelpie.adapters.hermes.adapter import MAX_COMMENTS

    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.comment("t_1", "alice", "Before merge: 5 runs in a row.", ago(hours=20))
    for i in range(MAX_COMMENTS + 3):
        board.comment("t_1", "dev", f"note {i}", ago(hours=10) + timedelta(minutes=i))
    a = HermesAdapter(db_path=board.path, spool=None, human_owners=("alice",))
    (task,) = a.snapshot(NOW).tasks
    assert task.comments[0].author == "alice" and len(task.comments) == MAX_COMMENTS + 1


def test_only_the_last_comments_are_kept(board: Board) -> None:
    from kelpie.adapters.hermes.adapter import MAX_COMMENTS

    board.task("t_1", "ready", ago(days=1), assignee="dev")
    for i in range(MAX_COMMENTS + 3):
        board.comment("t_1", "dev", f"note {i}", ago(hours=10) + timedelta(minutes=i))
    (task,) = adapter(board).snapshot(NOW).tasks
    assert (
        len(task.comments) == MAX_COMMENTS and task.comments[-1].text == f"note {MAX_COMMENTS + 2}"
    )


def test_a_board_without_comments_still_reads(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.conn.execute("DROP TABLE task_comments")
    (task,) = adapter(board).snapshot(NOW).tasks
    assert task.comments == ()


def test_roles_from_the_table_or_the_profile_without_instance_number(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev-1")
    board.task("t_2", "ready", ago(days=1), assignee="dev-2")
    board.task("t_3", "ready", ago(days=1), assignee="reviewer-bot")
    board.task("t_4", "ready", ago(days=1), assignee="ops")
    board.task("t_5", "ready", ago(days=1), assignee="build-2024-1")
    board.task("t_6", "ready", ago(days=1), assignee="")
    a = HermesAdapter(db_path=board.path, spool=None, roles={"Reviewer-Bot": "reviewer"})
    snap = a.snapshot(NOW)
    roles = {t.id: t.role for t in snap.tasks}
    assert roles == {
        "t_1": "dev",
        "t_2": "dev",
        "t_3": "reviewer",
        "t_4": "ops",
        "t_5": "build-2024",
        "t_6": None,
    }
    assert {o.id: o.role for o in snap.owners}["dev-2"] == "dev"


def test_role_of_ignores_names_that_are_only_a_number() -> None:
    a = HermesAdapter(spool=None)
    assert a.role_of("-1") == "-1" and a.role_of("x-") == "x-" and a.role_of("Dev-3") == "dev"


def test_role_values_are_casefolded_and_name_heuristic_can_be_off(board: Board) -> None:
    a = HermesAdapter(db_path=board.path, spool=None, roles={"bot": "Dev"})
    assert a.role_of("bot") == "dev" == a.role_of("dev-1")
    off = HermesAdapter(db_path=board.path, spool=None, role_from_name=False)
    assert off.role_of("dev-1") == "dev-1"


# --- owner: a live run claims the task (issue 68) ---------------------------------------


def test_owner_is_the_profile_only_while_a_live_run_claims_the_task(board: Board) -> None:
    board.task("t_run", "running", ago(days=1), assignee="dev-1")
    board.update("t_run", current_run_id=board.run("t_run", "dev-1", ago(minutes=20)))
    board.task("t_rev", "review", ago(days=1), assignee="reviewer-1")
    board.update("t_rev", current_run_id=board.run("t_rev", "reviewer-1", ago(minutes=5)))
    board.task("t_ended", "running", ago(days=1), assignee="dev-2")
    rid = board.run("t_ended", "dev-2", ago(hours=2), status="done", ended=ago(hours=1))
    board.update("t_ended", current_run_id=rid)  # its run ended: nobody holds it
    board.task("t_ready", "ready", ago(days=1), assignee="dev-3")
    board.task("t_blocked", "blocked", ago(days=1), assignee="dev-3")
    snap = adapter(board).snapshot(NOW)
    owners = {t.id: t.owner_id for t in snap.tasks}
    assert owners == {
        "t_run": "dev-1",
        "t_rev": "reviewer-1",
        "t_ended": None,
        "t_ready": None,
        "t_blocked": None,
    }
    # Assigned profiles are still owners of the farm (capacity per role), claimed or not.
    assert {"dev-1", "dev-2", "dev-3", "reviewer-1"} <= {o.id for o in snap.owners}


def test_a_human_assignee_owns_the_task_without_a_run(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="alice")
    a = HermesAdapter(db_path=board.path, spool=None, human_owners=frozenset({"Alice"}))
    (task,) = a.snapshot(NOW).tasks
    assert task.owner_id == "alice"


def test_older_schema_without_current_run_counts_running_as_claimed(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    board.task("t_2", "ready", ago(days=1), assignee="dev")
    board.conn.execute("ALTER TABLE tasks DROP COLUMN current_run_id")
    board.conn.commit()
    owners = {t.id: t.owner_id for t in adapter(board).snapshot(NOW).tasks}
    assert owners == {"t_1": "dev", "t_2": None}


def test_owner_is_the_profile_of_the_claiming_run(board: Board) -> None:
    board.task("t_1", "review", ago(days=1), assignee="dev-2")  # routed back to its developer
    board.update("t_1", current_run_id=board.run("t_1", "reviewer", ago(minutes=5)))
    (task,) = adapter(board).snapshot(NOW).tasks
    assert task.owner_id == "reviewer" and task.assignee == "dev-2" and task.role == "dev"


def test_assignee_is_reported_whether_claimed_or_not(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="dev-1")
    board.task("t_2", "ready", ago(days=1), assignee="")
    tasks = {t.id: t for t in adapter(board).snapshot(NOW).tasks}
    assert tasks["t_1"].assignee == "dev-1" and tasks["t_1"].owner_id is None
    assert tasks["t_2"].assignee is None


# --- parents and pre-work cards (incident 17) --------------------------------------------


def test_a_decomposed_root_lists_its_subtasks(board: Board) -> None:
    board.task("t_root", "todo", ago(days=1), assignee="orchestrator")
    board.event("t_root", "decomposed", ago(hours=5), {"child_ids": ["t_a", "t_b", "t_a", 3]})
    board.task("t_a", "done", ago(days=1), assignee="dev")
    board.task("t_plain", "todo", ago(days=1), assignee="dev")
    tasks = {t.id: t for t in adapter(board).snapshot(NOW).tasks}
    assert tasks["t_root"].subtasks == ("t_a", "t_b")
    assert tasks["t_plain"].subtasks == ()


def test_pre_work_statuses_mark_cards(board: Board) -> None:
    board.task("t_triage", "triage", ago(days=1))
    board.task("t_sched", "scheduled", ago(days=1))
    board.task("t_todo", "todo", ago(days=1))
    snap = adapter(board).snapshot(NOW)
    tasks = {t.id: t.prework for t in snap.tasks}
    assert tasks == {"t_triage": True, "t_sched": True, "t_todo": False}
    # All three are backlog, but the platform's own status is kept (incident 17).
    assert {t.id: (t.status, t.platform_status) for t in snap.tasks} == {
        "t_triage": (Status.BACKLOG, "triage"),
        "t_sched": (Status.BACKLOG, "scheduled"),
        "t_todo": (Status.BACKLOG, "todo"),
    }
    a = HermesAdapter(db_path=board.path, spool=None, prework_statuses=("triage",))
    assert {t.id: t.prework for t in a.snapshot(NOW).tasks}["t_sched"] is False


def test_kelpies_own_park_is_waiting_not_pre_work(board: Board) -> None:
    from kelpie.adapters.hermes.park import encode_wait, load_key
    from kelpie.core.model import Condition, ConditionKind

    board.task("t_1", "scheduled", ago(days=1), assignee="dev")
    a = adapter(board)
    key = load_key(board.path.parent / "wait.key", create=True)
    assert key is not None
    a.wait_key = board.path.parent / "wait.key"
    cond = Condition("ready:t_1:x", ConditionKind.CUSTOM, "t_1")
    board.event(
        "t_1",
        "scheduled",
        NOW - timedelta(minutes=5),
        {"reason": encode_wait("r", (cond,), "t_1", key, NOW - timedelta(minutes=5))},
    )
    (task,) = a.snapshot(NOW).tasks
    assert task.status is Status.WAITING and task.prework is False
    assert task.platform_status == "scheduled"


def test_review_returns_are_reported(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.event("t_1", "changes_requested", ago(hours=3), {"reason": "merge conflict in a.py"})
    board.event("t_1", "review_reopened", ago(hours=2))
    board.event("t_1", "changes_requested", ago(days=3), {"reason": "older than lookback"})
    board.event("t_1", "changes_requested", ago(hours=1), {"reason": 7})  # not a string
    board.event("t_1", "changes_requested", ago(minutes=30), {"reason": "x" * 3000})
    (task,) = adapter(board).snapshot(NOW).tasks
    assert [(r.reason[:30], r.category) for r in task.returns] == [
        ("older than lookback", "review"),  # a review loop is the task's whole history
        ("merge conflict in a.py", "review"),
        ("review reopened", "review"),
        ("changes requested", "review"),
        ("x" * 30, "review"),
    ]
    assert len(task.returns[-1].reason) == 2000


def test_a_hermes_review_loop_reaches_ping_pong(board: Board) -> None:
    from kelpie.rules.ping_pong import PingPong

    board.task("t_1", "ready", ago(days=5), assignee="dev")
    for days in (4, 3, 2):  # some of it older than lookback
        board.event("t_1", "changes_requested", ago(days=days), {"reason": f"round {days}"})
    snap = adapter(board).snapshot(NOW)
    [escalation] = PingPong().evaluate(snap)
    assert escalation.task_id == "t_1" and "3" in escalation.reason


# --- evidence for "done" and reopens (rule 1) --------------------------------------------


def test_a_done_task_moved_back_is_a_reopen(board: Board) -> None:
    board.task("t_1", "ready", ago(days=2), assignee="dev")
    board.event("t_1", "completed", ago(hours=10))
    board.event("t_1", "worker_result", ago(hours=10))  # bookkeeping after completion
    board.event("t_1", "pr_acceptance", ago(hours=9))
    board.event("t_1", "status", ago(hours=9), {"status": "archived"})  # still done
    board.event("t_1", "status", ago(hours=8), {"status": "ready"})  # reopened
    board.event("t_1", "status", ago(hours=6), {"status": "done"})
    board.event("t_1", "descendant_invalidated", ago(hours=5), {"previous_status": "done"})
    board.event("t_1", "status", ago(hours=5), {"status": "todo"})  # reopened again, once
    board.event("t_1", "status", ago(hours=4), {"status": "ready"})  # not done before
    board.task("t_2", "done", ago(days=1), assignee="dev")
    board.event("t_2", "completed", ago(hours=3))
    board.event("t_2", "worker_result", ago(hours=3))
    tasks = {t.id: t for t in adapter(board).snapshot(NOW).tasks}
    assert tasks["t_1"].reopened_at == (ago(hours=8), ago(hours=5))
    assert tasks["t_2"].reopened_at == ()
    assert tasks["t_1"].returns == ()  # a reopen is not a send-back before done
    assert tasks["t_2"].status_since == ago(hours=3)  # bookkeeping does not move it


def attachments_table(board: Board) -> None:
    board.conn.execute(
        "CREATE TABLE task_attachments (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT,"
        " filename TEXT, stored_path TEXT, content_type TEXT, size INTEGER, uploaded_by TEXT,"
        " created_at INTEGER)"
    )


def attach(board: Board, name: str, at: datetime, by: str | None) -> None:
    board.conn.execute(
        "INSERT INTO task_attachments (task_id, filename, stored_path, uploaded_by, created_at)"
        " VALUES ('t_1', ?, '/x', ?, ?)",
        (name, by, int(at.timestamp())),
    )


def test_evidence_is_what_a_run_left(board: Board) -> None:
    board.task("t_1", "done", ago(days=1), assignee="dev")
    first = board.run("t_1", "dev", ago(hours=5), status="done", ended=ago(hours=4))
    second = board.run("t_1", "dev", ago(hours=3), status="done", ended=ago(hours=2))
    board.event(
        "t_1",
        "completed",
        ago(hours=2),
        {"artifacts": ["reports/junit.xml", "", 7]},
        run_id=second,
    )
    attachments_table(board)
    attach(board, "test-plan.md", ago(days=1), "alice")  # a person's spec, before any run
    attach(board, "spec-test.md", ago(hours=4, minutes=30), "alice")  # a person, mid-run
    attach(board, "pytest.txt", ago(hours=4, minutes=30), "dev")  # the agent, during run 1
    attach(board, "early-test.txt", ago(days=2), "dev")  # the agent, before its first run
    attach(board, "live-check.txt", ago(hours=1), "dev")  # after run 2 ended: run 2
    board.conn.commit()
    sessions = {s.id: s for s in adapter(board).snapshot(NOW).sessions}
    assert [a.path for a in sessions[f"hermes-run-{first}"].artifacts] == ["pytest.txt"]
    assert [a.path for a in sessions[f"hermes-run-{second}"].artifacts] == [
        "reports/junit.xml",
        "live-check.txt",
    ]


def test_no_attachments_table_is_fine(board: Board) -> None:
    board.task("t_1", "done", ago(days=1), assignee="dev")
    board.run("t_1", "dev", ago(hours=5), status="done", ended=ago(hours=4))
    (session,) = adapter(board).snapshot(NOW).sessions
    assert session.artifacts == ()


def test_a_cascade_from_a_reopened_ancestor_is_not_the_childs_reopen(board: Board) -> None:
    board.task("t_1", "todo", ago(days=1), assignee="dev")
    board.event("t_1", "completed", ago(hours=6))
    board.event("t_1", "status", ago(hours=2), {"status": "todo", "reason": "ancestor_reopened"})
    (task,) = adapter(board).snapshot(NOW).tasks
    assert task.reopened_at == ()


def test_attachments_of_hermes_attach_tool_count_for_the_running_run(board: Board) -> None:
    board.task("t_1", "done", ago(days=1), assignee="dev")
    run = board.run("t_1", "dev", ago(hours=3), status="done", ended=ago(hours=1))
    attachments_table(board)
    attach(board, "pytest.txt", ago(hours=2), "agent")  # kanban_attach records "agent"
    attach(board, "notes.txt", ago(hours=2), "dashboard")  # a person, through the dashboard
    board.conn.commit()
    (session,) = adapter(board).snapshot(NOW).sessions
    assert session.id == f"hermes-run-{run}" and [a.path for a in session.artifacts] == [
        "pytest.txt"
    ]


def test_a_persons_approval_is_not_an_agents_completion(board: Board) -> None:
    from kelpie.rules.exit_contract import build

    board.task("t_1", "done", ago(days=1), assignee="dev")
    board.run(
        "t_1", "dev", ago(hours=4), status="review", ended=ago(hours=3), outcome="review_requested"
    )
    # The approval: Hermes synthesizes a zero-length completed run.
    board.run("t_1", "dev", ago(hours=1), status="done", ended=ago(hours=1), outcome="completed")
    board.event("t_1", "completed", ago(hours=1))
    snap = adapter(board).snapshot(NOW)
    assert [s.final_status for s in snap.sessions] == [Status.REVIEW]
    rule = build({"done_evidence": [{"name": "tests", "artifacts": ["*pytest*"]}]})
    assert rule.evaluate(snap) == []


def test_a_marker_with_full_width_punctuation_matches_itself() -> None:
    from kelpie.adapters.hermes import HumanDecision

    d = HumanDecision("решение\uff08CEO\uff09", frozenset({"partner"}))  # full-width ( )
    assert d.recorded("partner", "Решение(CEO): 20 runs")  # NFKC, then escaped
    assert not d.recorded("partner", "Решение CEO: 20 runs")


@pytest.mark.parametrize(
    ("fields", "outcome", "result", "code"),
    [
        # The farm's sample (#229): what each kind of call becomes on its span.
        ({"exit_code": 0}, "ok", "ok", 0),
        ({"exit_code": 127}, "error", "error", 127),
        ({"exit_code": 1, "outcome": "ok"}, "error", "error", 1),  # the exit code wins
        ({"exit_code": 124}, "error", "error", 124),
        ({"exit_code": 0, "background": True}, "ok", "unknown", 0),  # started, not done
        # Refused: neither a failure nor a success (Hermes' hook says error for it).
        ({"exit_code": -1, "blocked": True, "outcome": "error"}, None, "blocked", -1),
        ({"exit_code": -1}, "error", "error", -1),  # a degraded backend, a fatal error
        ({"exit_code": -15, "tool": "process"}, "ok", None, None),  # another job's code
        ({"exit_code": "1"}, "ok", None, None),  # not an int: no result known
        ({"exit_code": True}, "ok", None, None),
        ({}, "ok", None, None),  # an older plugin, or a tool without an exit code
        ({"outcome": "error"}, "error", None, None),
    ],
)  # fmt: skip
def test_a_spool_exit_code_becomes_the_calls_result(
    board: Board,
    tmp_path: Path,
    fields: dict[str, object],
    outcome: str | None,
    result: str | None,
    code: int | None,
) -> None:
    from kelpie.core.tools import exit_code_of, result_of

    spool = tmp_path / "spool.jsonl"
    spool.write_text(tool_line(ago(minutes=5), **fields) + "\n")
    [tool] = [s for s in adapter(board, spool).snapshot(NOW).spans if s.kind is SpanKind.TOOL]
    assert (tool.outcome, result_of(tool), exit_code_of(tool)) == (outcome, result, code)
