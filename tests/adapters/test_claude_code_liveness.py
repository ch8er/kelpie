"""Liveness of Claude Code sessions from their process (idle sessions are not ended)."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.claude_code import ClaudeCodeAdapter, hook
from kelpie.adapters.claude_code.adapter import WATCH_ENDED, WATCH_UNKNOWN, parse_line
from kelpie.adapters.claude_code.process import process_state
from kelpie.core.actions import ActionKind, ActionResult
from kelpie.core.ports import UnsupportedAction
from kelpie.rules.exit_contract import ExitContract

REAL_PROC = Path("/proc")
linux = pytest.mark.skipif(not (REAL_PROC / "self" / "stat").exists(), reason="needs /proc")
S1 = "0b5c6f7e-0000-4000-8000-000000000001"
NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
ME = {"host": "agent-host", "uid": 1000, "pidns": "pid:[4026531836]", "boot": "b-1"}


@pytest.fixture
def sleeper() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    yield proc
    proc.kill()
    proc.wait()


def _fake_proc(
    root: Path, pid: int, name: bytes, ppid: int, start: int = 1234, state: str = "S"
) -> None:
    (root / str(pid)).mkdir(parents=True)
    (root / str(pid) / "comm").write_bytes(name + b"\n")
    rest = " ".join([state, str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 5)
    (root / str(pid) / "stat").write_bytes(b"%d (%s) %s\n" % (pid, name, rest.encode()))


# --- the hook ------------------------------------------------------------------------------


def test_claude_pid_counts_only_as_an_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(tmp_path, 300, b"sh", 200)
    _fake_proc(tmp_path, 200, b"claude", 100, start=4242)
    _fake_proc(tmp_path, 100, b"tmux", 1)
    _fake_proc(tmp_path, 777, b"claude", 1, start=9)  # another session's process
    monkeypatch.setattr(os, "getppid", lambda: 300)
    monkeypatch.setenv("CLAUDE_PID", "200")
    assert hook.claude_process(tmp_path)["pid"] == 200
    monkeypatch.setenv("CLAUDE_PID", "777")  # inherited from elsewhere: not trusted
    found = hook.claude_process(tmp_path)
    assert (found["pid"], found["pid_start"]) == (200, "4242")  # the claude ancestor instead
    monkeypatch.delenv("CLAUDE_PID")
    assert hook.claude_process(tmp_path)["pid"] == 200


def test_an_inner_claude_wins_over_an_inherited_claude_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude (100) > bash (200) > claude (300) > sh (400) > hook, with CLAUDE_PID=100 left
    over from the outer session: the inner session's process is 300."""
    _fake_proc(tmp_path, 400, b"sh", 300)
    _fake_proc(tmp_path, 300, b"claude", 200, start=33)
    _fake_proc(tmp_path, 200, b"bash", 100)
    _fake_proc(tmp_path, 100, b"claude", 1, start=11)
    monkeypatch.setattr(os, "getppid", lambda: 400)
    monkeypatch.setenv("CLAUDE_PID", "100")
    assert hook.claude_process(tmp_path)["pid"] == 300
    monkeypatch.setenv("CLAUDE_PID", "300")
    assert hook.claude_process(tmp_path)["pid"] == 300


def test_no_claude_ancestor_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_proc(tmp_path, 300, b"bash", 1)
    monkeypatch.setattr(os, "getppid", lambda: 300)
    monkeypatch.setenv("CLAUDE_PID", "999")  # not an ancestor, and no claude above
    assert hook.claude_process(tmp_path) == {}


def test_proc_stat_survives_odd_names(tmp_path: Path) -> None:
    _fake_proc(tmp_path, 7, b"a (b) c", 1, start=99)
    _fake_proc(tmp_path, 8, b"\xff\xfeclaude", 1, start=55)  # not UTF-8 (prctl)
    assert hook.proc_start(7, tmp_path) == "99"
    assert hook.proc_start(8, tmp_path) == "55"
    assert hook.proc_start(9, tmp_path) is None
    (tmp_path / "10").mkdir()
    (tmp_path / "10" / "stat").write_bytes(b"garbage")
    assert hook.proc_start(10, tmp_path) is None


