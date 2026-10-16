"""Stopping a Claude Code session: the adapter writes a marker, the hook enforces it."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.claude_code import ClaudeCodeAdapter, hook
from kelpie.adapters.claude_code.adapter import ClaudeCodeError, write_marker
from kelpie.core.actions import ActionKind, ActionResult
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.ports import UnsupportedAction
from kelpie.rules.loop_breaker import LoopBreaker
from tests.fakes import FakeAlerter

S1 = "0b5c6f7e-0000-4000-8000-000000000001"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Spool and stop dir in ``tmp_path``; returns the stop dir."""
    monkeypatch.setenv(hook.SPOOL_ENV, str(tmp_path / "spool.jsonl"))
    monkeypatch.setenv(hook.STOP_DIR_ENV, str(tmp_path / "stop"))
    for var in (hook.STOP_TTL_ENV, "CLAUDE_CODE_TASK_LIST_ID", "KELPIE_TASK_ID", "KELPIE_AGENT"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "stop"


def call(event: str, session: str = S1, **fields: Any) -> tuple[dict[str, Any] | None, int]:
    out = io.StringIO()
    code = hook.main(
        io.StringIO(json.dumps({"hook_event_name": event, "session_id": session, **fields})), out
    )
    text = out.getvalue()
    return (json.loads(text) if text else None), code


def pre_tool(session: str = S1) -> tuple[dict[str, Any] | None, int]:
    return call(
        "PreToolUse", session, tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="t"
    )


def spool(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "spool.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_no_marker_no_output(env: Path, tmp_path: Path) -> None:
    assert pre_tool() == (None, 0)
    assert spool(tmp_path) == []  # PreToolUse is only recorded when it stops


def test_marker_stops_tool_calls(env: Path, tmp_path: Path) -> None:
    write_marker(env, S1, "9 identical `Bash` calls", time.time())
    answer, code = pre_tool()
    assert code == 0 and answer is not None
    assert answer["continue"] is False
    assert answer["stopReason"].startswith("Stopped by Kelpie: 9 identical `Bash` calls.")
    assert str(env / f"{S1}.json") in answer["stopReason"]
    # The model reads only the short reason, never how to lift the stop.
    assert answer["hookSpecificOutput"] == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "Stopped by Kelpie: 9 identical `Bash` calls.",
    }
    assert [(r["type"], r["event"]) for r in spool(tmp_path)] == [("stopped", "PreToolUse")]
    assert pre_tool()[0] is not None  # holds until cleared
    assert pre_tool("other-session") == (None, 0)


@pytest.mark.parametrize("source", ["sdk", "system", "loop_wakeup", "schedule_wakeup", None])
def test_machine_prompts_are_blocked(env: Path, source: str | None) -> None:
    write_marker(env, S1, "budget", time.time())
    fields = {"prompt": "keep going"} | ({"source": source} if source else {})
    answer, _ = call("UserPromptSubmit", **fields)
    assert answer is not None
    assert (answer["continue"], answer["decision"]) == (False, "block")
    assert answer["reason"] == answer["stopReason"]
    assert (env / f"{S1}.json").exists()


def test_a_human_prompt_resumes(env: Path, tmp_path: Path) -> None:
    write_marker(env, S1, "budget", time.time())
    assert call("UserPromptSubmit", prompt="carry on", source="user") == (None, 0)
    assert not (env / f"{S1}.json").exists()
    assert spool(tmp_path)[-1]["type"] == "prompt"
    assert pre_tool() == (None, 0)


def test_session_end_clears_the_marker(env: Path) -> None:
    write_marker(env, S1, "budget", time.time())
    assert call("SessionEnd", reason="other") == (None, 0)
    assert not (env / f"{S1}.json").exists()


def test_stop_does_not_need_a_writable_spool(
    env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_marker(env, S1, "budget", time.time())
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv(hook.SPOOL_ENV, str(blocker / "spool.jsonl"))
    assert pre_tool()[0] is not None


@pytest.mark.parametrize(
    "content",
    [
        lambda now: {"at": now - 25 * 3600, "reason": "old"},  # past the TTL
        lambda now: {"at": now + 3600, "reason": "future"},
        lambda now: {"at": "now", "reason": "x"},
        lambda now: {"at": True, "reason": "x"},
        lambda now: {"at": now, "reason": 5},
        lambda now: ["at", now],
    ],
)
def test_unusable_markers_are_ignored(env: Path, content: Any) -> None:
    env.mkdir()
    (env / f"{S1}.json").write_text(json.dumps(content(time.time())))
    assert pre_tool() == (None, 0)


def test_garbage_and_huge_markers_are_ignored(env: Path, tmp_path: Path) -> None:
    env.mkdir()
    path = env / f"{S1}.json"
    path.write_text("{not json")
    assert pre_tool() == (None, 0)
    path.write_text(json.dumps({"at": time.time(), "reason": "x" * (hook.MAX_MARKER + 10)}))
    assert pre_tool() == (None, 0)


def test_ttl_can_be_shortened(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_marker(env, S1, "budget", time.time() - 120)
    monkeypatch.setenv(hook.STOP_TTL_ENV, "60")
    assert pre_tool() == (None, 0)
    monkeypatch.setenv(hook.STOP_TTL_ENV, "bogus")  # falls back to 24h
    assert pre_tool()[0] is not None


def test_symlinked_marker_is_not_followed(env: Path, tmp_path: Path) -> None:
    env.mkdir()
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"at": time.time(), "reason": "planted"}))
    (env / f"{S1}.json").symlink_to(real)
    assert pre_tool() == (None, 0)


