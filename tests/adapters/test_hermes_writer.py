"""The writer of a Hermes spool line (#302, audit M1): the plugin writes its pid and start
fingerprint; the adapter marks lines whose writer is not the run's recorded worker as
unverified, and rule 10 does not stop a run on those alone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.hermes import HermesAdapter, plugin, proctable
from kelpie.adapters.hermes.proctable import (
    Libproc,
    fingerprint,
    host_table,
    instantiation_epoch,
    read_proc,
)
from kelpie.adapters.hermes.worker import UNVERIFIED, RunWorker, writer_matches
from kelpie.core.actions import ActionKind
from kelpie.core.model import Session, Snapshot, Span, SpanKind
from kelpie.core.spans import UNVERIFIED_ATTRIBUTE
from kelpie.rules import REGISTRY
from tests.adapters.kanban import Board
from tests.adapters.libproc_fake import fake_farm, stamp

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
    monkeypatch.setattr(plugin, "_DARWIN", False)
    monkeypatch.setattr(plugin, "_PROC", tmp_path / "no-proc")
    monkeypatch.setattr(plugin, "_WRITER", None)
    assert plugin._writer() == {"pid": os.getpid(), "pid_start": None}


# --- macOS (#353): the plugin fingerprints its process through libproc ----------------------


def test_the_plugin_and_the_process_table_share_one_definition() -> None:
    """Hermes' macOS start time is defined once, in the plugin (it cannot import Kelpie);
    the process table that verifies a worker before ending it uses that definition."""
    assert proctable.ProcBsdInfo is plugin.ProcBsdInfo
    assert proctable.birth is plugin.birth
    assert proctable.PROC_PIDTBSDINFO == plugin.PROC_PIDTBSDINFO == 3


def test_plugin_on_macos_writes_what_the_process_table_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a fake libproc everywhere: the plugin's ``pid_start`` of a process is the
    fingerprint Kelpie computes for it, the one a macOS Hermes records (``"|<start>"``)."""
    lib = fake_farm()
    monkeypatch.setattr(plugin, "_DARWIN", True)
    monkeypatch.setattr(plugin, "_PIDINFO", lib.proc_pidinfo)
    table = Libproc(lib)
    epoch, why = table.epoch()
    assert why is None
    for pid in (30, 40):
        p = table.read(pid)
        assert p is not None
        assert plugin._fingerprint(pid) == table.fingerprint(p, epoch) == stamp(lib, pid)
    worker = RunWorker(30, stamp(lib, 30), 40, stamp(lib, 40), "h:1")
    assert writer_matches((40, plugin._fingerprint(40)), worker) is True
    assert plugin._fingerprint(99) is None  # no such process


def test_plugin_on_macos_without_libproc_writes_no_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> object:
        raise OSError("no libproc")

    monkeypatch.setattr(plugin, "_DARWIN", True)
    monkeypatch.setattr(plugin, "_PIDINFO", None)
    monkeypatch.setattr(plugin, "_proc_pidinfo", missing)
    monkeypatch.setattr(plugin, "_WRITER", None)
    assert plugin._writer() == {"pid": os.getpid(), "pid_start": None}


def test_plugin_loads_libproc_once_with_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """libproc.dylib first, libSystem (``CDLL(None)``) when it has no ``proc_pidinfo``; loaded
    once per process; nothing loadable: no fingerprint, no error."""
    import ctypes
    import ctypes.util

    class NoSymbol:
        def __getattr__(self, name: str) -> object:
            raise AttributeError(name)

    class System:
        def __init__(self) -> None:
            self.proc_pidinfo = lambda *a: 0

    loads: list[object] = []

    def cdll(path: object) -> object:
        loads.append(path)
        return System() if path is None else NoSymbol()

    monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
    monkeypatch.setattr(ctypes, "CDLL", cdll)
    monkeypatch.setattr(plugin, "_PIDINFO", None)
    first = plugin._proc_pidinfo()
    assert plugin._proc_pidinfo() is first and loads == ["/usr/lib/libproc.dylib", None]
    assert plugin.darwin_start(os.getpid()) is None  # a short read: no start

    def broken(path: object) -> object:
        raise OSError("no library")

    monkeypatch.setattr(ctypes, "CDLL", broken)
    monkeypatch.setattr(plugin, "_PIDINFO", None)
    assert plugin.darwin_start(os.getpid()) is None


def test_the_plugin_source_parses_on_python_3_8() -> None:
    """Hermes loads the plugin in its own Python; the floor is 3.8, like the other hooks.
    (Syntax only: newer runtime APIs are kept out by review.)"""
    import ast

    ast.parse(Path(plugin.__file__).read_text(), feature_version=(3, 8))


