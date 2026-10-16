"""The OpenClaw hook script (kelpie_hook.py): spool lines from forwarded hooks, privacy,
never failing."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.openclaw.plugin import kelpie_hook as hook
from kelpie.traces.args import args_hash
from tests.traces.test_redaction import LEAKS

SID = "1ce1fbe0-c1fc-4572-91c4-394ad62e4a40"
SECRET = bytes(range(32))
CTX = {"sessionId": SID, "sessionKey": "agent:main:main", "agentId": "main", "runId": "run-1"}


@pytest.fixture
def spool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sub" / "spool.jsonl"
    monkeypatch.setenv(hook.SPOOL_ENV, str(path))
    for var in (hook.COMMANDS_ENV, hook.MAX_BYTES_ENV):
        monkeypatch.delenv(var, raising=False)
    return path


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def run(data: Any) -> int:
    raw = data if isinstance(data, str) else json.dumps(data)
    return hook.main(io.StringIO(raw))


def forwarded(name: str, event: dict[str, Any], **ctx: Any) -> dict[str, Any]:
    return {"hook": name, "at": 1792191669.5, "event": event, "ctx": {**CTX, **ctx}}


def test_tool_call(spool: Path) -> None:
    params = {"command": "GH_TOKEN=abc gh pr checks 5", "workdir": "/srv/repo"}
    event = {
        "toolName": "exec",
        "params": params,
        "runId": "run-1",
        "toolCallId": "call_1",
        "durationMs": 250,
        "outputChars": 17,
    }
    assert run(forwarded("after_tool_call", event, toolCallId="call_1")) == 0
    (rec,) = lines(spool)
    assert rec == {
        "type": "tool",
        "at": 1792191669.5,
        "session_id": SID,
        "v": hook.HOOK_VERSION,
        "session_key": "agent:main:main",
        "agent": "main",
        "run_id": "run-1",
        "tool": "exec",
        "tool_call_id": "call_1",
        "args_hash": args_hash(params),
        "command": "gh pr checks 5",
        "outcome": "ok",
        "duration_ms": 250,
        "output_chars": 17,
    }
    assert "/srv/repo" not in spool.read_text()


def test_failed_tool_call_has_no_output_size(spool: Path) -> None:
    event = {"toolName": "read", "params": {"path": "HEARTBEAT.md"}, "error": "error"}
    run(forwarded("after_tool_call", {**event, "outputChars": 99}))
    (rec,) = lines(spool)
    assert (rec["outcome"], rec["output_chars"], rec["command"]) == ("error", None, None)
    assert "HEARTBEAT" not in spool.read_text()


def test_session_lines(spool: Path) -> None:
    run(forwarded("session_start", {"sessionId": SID, "sessionKey": "agent:main:main"}))
    run(
        forwarded(
            "session_end",
            {"sessionId": SID, "reason": "reset", "nextSessionId": "next-1"},
            runId=None,
        )
    )
    start, end = lines(spool)
    assert (start["type"], start["resumed_from"], start["session_key"]) == (
        "session_start",
        None,
        "agent:main:main",
    )
    assert (end["type"], end["reason"], end["next_session_id"], end["run_id"]) == (
        "session_end",
        "reset",
        "next-1",
        None,
    )


def test_llm_and_run_end(spool: Path) -> None:
    usage = {"input": 4000, "output": 40, "cacheRead": 12000, "cacheWrite": 0}
    run(
        forwarded(
            "llm_output",
            {
                "runId": "run-1",
                "sessionId": SID,
                "provider": "anthropic",
                "model": "m",
                "usage": usage,
            },
            trigger="heartbeat",
        )
    )
    run(forwarded("agent_end", {"runId": "run-1", "success": False, "durationMs": 994}))
    llm, end = lines(spool)
    assert llm["type"] == "llm"
    assert (llm["provider"], llm["model"], llm["trigger"]) == ("anthropic", "m", "heartbeat")
    assert (llm["input"], llm["output"], llm["cacheRead"], llm["cacheWrite"]) == (
        4000,
        40,
        12000,
        0,
    )
    assert (end["type"], end["success"], end["duration_ms"]) == ("run_end", False, 994)


@pytest.mark.parametrize(
    "data",
    [
        "not json",
        "[]",
        {"hook": "before_tool_call", "event": {}, "ctx": CTX},  # not recorded
        {"hook": "after_tool_call", "event": {"toolName": "x"}, "ctx": {}},  # no session
        {"hook": "after_tool_call", "event": "x", "ctx": "y"},
    ],
)
def test_ignored_input(spool: Path, data: Any) -> None:
    assert run(data) == 0
    assert not spool.exists()


def test_bad_values_are_dropped(spool: Path) -> None:
    event = {
        "toolName": {"x": 1},
        "params": None,
        "durationMs": -5,
        "outputChars": True,
    }
    run({"hook": "after_tool_call", "at": "soon", "event": event, "ctx": {"sessionId": "s" * 300}})
    (rec,) = lines(spool)
    assert rec["session_id"] == "s" * hook.MAX_ID
    assert (rec["tool"], rec["duration_ms"], rec["output_chars"]) == (None, None, None)
    assert isinstance(rec["at"], float)


def test_a_future_time_is_replaced(spool: Path) -> None:
    data = forwarded("after_tool_call", {"toolName": "read", "params": {}})
    data["at"] = 9e12
    run(data)
    (rec,) = lines(spool)
    assert rec["at"] < 9e12


def test_commands_can_be_turned_off(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hook.COMMANDS_ENV, "0")
    run(forwarded("after_tool_call", {"toolName": "exec", "params": {"command": "make test"}}))
    (rec,) = lines(spool)
    assert rec["command"] is None


@pytest.mark.parametrize(("command", "secret"), LEAKS[:12])
def test_no_secret_reaches_the_spool(spool: Path, command: str, secret: str) -> None:
    run(forwarded("after_tool_call", {"toolName": "exec", "params": {"command": command}}))
    assert secret not in spool.read_text()


def test_spool_mode_and_rotation(spool: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(hook.MAX_BYTES_ENV, "100")
    for _ in range(3):
        run(forwarded("after_tool_call", {"toolName": "read", "params": {}}))
    assert stat.S_IMODE(spool.stat().st_mode) == 0o600
    assert stat.S_IMODE(spool.parent.stat().st_mode) == 0o700
    rotated = spool.with_name(spool.name + ".1")
    assert rotated.exists()
    assert len(lines(spool)) == 1


def test_a_symlinked_spool_is_not_followed(spool: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    spool.parent.mkdir(parents=True)
    spool.symlink_to(target)
    assert run(forwarded("after_tool_call", {"toolName": "read", "params": {}})) == 0
    assert target.read_text() == ""


@pytest.mark.parametrize(
    ("key", "kept"),
    [
        ("agent:main:main", True),
        ("agent:main:main:heartbeat", True),
        ("agent:dev:subagent:workboard-default-c1", True),
        ("agent:main:cron:nightly:run:8821", True),
        ("cron:nightly", True),
        ("hook:2b9c", False),
        ("hook:imap:acct:inbox", False),
        ("agent:main:direct:alice:thread:1700000000.000100", False),
        ("agent:main:direct:u0123abcd:thread:99", False),
        ("agent:main:slack:acct1:direct:u01:thread:5", False),
        ("agent:main:telegram", False),
        ("agent:main:internal-session-effects:skill-workshop-review_x", True),
        ("unknown", True),
        ("agent:main:whatsapp:direct:+15550100", False),
        ("agent:main:telegram:group:-100123", False),
        ("agent:main:direct:alice", False),
        ("agent:main:discord:channel:123:thread:9", False),
        ("something else", False),
    ],
)
def test_session_keys_lose_their_peer(key: str, kept: bool) -> None:
    out = hook.safe_session_key(key, SECRET)
    assert out is not None
    if kept:
        assert out == key
    else:
        kept_parts = out.split(":")[:-1]
        assert out.split(":")[-1].startswith("~")
        for secret in ("alice", "u0123abcd", "u01", "+15550100", "-100123", "imap", "123"):
            assert secret not in kept_parts
        assert out == hook.safe_session_key(key, SECRET)  # stable: the same peer, the same key


def test_long_keys_are_hashed_whole() -> None:
    base = "agent:main:whatsapp:direct:" + "9" * 200
    a, b = hook.safe_session_key(base + "1", SECRET), hook.safe_session_key(base + "2", SECRET)
    assert a != b and a is not None and len(a) < 64
    assert hook.safe_session_key("x" * 5000, SECRET) is None


def test_pseudonyms_are_keyed(tmp_path: Path) -> None:
    """A phone number cannot be found by hashing every number: the digits are an HMAC."""
    key = "agent:main:whatsapp:direct:+15550100"
    plain = hashlib.sha256(b"+15550100").hexdigest()[:12]
    one = hook.safe_session_key(key, SECRET)
    other = hook.safe_session_key(key, bytes(32))
    assert one is not None and other is not None and one != other and plain not in one
    assert hook.safe_session_key(key, None) == "agent:main:whatsapp:direct:~nokey"
    assert hook.safe_session_key("agent:main:main", None) == "agent:main:main"


def test_the_peer_key_file(spool: Path) -> None:
    path = hook.peer_key_path()
    assert path == spool.with_name("peer.key") and not path.exists()
    run(forwarded("after_tool_call", {"toolName": "read", "params": {}}))
    assert not path.exists()  # a key that names no one needs no secret
    dm = "agent:main:whatsapp:direct:+15550100"
    run(forwarded("after_tool_call", {"toolName": "read", "params": {}}, sessionKey=dm))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    secret = hook.read_peer_key(path)
    assert secret is not None and len(secret) == 32
    run(forwarded("after_tool_call", {"toolName": "read", "params": {}}, sessionKey=dm))
    first, second = (r["session_key"] for r in lines(spool)[1:])
    assert first == second == hook.safe_session_key(dm, secret)
    path.write_text("short")
    assert hook.read_peer_key(path) is None
    assert hook.peer_key() is None  # an unusable key file is not replaced


def test_the_spool_holds_the_reduced_key(spool: Path) -> None:
    key = "agent:main:whatsapp:direct:+15550100"
    run(forwarded("after_tool_call", {"toolName": "read", "params": {}}, sessionKey=key))
    assert "+1555" not in spool.read_text()
    assert lines(spool)[0]["session_key"].startswith("agent:main:whatsapp:direct:~")


def test_a_digest_from_the_plugin_replaces_the_hash(spool: Path) -> None:
    event = {"toolName": "write", "params": None, "paramsDigest": "0123456789abcdef"}
    run(forwarded("after_tool_call", event))
    (rec,) = lines(spool)
    assert rec["args_hash"] == "0123456789abcdef"


def test_stopped_and_resumed_lines(spool: Path) -> None:
    run(forwarded("kelpie_stopped", {"event": "before_tool_call"}))
    run(forwarded("kelpie_resumed", {"event": "before_agent_run"}))
    stopped, resumed = lines(spool)
    assert (stopped["type"], stopped["event"], stopped["v"]) == ("stopped", "before_tool_call", 2)
    assert (resumed["type"], resumed["event"]) == ("resumed", "before_agent_run")


def test_args_hash_matches_otlp_ingest() -> None:
    for args in ({"b": 1, "a": [1, 2]}, None, "text", {"x": object()}):
        assert hook.args_hash(args) == args_hash(args)


def test_runs_as_a_script_with_only_the_standard_library(spool: Path) -> None:
    """The hook runs on the Gateway host with the system Python, without Kelpie."""
    tree = ast.parse(Path(hook.__file__).read_text())
    imported = {
        name.split(".")[0]
        for node in ast.walk(tree)
        for name in (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module]
            if isinstance(node, ast.ImportFrom) and node.module
            else []
        )
    }
    assert "kelpie" not in imported
    assert imported <= set(sys.stdlib_module_names)
    data = forwarded("after_tool_call", {"toolName": "read", "params": {}})
    env = {"PATH": os.environ["PATH"], hook.SPOOL_ENV: str(spool)}
    done = subprocess.run(
        [sys.executable, "-I", hook.__file__],
        input=json.dumps(data),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert (done.returncode, done.stdout) == (0, "")
    assert lines(spool)[0]["tool"] == "read"
