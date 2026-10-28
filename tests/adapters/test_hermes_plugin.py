from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.hermes import plugin
from tests.traces.test_redaction import LEAKS


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
        tool_call_id="call_7",
        duration_ms=250,
        status="ok",
    )
    (rec,) = lines(spool)
    assert rec["tool_call_id"] == "call_7"
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
    assert rec["type"] == "tool" and isinstance(rec["at"], float)
    assert rec["task_id"] == "t_1" and rec["run_id"] == "42" and rec["session_id"] == "sess-1"
    assert rec["tool"] == "terminal" and rec["outcome"] == "ok" and rec["duration_ms"] == 250
    assert rec["args_hash"] == hashlib.sha256(canonical.encode()).hexdigest()[:16]
    assert rec["command"] == "pytest -q"


def test_outside_a_kanban_worker_task_id_is_none(
    spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_SESSION_ID", "env-sess")
    # Hermes' own ``task_id`` keyword is an internal id, not a kanban task.
    plugin.post_tool_call(
        tool_name="read_file", args={"path": "a"}, task_id="x", run_id=3, tool_call_id=""
    )
    (rec,) = lines(spool)
    assert rec["task_id"] is None and rec["run_id"] == "3" and rec["session_id"] == "env-sess"
    assert rec["command"] is None and rec["duration_ms"] is None
    assert rec["tool_call_id"] is None  # Hermes passes "" when there is none


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


def test_command_is_capped_and_can_be_disabled(
    spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin.post_tool_call(tool_name="terminal", args={"command": "ls " + "a" * 10_000})
    assert lines(spool)[0]["command"] == "ls ***"
    monkeypatch.setenv(plugin.COMMANDS_ENV, "0")
    plugin.post_tool_call(tool_name="terminal", args={"command": "ls"})
    assert lines(spool)[1]["command"] is None


# (command, secret that must not reach the spool)
SECRET_COMMANDS = [
    ("GH_TOKEN=ghp_abc123SECRET gh pr checks 5", "ghp_abc123SECRET"),
    ('curl -H "Authorization: Bearer xyzSECRETxyz" https://api.example.test/x', "xyzSECRETxyz"),
    ("curl -H 'Authorization:Bearer glued-SECRET' https://api.example.test", "glued-SECRET"),
    ("git clone https://user:pa55SECRET@host.example.test/repo", "pa55SECRET"),
    ("tool --token s3cretVALUE run", "s3cretVALUE"),
    ("tool --password=hunter2SECRET run", "hunter2SECRET"),
    ("mysql -p hunter3SECRET db", "hunter3SECRET"),
    ("mysql -phunter4SECRET db", "hunter4SECRET"),
    ("env API_KEY=k3yvalueSECRET make test", "k3yvalueSECRET"),
    ("curl https://api.example.test/v1?access_token=qs5ECRETq&x=1", "qs5ECRETq"),
    ("deploy Zx9Qe7Lm2Vb4Nc6Ty8Uh1Jk3ABC", "Zx9Qe7Lm2Vb4Nc6Ty8Uh1Jk3"),
    ('git commit -m "rotate token abcSECRET"', "abcSECRET"),
    ("echo hi\nexport TOKEN=second-line-SECRET", "second-line-SECRET"),
    ("export TOKEN=zzSECRETzz; gh pr checks", "zzSECRETzz"),
]


@pytest.mark.parametrize(("command", "secret"), SECRET_COMMANDS + LEAKS)
def test_secrets_never_reach_the_spool(spool: Path, command: str, secret: str) -> None:
    plugin.post_tool_call(tool_name="terminal", args={"command": command})
    assert secret not in spool.read_text()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("gh pr checks 5", "gh pr checks 5"),
        ("GH_TOKEN=abc gh pr checks 5", "gh pr checks 5"),
        ("  pytest -q tests/test_x.py -k foo\nsecond line", "pytest -q *** -k"),
        ("git status", "git status"),
        ('curl -H "Authorization: Bearer xyz" https://h.test', "curl -H *** ***"),
        ("git clone https://user:pass@host.example.test/repo", "git clone ***"),
    ],
)
def test_redaction_keeps_commands_recognizable(command: str, expected: str) -> None:
    assert plugin.redact_command(command) == expected


def test_poll_patterns_still_match_redacted_commands(spool: Path) -> None:
    plugin.post_tool_call(
        tool_name="terminal", args={"command": "GH_TOKEN=abc123 gh pr checks 5 --watch"}
    )
    assert lines(spool)[0]["command"].startswith("gh pr checks")


def test_redact_empty() -> None:
    assert plugin.redact_command("") is None
    assert plugin.redact_command("FOO=bar") is None


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


