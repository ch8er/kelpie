"""The OpenClaw plugin (index.js) under Node: which hooks it registers, what it forwards to
kelpie_hook.py and that it never throws into the Gateway. Events are copies of what OpenClaw
2026.10.4 passed in a live Gateway run. Skipped without ``node``."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.openclaw.plugin import kelpie_hook

PLUGIN = Path(kelpie_hook.__file__).parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SID = "1ce1fbe0-c1fc-4572-91c4-394ad62e4a40"
TOOL_CTX = {
    "toolName": "exec",
    "agentId": "main",
    "sessionKey": "agent:main:main",
    "sessionId": SID,
    "runId": "run-1",
    "toolCallId": "call_1",
    "requester": {"senderIsOwner": True},
}
CALLS: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("session_start", {"sessionId": SID, "sessionKey": "agent:main:main"}, {"sessionId": SID}),
    (
        "after_tool_call",
        {
            "toolName": "exec",
            "params": {"command": "curl -u admin:hunter2 https://x.test"},
            "runId": "run-1",
            "toolCallId": "call_1",
            "result": {"content": [{"type": "text", "text": "SECRET OUTPUT"}]},
            "durationMs": 48,
        },
        TOOL_CTX,
    ),
    (
        "after_tool_call",
        {
            "toolName": "read",
            "params": {"path": "HEARTBEAT.md"},
            "runId": "run-1",
            "toolCallId": "call_2",
            "result": {"details": {"status": "error"}},
            "error": "File not found: /home/agent/.openclaw/workspace/HEARTBEAT.md.",
            "durationMs": 12,
        },
        {**TOOL_CTX, "toolName": "read", "toolCallId": "call_2"},
    ),
    (
        "llm_output",
        {
            "runId": "run-1",
            "sessionId": SID,
            "provider": "mock",
            "model": "loop",
            "assistantTexts": ["SECRET ANSWER"],
            "prompt": "SECRET PROMPT",
            "usage": {"input": 4000, "output": 40, "cacheRead": 0, "cacheWrite": 0, "total": 4040},
        },
        {**TOOL_CTX, "trigger": "heartbeat"},
    ),
    (
        "agent_end",
        {"messages": [{"role": "user", "content": "SECRET"}], "success": True, "runId": "run-1"},
        TOOL_CTX,
    ),
    ("before_reset", {"reason": "new"}, TOOL_CTX),  # not registered: ignored
]

HARNESS = """
import plugin from %(index)s;
const handlers = {};
const warnings = [];
const api = {
  pluginConfig: %(config)s,
  logger: { warn: (m) => warnings.push(m) },
  on: (name, fn) => { handlers[name] = fn; },
};
plugin.register(api);
const calls = %(calls)s;
for (const [name, event, ctx] of calls) {
  const fn = handlers[name];
  if (fn) {
    const out = fn(event, ctx);
    if (out !== undefined) throw new Error(`${name} returned ${JSON.stringify(out)}`);
  }
}
// bad input must not throw either
handlers.after_tool_call(undefined, undefined);
handlers.after_tool_call({ toolName: "x", params: { a: 1n } }, { sessionId: "s" });
const loop = { toolName: "y" };
loop.params = { self: loop };
handlers.after_tool_call(loop, { sessionId: "s" });
setTimeout(() => {
  console.log(JSON.stringify({ registered: Object.keys(handlers).sort(), warnings }));
}, 3000);
"""


def run_plugin(tmp_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    script = tmp_path / "harness.mjs"
    script.write_text(
        HARNESS
        % {
            "index": json.dumps((PLUGIN / "index.js").as_uri()),
            "config": json.dumps(config),
            "calls": json.dumps(CALLS),
        }
    )
    assert NODE is not None
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_forwards_reduced_events(tmp_path: Path) -> None:
    spool = tmp_path / "kelpie" / "spool.jsonl"
    out = run_plugin(tmp_path, {"python": sys.executable, "spool": str(spool)})
    assert out["registered"] == [
        "after_tool_call",
        "agent_end",
        "llm_output",
        "session_end",
        "session_start",
    ]
    assert out["warnings"] == []
    text = spool.read_text()
    assert "SECRET" not in text and "hunter2" not in text and "HEARTBEAT" not in text
    recs = [json.loads(x) for x in text.splitlines()]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in recs:
        by_type.setdefault(r["type"], []).append(r)
    assert sorted(by_type) == ["llm", "run_end", "session_start", "tool"]
    tools = sorted(by_type["tool"], key=lambda r: r["tool_call_id"] or "")
    exec_call, read_call = tools[-2:]
    assert (exec_call["tool"], exec_call["command"], exec_call["outcome"]) == (
        "exec",
        "curl -u *** ***",
        "ok",
    )
    assert exec_call["output_chars"] == len(
        json.dumps(CALLS[1][1]["result"], separators=(",", ":"))
    )
    assert (read_call["outcome"], read_call["output_chars"]) == ("error", None)
    odd = {r["tool"]: r for r in recs if r["session_id"] == "s"}
    assert odd["x"]["args_hash"] == kelpie_hook.args_hash({"a": "1"})  # BigInt as text
    assert odd["y"]["args_hash"] == kelpie_hook.args_hash(None)  # circular: no arguments
    assert all(r["session_id"] == SID for r in recs if r["session_id"] != "s")
    (llm,) = by_type["llm"]
    assert (llm["input"], llm["output"], llm["trigger"]) == (4000, 40, "heartbeat")


def test_commands_off_and_a_missing_python(tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    out = run_plugin(tmp_path, {"python": sys.executable, "spool": str(spool), "commands": False})
    assert all(json.loads(x).get("command") is None for x in spool.read_text().splitlines())
    out = run_plugin(tmp_path, {"python": str(tmp_path / "no-python"), "spool": str(spool)})
    assert len(out["warnings"]) == 1 and "cannot run" in out["warnings"][0]