def test_a_broken_proc_never_loses_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(proc: Path | None = None) -> dict[str, Any]:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(hook, "claude_process", boom)
    spool = tmp_path / "spool.jsonl"
    monkeypatch.setenv(hook.SPOOL_ENV, str(spool))
    hook.main(io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": S1})))
    assert json.loads(spool.read_text())["type"] == "turn_end"


@linux
def test_identity_of_this_host() -> None:
    found = hook.identity(REAL_PROC)
    assert found["pidns"] and found["pidns"].startswith("pid:[")
    assert found["boot"] and found["uid"] == os.getuid()


@pytest.mark.parametrize("value", [True, "123", 1, 0, -5, 3.5, None])
def test_hostile_pid_values_are_ignored(value: object) -> None:
    line = json.dumps({"type": "prompt", "at": 1.0, "session_id": "s", "pid": value, "uid": True})
    event = parse_line(line, datetime(1970, 1, 1, tzinfo=UTC))
    assert event is not None and event.pid is None and event.uid is None


# --- the check -----------------------------------------------------------------------------


@linux
def test_process_state_on_a_real_process(sleeper: subprocess.Popen[bytes]) -> None:
    local = hook.identity(REAL_PROC)
    start = hook.proc_start(sleeper.pid, REAL_PROC)
    seen = {"pid_start": start, **local}
    assert process_state(sleeper.pid, seen, local, REAL_PROC) is True
    assert process_state(sleeper.pid, {**seen, "pid_start": "1"}, local, REAL_PROC) is False
    for key, other in (("host", "elsewhere"), ("uid", -1), ("pidns", "pid:[1]"), ("boot", "x")):
        assert process_state(sleeper.pid, {**seen, key: other}, local, REAL_PROC) is None, key
    assert process_state(sleeper.pid, {**seen, "boot": None}, local, REAL_PROC) is None
    assert process_state(sleeper.pid, {**seen, "pid_start": None}, local, REAL_PROC) is None
    sleeper.send_signal(signal.SIGKILL)
    for _ in range(100):  # a zombie until reaped: already gone
        if (hook.proc_stat(sleeper.pid, REAL_PROC) or ["?"])[0] == "Z":
            break
        time.sleep(0.02)
    assert process_state(sleeper.pid, seen, local, REAL_PROC) is False
    sleeper.wait()
    assert process_state(sleeper.pid, seen, local, REAL_PROC) is False


def test_process_state_on_fake_proc(tmp_path: Path) -> None:
    _fake_proc(tmp_path, 50, b"claude", 1, start=7)
    _fake_proc(tmp_path, 51, b"claude", 1, start=7, state="Z")
    seen = {"pid_start": "7", **ME}
    assert process_state(50, seen, ME, tmp_path) is True
    assert process_state(51, seen, ME, tmp_path) is False  # zombie
    assert process_state(52, seen, ME, tmp_path) is False  # gone


# --- sessions ------------------------------------------------------------------------------


def _farm(tmp_path: Path, *lines: dict[str, object], **kw: Any) -> ClaudeCodeAdapter:
    folder = tmp_path / "tasks" / "farm"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "1.json").write_text(json.dumps({"id": "1", "status": "in_progress", "owner": "b"}))
    os.utime(folder / "1.json", ((NOW - timedelta(hours=3)).timestamp(),) * 2)
    spool = tmp_path / "spool.jsonl"
    spool.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return ClaudeCodeAdapter(tasks_dir=tmp_path / "tasks", task_lists=("farm",), spool=spool, **kw)


def _lines(pid: int | None = 4321, age: timedelta = timedelta(hours=2)) -> list[dict[str, object]]:
    at = (NOW - age).timestamp()
    base = {"session_id": S1, "v": 1, "task_list": "farm"}
    proc = {"pid": pid, "pid_start": "777", **ME} if pid else {}
    return [
        {**base, **proc, "type": "session_start", "at": at - 60},
        {**base, **proc, "type": "tool", "at": at, "tool": "TaskUpdate", "task": "1",
         "task_status": "in_progress"},
        {**base, **proc, "type": "turn_end", "at": at + 1},
    ]  # fmt: skip


def fixed(answer: bool | None, calls: list[int] | None = None):  # type: ignore[no-untyped-def]
    def check(pid: int, recorded: dict[str, Any]) -> bool | None:
        if calls is not None:
            calls.append(pid)
        return answer

    return check