WRITER_CHILD = """
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location("hermes_plugins.kelpie", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module._writer()), flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(host_table().unavailable() is not None, reason="needs /proc or libproc")
def test_a_live_plugin_process_writes_the_fingerprint_kelpie_verifies(tmp_path: Path) -> None:
    """The installed plugin file, alone in another Python process (as Hermes loads it): its
    lines' writer is what Kelpie's process table reads for that process, on Linux and on
    macOS (CI's macos job), so the run's worker passes the writer check (#353)."""
    installed = tmp_path / "plugins" / "kelpie" / "__init__.py"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(Path(plugin.__file__).read_bytes())
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", WRITER_CHILD, str(installed)],
        stdout=subprocess.PIPE,
        cwd=tmp_path,
    )
    try:
        assert child.stdout is not None
        written = json.loads(child.stdout.readline())
        table = host_table()
        epoch, why = table.epoch()
        assert why is None
        p = table.read(child.pid)
        assert p is not None
        expected = table.fingerprint(p, epoch)
        assert written == {"pid": child.pid, "pid_start": expected}
        worker = RunWorker(None, None, child.pid, expected, "h:1")
        assert writer_matches((written["pid"], written["pid_start"]), worker) is True
        if sys.platform == "darwin":
            assert expected.startswith("|") and expected[1:].isdigit()
    finally:
        child.kill()
        child.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc")
def test_on_macos_the_plugin_writes_what_hermes_records() -> None:
    """Hermes on macOS: ``round(process_birth(psutil.Process(pid)) * 100)``, no epoch."""
    psutil = pytest.importorskip("psutil")
    proc = psutil.Process(os.getpid())
    try:
        hermes = round(proc._proc.create_time(monotonic=True) * 100)
    except TypeError:  # a psutil without the unadjusted read
        hermes = round(proc.create_time() * 100)
    assert plugin._fingerprint(os.getpid()) == f"|{hermes}"


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


def test_a_forged_line_with_the_same_call_id_does_not_double_a_call() -> None:
    from kelpie.traces.hooks import merge_hook_spans

    real = hook_and_otlp(5, False, "hermes-run-1")
    forged = [
        replace(s, span_id=f"f{s.span_id}", start=s.start - timedelta(milliseconds=1),
                end=s.start - timedelta(milliseconds=1),
                attributes={**s.attributes, UNVERIFIED_ATTRIBUTE: True})
        for s in real
        if s.span_id.startswith("h")
    ]  # fmt: skip
    folded = merge_hook_spans(forged + real).spans
    verified = [
        s for s in folded if s.kind is SpanKind.TOOL and UNVERIFIED_ATTRIBUTE not in s.attributes
    ]
    assert sorted(s.span_id for s in verified) == ["o0", "o1", "o2", "o3", "o4"]
    snap = Snapshot(
        now=NOW,
        sessions=(Session("hermes-run-1", "T-1", "a", NOW - timedelta(hours=1),
                          last_heartbeat=NOW),),
        spans=folded,
    )  # fmt: skip
    actions = REGISTRY["loop_breaker"]({}).evaluate(snap)
    assert not [a for a in actions if a.kind is ActionKind.STOP]


def test_a_forged_line_closer_in_time_does_not_double_a_call() -> None:
    from kelpie.traces.hooks import merge_hook_spans

    spans = hook_and_otlp(5, False, "hermes-run-1")
    otlp = [replace(s, attributes={}) for s in spans if s.span_id.startswith("o")]  # no call id
    call_id = "gen_ai.tool.call.id"
    real = [
        replace(s, start=s.start + timedelta(milliseconds=40),
                end=s.end + timedelta(milliseconds=40),
                attributes={k: v for k, v in s.attributes.items() if k != call_id})
        for s in spans
        if s.span_id.startswith("h")
    ]  # fmt: skip
    forged = [
        replace(s, span_id=f"f{s.span_id}", start=s.start - timedelta(milliseconds=40),
                end=s.end - timedelta(milliseconds=40),
                attributes={**s.attributes, UNVERIFIED_ATTRIBUTE: True})
        for s in real
    ]  # fmt: skip
    folded = merge_hook_spans(forged + real + otlp).spans
    verified = [
        s for s in folded if s.kind is SpanKind.TOOL and UNVERIFIED_ATTRIBUTE not in s.attributes
    ]
    assert sorted(s.span_id for s in verified) == ["o0", "o1", "o2", "o3", "o4"]
