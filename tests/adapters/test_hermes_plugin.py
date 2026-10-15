from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.hermes import plugin


class Ctx:
    def __init__(self) -> None:
        self.hooks: dict[str, Any] = {}

    def register_hook(self, name: str, fn: Any) -> None:
        self.hooks[name] = fn


@pytest.fixture
def spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sub" / "spool.jsonl"
    monkeypatch.setenv(plugin.SPOOL_ENV, str(path))
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(plugin.COMMANDS_ENV, raising=False)
    monkeypatch.delenv(plugin.MAX_BYTES_ENV, raising=False)
    return path


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_register_hooks() -> None:
    ctx = Ctx()
    plugin.register(ctx)
    assert set(ctx.hooks) == {"post_tool_call", "on_session_end"}
    assert ctx.hooks["post_tool_call"] is plugin.post_tool_call
    assert ctx.hooks["on_session_end"] is plugin.on_session_end


def test_tool_call_line(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    args = {"command": "pytest -q", "timeout": 30}
    plugin.post_tool_call(
        tool_name="terminal",
        args=args,
        result={"stdout": "SECRET OUTPUT"},
        task_id="internal-uuid",
        session_id="sess-1",
        duration_ms=250,
        status="ok",
    )
    (rec,) = lines(spool)
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    assert rec["type"] == "tool" and isinstance(rec["at"], float)
    assert rec["task_id"] == "t_1" and rec["run_id"] == "42" and rec["session_id"] == "sess-1"
    assert rec["tool"] == "terminal" and rec["outcome"] == "ok" and rec["duration_ms"] == 250
    assert rec["args_hash"] == hashlib.sha256(canonical.encode()).hexdigest()[:16]
    assert rec["command"] == "pytest -q"


def test_ids_from_kwargs_without_kanban_env(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SESSION_ID", "env-sess")
    plugin.post_tool_call(tool_name="read_file", args={"path": "a"}, task_id="x", run_id=3)
    (rec,) = lines(spool)
    assert rec["task_id"] == "x" and rec["run_id"] == "3" and rec["session_id"] == "env-sess"
    assert rec["command"] is None and rec["duration_ms"] is None


def test_error_outcome_and_params_alias(spool: Path) -> None:
    plugin.post_tool_call(tool_name="terminal", params={"cmd": "false"}, status="error")
    (rec,) = lines(spool)
    assert rec["outcome"] == "error" and rec["command"] == "false"


def test_args_and_results_are_never_stored(spool: Path) -> None:
    plugin.post_tool_call(
        tool_name="write_file",
        args={"path": "/home/x/secret.txt", "content": "TOP-SECRET-TOKEN"},
        result="RESULT-SECRET",
        status="ok",
    )
    text = spool.read_text()
    assert "TOP-SECRET-TOKEN" not in text and "RESULT-SECRET" not in text
    assert "secret.txt" not in text
    assert lines(spool)[0]["command"] is None  # no shell command arg


def test_same_args_same_hash_regardless_of_key_order(spool: Path) -> None:
    plugin.post_tool_call(tool_name="t", args={"a": 1, "b": 2})
    plugin.post_tool_call(tool_name="t", args={"b": 2, "a": 1})
    plugin.post_tool_call(tool_name="t", args={"a": 1, "b": 3})
    first, second, third = lines(spool)
    assert first["args_hash"] == second["args_hash"] != third["args_hash"]


def test_command_is_truncated_and_can_be_disabled(
    spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin.post_tool_call(tool_name="terminal", args={"command": "x" * 500})
    assert len(lines(spool)[0]["command"]) == plugin.COMMAND_LIMIT
    monkeypatch.setenv(plugin.COMMANDS_ENV, "0")
    plugin.post_tool_call(tool_name="terminal", args={"command": "ls"})
    assert lines(spool)[1]["command"] is None


def test_session_end_line(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_1")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "9")
    plugin.on_session_end(session_id="sess", completed=True, interrupted="maybe", messages=["x"])
    (rec,) = lines(spool)
    assert rec["type"] == "session_end" and rec["task_id"] == "t_1" and rec["run_id"] == "9"
    assert rec["completed"] is True and rec["interrupted"] is None
    assert "messages" not in rec


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"args": object(), "tool_name": object(), "duration_ms": "fast", "status": 5},
        {"args": {"command": 5}, "task_id": 7},
        {"args": float("nan")},
        {"args": {1: {2: {3: set()}}}},
    ],
)
def test_never_raises_on_garbage(spool: Path, kwargs: dict[str, Any]) -> None:
    plugin.post_tool_call(**kwargs)
    plugin.on_session_end(**kwargs)
    for rec in lines(spool):  # whatever was written is valid JSON
        assert rec["type"] in {"tool", "session_end"}


def test_never_raises_when_spool_is_unwritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(plugin.SPOOL_ENV, str(blocker / "spool.jsonl"))  # parent is a file
    plugin.post_tool_call(tool_name="t", args={})
    plugin.on_session_end(session_id="s")


def test_rotation_at_size_cap(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(plugin.MAX_BYTES_ENV, "300")
    for i in range(10):
        plugin.post_tool_call(tool_name=f"tool{i}", args={"i": i})
    rotated = spool.with_name("spool.jsonl.1")
    assert rotated.exists() and spool.exists()
    assert spool.stat().st_size < 300 + 400  # one line past the cap at most
    tools = [r["tool"] for r in lines(rotated)[-1:] + lines(spool)]
    assert tools[-1] == "tool9"
    assert len(lines(spool)) < 10


def test_bad_max_bytes_falls_back_to_default(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(plugin.MAX_BYTES_ENV, "lots")
    plugin.post_tool_call(tool_name="t", args={})
    assert not spool.with_name("spool.jsonl.1").exists() and len(lines(spool)) == 1


def test_default_spool_location(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(plugin.SPOOL_ENV, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert plugin.spool_path() == tmp_path / ".hermes" / "kelpie" / "spool.jsonl"


def test_plugin_is_standalone() -> None:
    """Hermes loads the file outside the Kelpie environment: no kelpie imports."""
    tree = ast.parse(Path(plugin.__file__).read_text())
    modules = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    assert not {m for m in modules if m and m.startswith("kelpie")}