def test_idle_session_with_a_live_process_stays_alive(tmp_path: Path) -> None:
    """Two hours at its prompt, its process still there: not ended, rule 1 stays quiet."""
    snap = _farm(tmp_path, *_lines(), process_check=fixed(True)).snapshot(NOW)
    (session,) = snap.sessions
    assert (session.ended_at, session.last_heartbeat) == (None, NOW)
    assert ExitContract().evaluate(snap) == []


def test_session_whose_process_is_gone_has_ended(tmp_path: Path) -> None:
    snap = _farm(tmp_path, *_lines(), process_check=fixed(False)).snapshot(NOW)
    (session,) = snap.sessions
    assert session.ended_at == datetime.fromtimestamp(_lines()[-1]["at"], UTC)  # type: ignore[arg-type]
    assert session.exit_reason == "process gone"
    assert [a.kind for a in ExitContract().evaluate(snap)] == [ActionKind.REQUEUE]


def test_gone_is_not_trusted_for_a_recent_line(tmp_path: Path) -> None:
    """A wrong "gone" (another PID namespace that shares the host name...) must not end a
    session that is visibly working."""
    adapter = _farm(tmp_path, *_lines(age=timedelta(minutes=1)), process_check=fixed(False))
    (session,) = adapter.snapshot(NOW).sessions
    assert session.ended_at is None and session.exit_reason is None


def test_unknown_process_keeps_spool_liveness(tmp_path: Path) -> None:
    for adapter in (
        _farm(tmp_path / "a", *_lines(pid=None), process_check=fixed(True)),
        _farm(tmp_path / "b", *_lines(), process_check=fixed(None)),
    ):
        snap = adapter.snapshot(NOW)
        (session,) = snap.sessions
        assert session.ended_at is None and session.last_heartbeat < NOW - timedelta(hours=1)
        assert [a.kind for a in ExitContract().evaluate(snap)] == [ActionKind.REQUEUE]


def test_the_newest_line_decides(tmp_path: Path) -> None:
    """A session resumed in another process: the old pid says nothing about it."""
    old = _lines(pid=1111)
    resumed_without_pid = {"session_id": S1, "v": 1, "type": "prompt",
                           "at": (NOW - timedelta(hours=1)).timestamp()}  # fmt: skip
    calls: list[int] = []
    adapter = _farm(tmp_path / "a", *old, resumed_without_pid, process_check=fixed(False, calls))
    (session,) = adapter.snapshot(NOW).sessions
    assert calls == [] and session.ended_at is None
    resumed = {**resumed_without_pid, "pid": 2222, "pid_start": "9", **ME}
    calls.clear()
    adapter = _farm(tmp_path / "b", *old, resumed, process_check=fixed(True, calls))
    adapter.snapshot(NOW)
    assert calls == [2222]


def test_a_session_end_wins_over_a_live_pid(tmp_path: Path) -> None:
    end = {"session_id": S1, "v": 1, "type": "session_end", "at": NOW.timestamp() - 30,
           "reason": "other", "pid": 4321, "pid_start": "777", **ME}  # fmt: skip
    calls: list[int] = []
    (session,) = (
        _farm(tmp_path, *_lines(), end, process_check=fixed(True, calls)).snapshot(NOW).sessions
    )
    assert session.ended_at is not None and calls == []


def test_running_sessions_stay_watched_beyond_lookback(tmp_path: Path) -> None:
    """Idle for longer than lookback: its lines are gone from the window, but it is still
    watched; when its process ends, rule 1 hears of it."""
    answer: list[bool | None] = [True]
    adapter = _farm(tmp_path, *_lines(), process_check=lambda pid, rec: answer[0])
    assert adapter.snapshot(NOW).sessions[0].last_heartbeat == NOW
    later = NOW + timedelta(hours=30)  # every line is now older than lookback (24h)
    (session,) = adapter.snapshot(later).sessions
    assert (session.ended_at, session.last_heartbeat) == (None, later)
    answer[0] = False
    snap = adapter.snapshot(later + timedelta(minutes=1))
    (session,) = snap.sessions
    assert session.exit_reason == "process gone" and session.ended_at is not None
    assert [a.kind for a in ExitContract().evaluate(snap)] == [ActionKind.REQUEUE]
    assert adapter.snapshot(later + WATCH_ENDED + timedelta(minutes=2)).sessions == ()