@pytest.mark.parametrize("session", ["../escape", "a/b", "", "x" * 200, "a b", "abc\n"])
def test_odd_session_ids_never_build_a_path(env: Path, tmp_path: Path, session: str) -> None:
    (tmp_path / "escape.json").write_text(json.dumps({"at": time.time(), "reason": "x"}))
    assert hook.marker_path(session) is None
    assert pre_tool(session) == (None, 0)


def test_reason_is_cleaned(env: Path) -> None:
    write_marker(env, S1, "line one\nline\x1b[31m two" + "y" * 600, time.time())
    answer, _ = pre_tool()
    assert answer is not None
    reason = answer["stopReason"]
    assert "\n" not in reason and "\x1b" not in reason and "..." in reason


def test_script_prints_one_json_answer(env: Path, tmp_path: Path) -> None:
    write_marker(env, S1, "budget", time.time())
    copy = tmp_path / "isolated" / "hook.py"
    copy.parent.mkdir()
    copy.write_text(Path(hook.__file__).read_text())
    proc = subprocess.run(
        [sys.executable, "-I", str(copy)],
        input=json.dumps({"hook_event_name": "PreToolUse", "session_id": S1, "tool_name": "Bash"}),
        capture_output=True,
        text=True,
        env={
            hook.SPOOL_ENV: str(tmp_path / "spool.jsonl"),
            hook.STOP_DIR_ENV: str(env),
            "PATH": "/usr/bin:/bin",
        },
        timeout=30,
        check=False,
    )
    assert (proc.returncode, proc.stderr) == (0, "")
    assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- the adapter --------------------------------------------------------------------------


def adapter(tmp_path: Path, **kw: Any) -> ClaudeCodeAdapter:
    kw.setdefault("stop_dir", tmp_path / "stop")
    return ClaudeCodeAdapter(tasks_dir=tmp_path / "tasks", spool=tmp_path / "spool.jsonl", **kw)


def test_adapter_stop_writes_a_private_marker(env: Path, tmp_path: Path) -> None:
    call("SessionStart", source="startup")
    done = adapter(tmp_path).stop(S1, "too many calls")
    assert done == (
        "the session stops at its next tool call, until a human types a prompt in it or "
        "the stop expires"
    )
    path = env / f"{S1}.json"
    data = json.loads(path.read_text())
    assert data["reason"] == "too many calls" and abs(data["at"] - time.time()) < 60
    assert path.stat().st_mode & 0o777 == 0o600
    assert env.stat().st_mode & 0o777 == 0o700
    assert [p.name for p in env.iterdir()] == [f"{S1}.json"]  # no temp file left
    adapter(tmp_path).stop(S1, "again")  # idempotent: replaced
    assert json.loads(path.read_text())["reason"] == "again"


