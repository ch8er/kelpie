"""The Claude Code command hook: spool lines from hook inputs, privacy, never failing."""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.claude_code import hook
from kelpie.traces.args import args_hash
from tests.traces.test_redaction import LEAKS

ENV = ("CLAUDE_CODE_TASK_LIST_ID", "KELPIE_TASK_ID", "KELPIE_AGENT")


@pytest.fixture
def spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sub" / "spool.jsonl"
    monkeypatch.setenv(hook.SPOOL_ENV, str(path))
    for var in (*ENV, hook.COMMANDS_ENV, hook.MAX_BYTES_ENV):
        monkeypatch.delenv(var, raising=False)
    return path


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def run(data: Any) -> int:
    raw = data if isinstance(data, str) else json.dumps(data)
    return hook.main(io.StringIO(raw))


def base(event: str, **fields: Any) -> dict[str, Any]:
    return {
        "hook_event_name": event,
        "session_id": "0b5c6f7e-0000-4000-8000-000000000001",
        "transcript_path": "/home/agent/.claude/projects/x/0b5c.jsonl",
        "cwd": "/work/repo",
        "permission_mode": "default",
        **fields,
    }


def test_tool_call(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_TASK_LIST_ID", "farm")
    monkeypatch.setenv("KELPIE_AGENT", "builder-1")
    args = {"command": "GH_TOKEN=abc gh pr checks 5", "description": "check CI"}
    assert (
        run(
            base(
                "PostToolUse",
                tool_name="Bash",
                tool_input=args,
                tool_response={"stdout": "SECRET OUTPUT", "stderr": ""},
                tool_use_id="toolu_01",
                duration_ms=250,
                agent_id="sub-1",
            )
        )
        == 0
    )
    (rec,) = lines(spool)
    assert rec == {
        "type": "tool",
        "at": rec["at"],
        "session_id": "0b5c6f7e-0000-4000-8000-000000000001",
        "v": hook.HOOK_VERSION,
        "agent_id": "sub-1",
        "task_list": "farm",
        "kelpie_task": None,
        "agent": "builder-1",
        "tool": "Bash",
        "tool_call_id": "toolu_01",
        "args_hash": args_hash(args),
        "command": "gh pr checks 5",
        "outcome": "ok",
        "duration_ms": 250,
        "output_chars": len(
            json.dumps({"stdout": "SECRET OUTPUT", "stderr": ""}, separators=(",", ":"))
        ),
    }
    text = spool.read_text()
    for secret in ("SECRET", "abc", "check CI", "transcript", "/work/repo"):
        assert secret not in text


def test_failed_tool_call(spool: Path) -> None:
    run(
        base(
            "PostToolUseFailure",
            tool_name="Read",
            tool_input={"file_path": "/etc/shadow"},
            tool_use_id="toolu_02",
            error="EACCES: /etc/shadow",
            is_interrupt=False,
        )
    )
    (rec,) = lines(spool)
    assert rec["outcome"] == "error" and rec["output_chars"] is None
    assert rec["command"] is None
    assert "shadow" not in spool.read_text()


def test_task_update_records_ids_only(spool: Path) -> None:
    run(
        base(
            "PostToolUse",
            tool_name="TaskUpdate",
            tool_input={
                "taskId": "3",
                "status": "in_progress",
                "owner": "builder-1",
                "description": "PRIVATE PLAN",
            },
            tool_response={"success": True},
            tool_use_id="toolu_03",
        )
    )
    (rec,) = lines(spool)
    assert (rec["task"], rec["task_status"], rec["task_owner"]) == ("3", "in_progress", "builder-1")
    assert "PRIVATE" not in spool.read_text()


def test_task_update_with_odd_values(spool: Path) -> None:
    run(
        base(
            "PostToolUse",
            tool_name="TaskUpdate",
            tool_input={"taskId": 7, "status": "exploded", "owner": ["x"]},
            tool_use_id="t",
        )
    )
    (rec,) = lines(spool)
    assert (rec["task"], rec["task_status"], rec["task_owner"]) == ("7", None, None)


def test_failed_task_update_is_not_a_status(spool: Path) -> None:
    run(
        base(
            "PostToolUseFailure",
            tool_name="TaskUpdate",
            tool_input={"taskId": "3", "status": "completed"},
            tool_use_id="t",
            error="no such task",
        )
    )
    (rec,) = lines(spool)
    assert "task_status" not in rec


@pytest.mark.parametrize(
    ("event", "fields", "expected"),
    [
        (
            "SessionStart",
            {"source": "startup", "model": "claude-x", "agent_type": "reviewer"},
            {
                "type": "session_start",
                "source": "startup",
                "model": "claude-x",
                "agent_type": "reviewer",
            },
        ),
        (
            "UserPromptSubmit",
            {"prompt": "PRIVATE PROMPT", "source": "sdk"},
            {"type": "prompt", "source": "sdk"},
        ),
        (
            "Stop",
            {"stop_hook_active": False, "last_assistant_message": "PRIVATE"},
            {"type": "turn_end", "error": None},
        ),
        (
            "StopFailure",
            {"error": "rate_limit", "error_details": "PRIVATE"},
            {"type": "turn_end", "error": "rate_limit"},
        ),
        (
            "SessionEnd",
            {"reason": "prompt_input_exit"},
            {"type": "session_end", "reason": "prompt_input_exit"},
        ),
    ],
)
def test_session_events(
    spool: Path, event: str, fields: dict[str, Any], expected: dict[str, Any]
) -> None:
    run(base(event, **fields))
    (rec,) = lines(spool)
    assert {k: rec[k] for k in expected} == expected
    assert "PRIVATE" not in spool.read_text()


@pytest.mark.parametrize(
    "data",
    [
        "not json",
        "[]",
        json.dumps(base("PreCompact", trigger="auto")),  # not recorded
        json.dumps({"hook_event_name": "Stop"}),  # no session
        json.dumps(base("Stop", session_id="")),
        "",
    ],
)
def test_ignored_inputs(spool: Path, data: str) -> None:
    assert run(data) == 0
    assert not spool.exists()


def test_never_raises_when_the_spool_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv(hook.SPOOL_ENV, str(blocker / "spool.jsonl"))
    assert run(base("Stop", stop_hook_active=False)) == 0


def test_oversized_input_is_skipped(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook, "MAX_INPUT", 10)
    assert run(base("Stop")) == 0
    assert not spool.exists()


def test_long_ids_are_cut(spool: Path) -> None:
    run(base("SessionEnd", session_id="s" * 500, reason="r" * 500))
    (rec,) = lines(spool)
    assert len(rec["session_id"]) == len(rec["reason"]) == hook.MAX_ID


def test_commands_can_be_turned_off(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hook.COMMANDS_ENV, "0")
    run(base("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="t"))
    assert lines(spool)[0]["command"] is None


def test_spool_rotates_and_is_private(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hook.MAX_BYTES_ENV, "1")
    run(base("Stop"))
    run(base("SessionEnd", reason="other"))
    assert [r["type"] for r in lines(spool.with_name("spool.jsonl.1"))] == ["turn_end"]
    assert [r["type"] for r in lines(spool)] == ["session_end"]
    assert spool.stat().st_mode & 0o777 == 0o600


def test_default_spool_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(hook.SPOOL_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert hook.spool_path() == tmp_path / ".claude" / "kelpie" / "spool.jsonl"


def test_runs_as_a_script_without_kelpie(spool: Path, tmp_path: Path) -> None:
    """Claude Code runs the copied file with a plain python3: no kelpie on the path, no
    output on stdout (some events add stdout to the agent's context), exit 0."""
    copy = tmp_path / "isolated" / "hook.py"
    copy.parent.mkdir()
    copy.write_text(Path(hook.__file__).read_text())
    proc = subprocess.run(
        [sys.executable, "-I", str(copy)],
        input=json.dumps(base("SessionEnd", reason="other")),
        capture_output=True,
        text=True,
        env={"KELPIE_CLAUDE_SPOOL": str(spool), "PATH": "/usr/bin:/bin"},
        timeout=30,
        check=False,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")
    assert lines(spool)[0]["reason"] == "other"


def _module_imports(path: str) -> set[str]:
    tree = ast.parse(Path(path).read_text())
    return {
        (n.module if isinstance(n, ast.ImportFrom) else a.name) or ""
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }


def test_hook_is_standalone() -> None:
    assert not {m for m in _module_imports(hook.__file__) if m.startswith("kelpie")}


def test_hook_avoids_newer_syntax() -> None:
    """``python3`` on an agent host may be older than Kelpie's Python: no ``X | Y`` in
    ``isinstance``, no ``match`` (PEP 604/634 need 3.10)."""
    source = Path(hook.__file__).read_text()
    ast.parse(source, feature_version=(3, 8))  # rejects any syntax newer than 3.8
    tree = ast.parse(source)
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Match)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "isinstance":
            assert not isinstance(node.args[1], ast.BinOp)


@pytest.mark.parametrize(("command", "secret"), LEAKS)
def test_secrets_never_reach_the_spool(spool: Path, command: str, secret: str) -> None:
    """The hook redacts with the shared redaction (tests/traces/test_redaction.py)."""
    run(base("PostToolUse", tool_name="Bash", tool_input={"command": command}, tool_use_id="t"))
    assert secret not in spool.read_text()


def test_long_lines_stay_cheap() -> None:
    start = time.monotonic()
    assert hook.redact_command("echo " + "A" * 2_000_000) == "echo ***"
    assert time.monotonic() - start < 2


def test_poll_patterns_still_match() -> None:
    """Rules 2 and 10 match commands by substrings such as ``gh pr checks``."""
    for command in ("gh pr checks 12 --watch", "gh run watch 99", "gh pr view 3 --json state"):
        redacted = hook.redact_command(command) or ""
        assert redacted.startswith(" ".join(command.split()[:3]))


def test_task_list_defaults_to_the_session(spool: Path) -> None:
    run(base("PostToolUse", tool_name="TaskUpdate", tool_input={"taskId": "1"}, tool_use_id="t"))
    assert lines(spool)[0]["task_list"] == "0b5c6f7e-0000-4000-8000-000000000001"


def test_rotation_happens_once(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two sessions that both saw a full spool rotate it once: the second finds it small."""
    spool.parent.mkdir(parents=True)
    spool.write_text("x" * 100)
    hook._rotate(spool, 50)
    spool.write_text("y")
    hook._rotate(spool, 50)  # stale decision would replace .1 with the fresh spool
    assert spool.with_name("spool.jsonl.1").read_text() == "x" * 100


def test_fifo_spool_does_not_block(spool: Path) -> None:
    spool.parent.mkdir(parents=True)
    os.mkfifo(spool)
    start = time.monotonic()
    assert run(base("Stop")) == 0
    assert time.monotonic() - start < 2


def test_symlinked_spool_is_not_followed(spool: Path, tmp_path: Path) -> None:
    spool.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("")
    spool.symlink_to(target)
    assert run(base("Stop")) == 0
    assert target.read_text() == ""


@pytest.mark.parametrize("args", [{"command": "ls -la"}, {}, None, ["a", 1], {"b": 2, "a": [1]}])
def test_args_hash_matches_otlp_ingest(args: Any) -> None:
    assert hook.args_hash(args) == args_hash(args)