def test_gone_watched_session_is_reported_until_watch_ended(tmp_path: Path) -> None:
    answer: list[bool | None] = [True]
    adapter = _farm(tmp_path, *_lines(), process_check=lambda pid, rec: answer[0])
    adapter.snapshot(NOW)
    later = NOW + timedelta(hours=30)
    answer[0] = False
    first = adapter.snapshot(later).sessions
    answer[0] = True  # not asked again: it is gone
    again = adapter.snapshot(later + WATCH_ENDED - timedelta(minutes=1)).sessions
    assert first == again and again[0].exit_reason == "process gone"


def test_unknown_drops_a_watched_session_only_when_it_lasts(tmp_path: Path) -> None:
    """A check that briefly cannot tell (``/proc`` unreadable for a moment) keeps the session
    as last seen; WATCH_UNKNOWN in a row drop it."""
    answer: list[bool | None] = [True]
    adapter = _farm(tmp_path, *_lines(), process_check=lambda pid, rec: answer[0])
    adapter.snapshot(NOW)
    later = NOW + timedelta(hours=30)
    assert adapter.snapshot(later).sessions[0].last_heartbeat == later
    answer[0] = None
    for tick in range(1, WATCH_UNKNOWN):
        (session,) = adapter.snapshot(later + timedelta(minutes=tick)).sessions
        assert (session.ended_at, session.last_heartbeat) == (None, later)
    answer[0] = True  # a good answer resets the count
    adapter.snapshot(later + timedelta(minutes=10))
    answer[0] = None
    assert len(adapter.snapshot(later + timedelta(minutes=11)).sessions) == 1
    for tick in range(12, 12 + WATCH_UNKNOWN):
        sessions = adapter.snapshot(later + timedelta(minutes=tick)).sessions
    assert sessions == ()


def test_stop_leaves_a_dead_session_alone(tmp_path: Path) -> None:
    adapter = _farm(tmp_path, *_lines(), stop_dir=tmp_path / "stop", lookback=timedelta(days=3650))
    adapter.process_check = fixed(False)
    assert adapter.stop(S1, "loop") is ActionResult.NOTHING_TO_DO
    assert not (tmp_path / "stop").exists()
    adapter.process_check = fixed(True)
    adapter.stop(S1, "loop")
    assert (tmp_path / "stop" / f"{S1}.json").exists()


def test_stop_reaches_a_watched_session_beyond_lookback(tmp_path: Path) -> None:
    """Its lines are older than lookback (stop reads the spool at the real now): the watched
    entry stands in for them."""
    answer: list[bool | None] = [True]
    adapter = _farm(
        tmp_path, *_lines(), stop_dir=tmp_path / "stop", process_check=lambda pid, rec: answer[0]
    )
    with pytest.raises(UnsupportedAction):  # never seen running: not known
        adapter.stop(S1, "loop")
    adapter.snapshot(NOW)
    adapter.stop(S1, "loop")
    assert (tmp_path / "stop" / f"{S1}.json").exists()
    (tmp_path / "stop" / f"{S1}.json").unlink()
    answer[0] = False
    adapter.snapshot(NOW + timedelta(hours=30))  # found gone
    assert adapter.stop(S1, "loop") is ActionResult.NOTHING_TO_DO
    assert not (tmp_path / "stop" / f"{S1}.json").exists()


@linux
def test_end_to_end_with_a_real_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeper: subprocess.Popen[bytes]
) -> None:
    """The real hook records a real process under it; Kelpie sees the idle session alive,
    then ended once the process is gone."""
    monkeypatch.setattr(hook, "PROC", REAL_PROC)
    monkeypatch.setattr(hook, "_ancestors", lambda proc=None: [(sleeper.pid, True)])
    spool = tmp_path / "spool.jsonl"
    monkeypatch.setenv(hook.SPOOL_ENV, str(spool))
    hook.main(io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": S1})))
    adapter = ClaudeCodeAdapter(tasks_dir=tmp_path / "tasks", spool=spool)
    later = datetime.now(UTC) + timedelta(hours=1)
    (session,) = adapter.snapshot(later).sessions
    assert (session.ended_at, session.last_heartbeat) == (None, later)
    sleeper.kill()
    sleeper.wait()
    (session,) = adapter.snapshot(later).sessions
    assert session.ended_at is not None and session.exit_reason == "process gone"
