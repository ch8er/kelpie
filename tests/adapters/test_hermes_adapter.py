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


def test_bytes_body_is_decoded_and_finished_tasks_keep_none(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.task("t_2", "done", ago(days=1), assignee="dev", completed=ago(hours=1), body="old")
    board.conn.execute("UPDATE tasks SET body = ? WHERE id = 't_1'", (b"after #7 lands \xff",))
    board.conn.commit()
    tasks = {t.id: t for t in adapter(board).snapshot(NOW).tasks}
    assert tasks["t_1"].body == "after #7 lands \ufffd"
    assert tasks["t_2"].body is None


def test_roles_from_the_table_or_the_profile_without_instance_number(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev-1")
    board.task("t_2", "ready", ago(days=1), assignee="dev-2")
    board.task("t_3", "ready", ago(days=1), assignee="reviewer-bot")
    board.task("t_4", "ready", ago(days=1), assignee="ops")
    board.task("t_5", "ready", ago(days=1), assignee="build-2024-1")
    board.task("t_6", "ready", ago(days=1), assignee="")
    a = HermesAdapter(db_path=board.path, spool=None, roles={"Reviewer-Bot": "reviewer"})
    snap = a.snapshot(NOW)
    roles = {t.id: (t.owner_id, t.role) for t in snap.tasks}
    assert roles == {
        "t_1": ("dev-1", "dev"),
        "t_2": ("dev-2", "dev"),
        "t_3": ("reviewer-bot", "reviewer"),
        "t_4": ("ops", "ops"),
        "t_5": ("build-2024-1", "build-2024"),
        "t_6": (None, None),
    }
    assert {o.id: o.role for o in snap.owners}["dev-2"] == "dev"


def test_role_of_ignores_names_that_are_only_a_number() -> None:
    a = HermesAdapter(spool=None)
    assert a.role_of("-1") == "-1" and a.role_of("x-") == "x-" and a.role_of("Dev-3") == "dev"
