"""The writer of a Hermes spool line (#302, audit M1): the plugin writes its pid and start
fingerprint; the adapter marks lines whose writer is not the run's recorded worker as
unverified, and rule 10 does not stop a run on those alone."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.hermes import HermesAdapter, plugin
from kelpie.adapters.hermes.proctable import fingerprint, instantiation_epoch, read_proc
from kelpie.adapters.hermes.worker import UNVERIFIED, RunWorker, writer_matches
from kelpie.core.actions import ActionKind
from kelpie.core.model import Span, SpanKind
from kelpie.core.spans import UNVERIFIED_ATTRIBUTE
from kelpie.rules import REGISTRY
from tests.adapters.kanban import Board

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
PRINT = "boot-1:30|4242"


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_plugin_writes_the_fingerprint_hermes_records() -> None:
    proc = read_proc(os.getpid())
    assert proc is not None
    expected = fingerprint(proc, instantiation_epoch())
    assert plugin._writer() == {"pid": os.getpid(), "pid_start": expected}


def test_plugin_writes_the_writer_on_every_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool.jsonl"
    monkeypatch.setenv(plugin.SPOOL_ENV, str(spool))
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "7")
    plugin.post_tool_call(tool_name="terminal", args={"command": "ls"}, result="ok")
    plugin.on_session_end(completed=True)
    lines = [json.loads(x) for x in spool.read_text().splitlines()]
    assert all(line["pid"] == os.getpid() and "pid_start" in line for line in lines)


def test_plugin_without_proc_writes_no_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_PROC", tmp_path / "no-proc")
    monkeypatch.setattr(plugin, "_WRITER", None)
    assert plugin._writer() == {"pid": os.getpid(), "pid_start": None}


@pytest.mark.parametrize(
    ("writer", "worker", "expected"),
    [
        ((10, PRINT), RunWorker(10, PRINT, None, None, "h:1"), True),
        ((11, PRINT), RunWorker(10, PRINT, 11, PRINT, "h:1"), True),  # the agent
        ((10, "boot-1:30|9999"), RunWorker(10, PRINT, None, None, "h:1"), False),  # pid reused
        ((99, PRINT), RunWorker(10, PRINT, None, None, "h:1"), False),  # another process
        (None, RunWorker(10, PRINT, None, None, "h:1"), None),  # an older plugin
        ((10, PRINT), None, None),  # an older board
        ((10, PRINT), RunWorker(None, None, None, None, None), None),  # no recorded worker
        ((10, UNVERIFIED), RunWorker(10, UNVERIFIED, None, None, "h:1"), None),
    ],
)
def test_writer_matches(writer: Any, worker: RunWorker | None, expected: bool | None) -> None:
    assert writer_matches(writer, worker) is expected


def line(n: int, run: int, task: str, writer: tuple[int, str] | None) -> str:
    pid, start = writer if writer is not None else (None, None)
    return json.dumps(
        {
            "type": "tool",
            "at": (NOW - timedelta(seconds=120 - 10 * n)).timestamp(),
            "task_id": task,
            "run_id": str(run),
            "tool": "terminal",
            "tool_call_id": f"{task}-{n}",
            "args_hash": "0123456789abcdef",
            "outcome": "ok",
            "duration_ms": 10,
            "pid": pid,
            "pid_start": start,
        }
    )


def farm(board: Board, tmp_path: Path, writers: dict[int, tuple[int, str] | None]) -> Path:
    """Runs 1 (task t_a, worker pid 10) and 2 (t_b, pid 20), each with 10 identical calls
    whose lines carry ``writers[run]``."""
    started = NOW - timedelta(minutes=20)
    lines = []
    for run, (task, pid) in {1: ("t_a", 10), 2: ("t_b", 20)}.items():
        board.task(task, "running", NOW - timedelta(hours=1), assignee="dev-1", started=started)
        rid = board.run(task, "dev-1", started, heartbeat=NOW)
        assert rid == run
        board.update(task, current_run_id=rid)
        board.conn.execute(
            "UPDATE task_runs SET worker_pid = ?, worker_started_at = ?, claim_lock = ?"
            " WHERE id = ?",
            (pid, PRINT, "host-a:1", rid),
        )
        lines += [line(n, run, task, writers[run]) for n in range(10)]
    board.conn.commit()
    spool = tmp_path / "spool.jsonl"
    spool.write_text("\n".join(lines) + "\n")
    return spool


def flags(board: Board, spool: Path) -> dict[str | None, set[bool]]:
    snap = HermesAdapter(db_path=board.path, spool=spool, git_state=False).snapshot(NOW)
    out: dict[str | None, set[bool]] = {}
    for s in snap.spans:
        assert "_kelpie.writer" not in s.attributes  # the writer is checked, then dropped
        if s.kind is not SpanKind.TOOL:
            continue
        out.setdefault(s.session_id, set()).add(s.attributes.get(UNVERIFIED_ATTRIBUTE) is True)
    return out


def test_lines_from_the_runs_own_worker_are_verified(board: Board, tmp_path: Path) -> None:
    spool = farm(board, tmp_path, {1: (10, PRINT), 2: (20, PRINT)})
    assert flags(board, spool) == {"hermes-run-1": {False}, "hermes-run-2": {False}}


def test_a_line_from_another_process_is_unverified(board: Board, tmp_path: Path) -> None:
    # run 2's worker (pid 20) writes lines naming run 1
    spool = farm(board, tmp_path, {1: (20, PRINT), 2: (20, PRINT)})
    assert flags(board, spool) == {"hermes-run-1": {True}, "hermes-run-2": {False}}


def test_lines_without_a_writer_are_unverified(board: Board, tmp_path: Path) -> None:
    spool = farm(board, tmp_path, {1: None, 2: (20, PRINT)})
    assert flags(board, spool) == {"hermes-run-1": {True}, "hermes-run-2": {False}}


def test_a_non_matching_pid_cannot_trigger_a_stop(board: Board, tmp_path: Path) -> None:
    spool = farm(board, tmp_path, {1: (20, PRINT), 2: (20, PRINT)})
    snap = HermesAdapter(db_path=board.path, spool=spool, git_state=False).snapshot(NOW)
    actions = REGISTRY["loop_breaker"]({}).evaluate(snap)
    stops = {a.params["session_id"] for a in actions if a.kind is ActionKind.STOP}
    assert stops == {"hermes-run-2"}
    (alert,) = [
        a
        for a in actions
        if a.kind is ActionKind.ALERT and a.params.get("session_id") == "hermes-run-1"
    ]
    assert alert.params["unverified"] is True and "Not stopped" in alert.reason


# --- folds, boards, the wire ---------------------------------------------------------------


def hook_and_otlp(n: int, unverified: bool, otlp_session: str | None) -> list[Span]:
    from kelpie.traces.hooks import HOOKS, SOURCE

    out = []
    for i in range(n):
        at = NOW - timedelta(seconds=120 - 10 * i)
        attrs: dict[str, Any] = {SOURCE: HOOKS, "gen_ai.tool.call.id": f"c{i}"}
        if unverified:
            attrs[UNVERIFIED_ATTRIBUTE] = True
        out.append(
            Span(
                "h",
                f"h{i}",
                "execute_tool terminal",
                SpanKind.TOOL,
                at,
                at,
                session_id="hermes-run-1",
                tool="terminal",
                args_hash="a" * 16,
                outcome="ok",
                attributes=attrs,
            )
        )
        out.append(
            Span(
                "o",
                f"o{i}",
                "execute_tool terminal",
                SpanKind.TOOL,
                at,
                at,
                session_id=otlp_session,
                tool="terminal",
                args_hash="a" * 16,
                outcome="ok",
                attributes={"gen_ai.tool.call.id": f"c{i}"},
            )
        )
    return out


def test_an_unverified_hook_does_not_mark_or_move_an_otlp_span() -> None:
    from kelpie.traces.hooks import merge_hook_spans

    folded = merge_hook_spans(hook_and_otlp(3, True, "hermes-run-1")).spans
    assert [s.span_id for s in folded] == ["o0", "o1", "o2"]
    assert all(UNVERIFIED_ATTRIBUTE not in s.attributes for s in folded)
    moved = merge_hook_spans(hook_and_otlp(3, True, "chat-9")).spans
    assert {s.session_id for s in moved} == {"chat-9"}  # not into the run the line names
    verified = merge_hook_spans(hook_and_otlp(3, False, "chat-9")).spans
    assert {s.session_id for s in verified} == {"hermes-run-1"}


def test_otlp_cannot_set_or_clear_the_mark() -> None:
    from kelpie.traces.semconv import is_kept

    assert not is_kept(UNVERIFIED_ATTRIBUTE) and not is_kept("kelpie.ingest.token")


def test_the_mark_survives_the_agent_to_hub_wire() -> None:
    from kelpie.core.model import Snapshot
    from kelpie.remote.protocol import decode_snapshot, encode_snapshot

    span = hook_and_otlp(1, True, None)[0]
    back = decode_snapshot(encode_snapshot(Snapshot(now=NOW, spans=(span,))))
    assert back.spans[0].attributes[UNVERIFIED_ATTRIBUTE] is True


def test_a_mismatched_writer_on_one_of_several_boards(tmp_path: Path) -> None:
    from kelpie.adapters.hermes.boards import HermesBoards

    boards = []
    for name in ("qr", "sign"):
        (tmp_path / name).mkdir()
        boards.append(Board(tmp_path / name / "kanban.db"))
    try:
        started = NOW - timedelta(minutes=20)
        lines = []
        for b, task, pid in ((boards[0], "t_q", 10), (boards[1], "t_s", 20)):
            b.task(task, "running", NOW - timedelta(hours=1), assignee="dev-1", started=started)
            rid = b.run(task, "dev-1", started, heartbeat=NOW)
            b.update(task, current_run_id=rid)
            b.conn.execute(
                "UPDATE task_runs SET worker_pid = ?, worker_started_at = ? WHERE id = ?",
                (pid, PRINT, rid),
            )
            b.conn.commit()
        lines = [line(n, 1, "t_q", (10, PRINT)) for n in range(3)]  # qr's own worker
        lines += [line(n, 1, "t_s", (10, PRINT)) for n in range(3)]  # qr's worker naming sign
        spool = tmp_path / "spool.jsonl"
        spool.write_text("\n".join(lines) + "\n")
        adapters = [
            HermesAdapter(db_path=b.path, spool=None, git_state=False, board=n)
            for b, n in zip(boards, ("qr", "sign"), strict=True)
        ]
        snap = HermesBoards(boards=adapters, spool=spool).snapshot(NOW)
        tools = [s for s in snap.spans if s.kind is SpanKind.TOOL]
        marks = {s.session_id: s.attributes.get(UNVERIFIED_ATTRIBUTE) is True for s in tools}
        assert marks == {"qr:hermes-run-1": False, "sign:hermes-run-1": True}
    finally:
        for b in boards:
            b.close()