@pytest.mark.parametrize(
    ("result", "args", "expected"),
    [
        # The farm's sample (#229): Hermes' terminal result, failed and successful alike.
        ('{"output": "BUILD OK", "exit_code": 0, "error": null}', {}, {"exit_code": 0}),
        ('{"output": "x: not found", "exit_code": 127, "error": null}', {}, {"exit_code": 127}),
        ({"output": "timed out", "exit_code": 124, "error": None}, {}, {"exit_code": 124}),
        (
            '{"output": "", "exit_code": -1, "status": "blocked", "error": "refused"}',
            {},
            {"exit_code": -1, "blocked": True},
        ),
        (
            '{"output": "started", "exit_code": 0, "error": null}',
            {"command": "make serve", "background": True},
            {"exit_code": 0, "background": True},
        ),
        # A degraded backend or a fatal error: -1 without a block is a failure.
        (
            '{"output": "", "exit_code": -1, "status": "degraded", "error": "SSH host down"}',
            {},
            {"exit_code": -1},
        ),
        # A long foreground call Hermes promoted to the background: started, not done.
        (
            '{"output": "Background process started", "session_id": "p1", "pid": 42,'
            ' "exit_code": 0, "promoted_from_foreground": true}',
            {"command": "xcodebuild test", "timeout": 3600},
            {"exit_code": 0, "background": True},
        ),
        ('{"exit_code": 0}', {"command": "x", "background": "false"}, {"exit_code": 0}),
        # Waiting for an approval: it never ran.
        (
            '{"output": "", "exit_code": -1, "status": "pending_approval"}',
            {},
            {"exit_code": -1, "blocked": True},
        ),
        # Yielded to the background while it ran: no exit code yet.
        ('{"exit_code": null, "session_id": "p1", "pid": 7}', {}, {"background": True}),
        ('{"exit_code": 10000000000000}', {}, {}),  # no exit code
        # Nothing to read: the error key alone, odd types, not JSON, too large.
        ('{"output": "x", "error": "boom"}', {}, {}),
        ('{"exit_code": "1"}', {}, {}),
        ('{"exit_code": true}', {}, {}),
        ("plain text output", {}, {}),
        ("[1, 2]", {}, {}),
        ("{" + " " * plugin.MAX_RESULT_PARSE + "}", {}, {}),
        ('{"exit_code": 1' , {}, {}),
    ],
)  # fmt: skip
def test_exit_code_and_background_from_the_result(
    spool: Path, result: object, args: dict[str, Any], expected: dict[str, Any]
) -> None:
    plugin.post_tool_call(tool_name="terminal", args=args, result=result, status="ok")
    (rec,) = lines(spool)
    found = {k: rec[k] for k in ("exit_code", "blocked", "background") if k in rec}
    assert found == expected
    text = spool.read_text()
    assert "BUILD OK" not in text and "refused" not in text and "not found" not in text
    assert "SSH host" not in text


def test_only_the_terminal_tool_reports_an_exit_code(spool: Path) -> None:
    """``process`` reports the exit code of the job it polls or kills, not its own."""
    for result in (
        '{"status": "exited", "exit_code": 1}',
        '{"status": "killed", "exit_code": -15}',
    ):
        plugin.post_tool_call(tool_name="process", args={"action": "poll"}, result=result)
    assert [
        {k for k in ("exit_code", "blocked", "background") if k in r} for r in lines(spool)
    ] == [
        set(),
        set(),
    ]


@pytest.mark.parametrize(
    ("command", "whole"),
    [
        ("./scripts/ios_sim.sh test", True),
        ("cd app && ./scripts/ios_sim.sh test", True),
        ("rtk xcodebuild test", True),
        ("xcodebuild clean build test | xcbeautify", False),  # the pipe was dropped
        ('./scripts/ios_sim.sh test\necho "exit: $?"', False),  # a second line
        ("ios_sim.sh test --device x", False),  # masked last word
        ("make test;", False),
        # Cut at the redaction's four words, ending on the same word as the command.
        ("xcodebuild clean build test | grep -v test", False),
        ("ios_sim.sh boot build test 2>&1 | tee test", False),
        ("xcodebuild clean build test", False),  # four words: cut or not, it cannot tell
        ("ios_sim.sh test  \n", True),
    ],
)
def test_whether_the_redacted_command_is_whole(spool: Path, command: str, whole: bool) -> None:
    plugin.post_tool_call(
        tool_name="terminal", args={"command": command}, result='{"exit_code": 0}'
    )
    (rec,) = lines(spool)
    assert rec.get("command_whole", False) is whole
    assert rec.get("command_multiline", False) is ("\n" in command.strip())