def test_adapter_leaves_an_ended_session_alone(env: Path, tmp_path: Path) -> None:
    call("SessionStart", source="startup")
    call("SessionEnd", reason="other")
    assert adapter(tmp_path).stop(S1, "late") is ActionResult.NOTHING_TO_DO
    assert not env.exists()


def test_adapter_refuses_unknown_sessions(env: Path, tmp_path: Path) -> None:
    with pytest.raises(UnsupportedAction, match="not in the hooks spool"):
        adapter(tmp_path).stop(S1, "x")
    with pytest.raises(ClaudeCodeError, match="not a Claude Code session id"):
        adapter(tmp_path).stop("../etc/passwd", "x")
    call("SessionStart", source="startup")
    with pytest.raises(UnsupportedAction, match="turned off"):
        adapter(tmp_path, stop_dir=None).stop(S1, "x")


def test_adapter_refuses_a_symlinked_stop_dir(env: Path, tmp_path: Path) -> None:
    call("SessionStart", source="startup")
    (tmp_path / "real").mkdir()
    env.symlink_to(tmp_path / "real")
    with pytest.raises(ClaudeCodeError, match="cannot write"):
        adapter(tmp_path).stop(S1, "x")
    assert list((tmp_path / "real").iterdir()) == []


def test_stopped_session_says_so(env: Path, tmp_path: Path) -> None:
    call("SessionStart", source="startup")
    adapter(tmp_path).stop(S1, "x")
    pre_tool()
    (session,) = adapter(tmp_path).snapshot(datetime.now(UTC)).sessions
    assert session.exit_reason == "stopped by kelpie"
    call("UserPromptSubmit", prompt="go on", source="user")
    (session,) = adapter(tmp_path).snapshot(datetime.now(UTC)).sessions
    assert session.exit_reason is None


def test_end_to_end_runaway_is_stopped(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 10 sees a loop in the hook spans, stops the session through the adapter, and the
    hook refuses the session's next tool call."""
    now = time.time()
    for i in range(10):
        monkeypatch.setattr(hook.time, "time", lambda i=i: now - 300 + i * 20)
        hook.main(
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "PostToolUse",
                        "session_id": S1,
                        "tool_name": "Bash",
                        "tool_input": {"command": "gh run view 42"},
                        "tool_response": {"stdout": "in_progress"},
                        "tool_use_id": f"toolu_{i}",
                        "duration_ms": 900,
                    }
                )
            )
        )
    monkeypatch.undo()
    monkeypatch.setenv(hook.SPOOL_ENV, str(tmp_path / "spool.jsonl"))
    monkeypatch.setenv(hook.STOP_DIR_ENV, str(env))
    cc = adapter(tmp_path)
    snapshot = cc.snapshot(datetime.fromtimestamp(now, UTC))
    alerter = FakeAlerter()
    engine = Engine(
        [LoopBreaker()],
        adapters=[cc],
        alerter=alerter,
        config=EngineConfig(dry_run=False, default_mode=Mode.ACT),
    )
    results = engine.execute(engine.evaluate(snapshot), snapshot)
    stops = [r for r in results if r.action.kind is ActionKind.STOP]
    assert [r.outcome for r in stops] == [Outcome.EXECUTED]
    answer, _ = pre_tool()
    assert answer is not None and answer["continue"] is False
    assert "identical consecutive `Bash` calls" in answer["stopReason"]
    assert datetime.fromtimestamp(now, UTC) - snapshot.sessions[0].started_at < timedelta(minutes=6)
    assert os.path.exists(env / f"{S1}.json")


def test_answer_survives_a_closed_reader(env: Path, tmp_path: Path) -> None:
    """The answer is written unbuffered: a reader that went away is not a non-zero exit."""
    write_marker(env, S1, "budget", time.time())
    copy = tmp_path / "isolated" / "hook.py"
    copy.parent.mkdir()
    copy.write_text(Path(hook.__file__).read_text())
    payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": S1})
    proc = subprocess.run(
        ["bash", "-c", f'{sys.executable} -I {copy} | true; echo "${{PIPESTATUS[0]}}"'],
        input=payload,
        capture_output=True,
        text=True,
        env={
            hook.SPOOL_ENV: str(tmp_path / "spool.jsonl"),
            hook.STOP_DIR_ENV: str(env),
            "PATH": "/usr/bin:/bin",
        },
        timeout=30,
        check=False,
    )
    assert proc.stdout.strip() == "0"


def test_old_hooks_cannot_be_stopped(env: Path, tmp_path: Path) -> None:
    """A session whose hook predates stop support (no ``v`` in its lines) is refused, so a
    human is alerted instead of a marker nobody reads."""
    (tmp_path / "spool.jsonl").write_text(
        json.dumps({"type": "prompt", "at": time.time() - 60, "session_id": S1}) + "\n"
    )
    with pytest.raises(UnsupportedAction, match="without stop support"):
        adapter(tmp_path).stop(S1, "x")
    assert not env.exists()


def test_unenforced_stop_is_visible(
    env: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Tool calls going on well after the marker, with no refusal recorded: the stop is not
    enforced (PreToolUse not registered, another user, another stop dir...)."""
    now = time.time()
    lines = [
        {"type": "session_start", "at": now - 900, "session_id": S1, "v": 1},
        {"type": "tool", "at": now - 60, "session_id": S1, "v": 1, "tool": "Bash"},
    ]
    (tmp_path / "spool.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    write_marker(env, S1, "loop", now - 600)
    snap = adapter(tmp_path).snapshot(datetime.fromtimestamp(now, UTC))
    assert snap.sessions[0].exit_reason == "stop not enforced"
    assert "still calls tools" in caplog.text
    # A refusal after the marker means it works; a tool call within the grace is fine too.
    with (tmp_path / "spool.jsonl").open("a") as f:
        f.write(json.dumps({"type": "stopped", "at": now - 30, "session_id": S1, "v": 1}) + "\n")
    snap = adapter(tmp_path).snapshot(datetime.fromtimestamp(now, UTC))
    assert snap.sessions[0].exit_reason == "stopped by kelpie"
    write_marker(env, S1, "loop", now - 90)
    (tmp_path / "spool.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    snap = adapter(tmp_path).snapshot(datetime.fromtimestamp(now, UTC))
    assert snap.sessions[0].exit_reason is None


def test_long_calls_and_expired_markers_are_not_misses(env: Path, tmp_path: Path) -> None:
    """A call started before the stop and finishing after the grace is not a miss, and nor is
    a call after the marker expired (the hook ignores it by then)."""
    now = time.time()
    long_call = {
        "type": "tool",
        "at": now - 60,
        "session_id": S1,
        "v": 1,
        "tool": "Bash",
        "duration_ms": 400_000,
    }
    (tmp_path / "spool.jsonl").write_text(json.dumps(long_call) + "\n")
    write_marker(env, S1, "loop", now - 300)
    snap = adapter(tmp_path).snapshot(datetime.fromtimestamp(now, UTC))
    assert snap.sessions[0].exit_reason is None
    late = {"type": "tool", "at": now - 60, "session_id": S1, "v": 1, "tool": "Bash"}
    (tmp_path / "spool.jsonl").write_text(json.dumps(late) + "\n")
    write_marker(env, S1, "loop", now - hook.DEFAULT_STOP_TTL - 3600)
    snap = adapter(tmp_path, lookback=timedelta(days=3)).snapshot(datetime.fromtimestamp(now, UTC))
    assert snap.sessions[0].exit_reason is None
