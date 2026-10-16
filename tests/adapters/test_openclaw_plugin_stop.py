"""The OpenClaw plugin enforces stop markers (index.js under Node): tool calls and runs that are
not the owner's message are refused, the owner's message or a reset lifts the stop, broken
markers are ignored. Skipped without ``node``."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.openclaw import stop as oc_stop
from kelpie.adapters.openclaw.plugin import kelpie_hook

PLUGIN = Path(kelpie_hook.__file__).parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SID = "1ce1fbe0-c1fc-4572-91c4-394ad62e4a40"
CTX = {"sessionId": SID, "sessionKey": "agent:main:main", "agentId": "main", "runId": "r1"}

HARNESS = """
import { symlinkSync, unlinkSync, writeFileSync } from "node:fs";
import plugin from %(index)s;
const handlers = {};
const warnings = [];
plugin.register({
  pluginConfig: %(config)s,
  logger: { warn: (m) => warnings.push(m) },
  on: (name, fn) => { handlers[name] = fn; },
});
const out = [];
for (const step of %(steps)s) {
  if (step.write) writeFileSync(step.write, step.text);
  else if (step.symlink) symlinkSync(step.target, step.symlink);
  else if (step.unlink) { try { unlinkSync(step.unlink); } catch {} }
  else {
    const [name, event, ctx] = step.call;
    out.push(handlers[name](event, ctx) ?? null);
  }
}
setTimeout(() => console.log(JSON.stringify({ out, warnings })), 1500);
"""


def run(tmp_path: Path, steps: list[dict[str, Any]], **config: Any) -> dict[str, Any]:
    script = tmp_path / "harness.mjs"
    settings = {
        "python": sys.executable,
        "spool": str(tmp_path / "spool.jsonl"),
        "stopDir": str(tmp_path / "stop"),
        **config,
    }
    script.write_text(
        HARNESS
        % {
            "index": json.dumps((PLUGIN / "index.js").as_uri()),
            "config": json.dumps(settings),
            "steps": json.dumps(steps),
        }
    )
    assert NODE is not None
    done = subprocess.run(
        [NODE, str(script)], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def marker(tmp_path: Path, at: float | None = None, reason: str = "loop") -> Path:
    return oc_stop.write_marker(tmp_path / "stop", SID, reason, time.time() if at is None else at)


def tool() -> dict[str, Any]:
    return {"call": ["before_tool_call", {"toolName": "read", "params": {}}, CTX]}


def agent_run(trigger: str, owner: bool | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {"prompt": "SECRET PROMPT", "messages": []}
    if owner is not None:
        event["senderIsOwner"] = owner
    return {"call": ["before_agent_run", event, {**CTX, "trigger": trigger}]}


def spool_lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "spool.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_no_marker_no_stop(tmp_path: Path) -> None:
    result = run(tmp_path, [tool(), agent_run("heartbeat")])
    assert result["out"] == [None, {"outcome": "pass"}]
    assert spool_lines(tmp_path) == []


def test_a_stop_refuses_tools_and_machine_runs(tmp_path: Path) -> None:
    path = marker(tmp_path, reason="Runaway run: 8 identical read calls")
    result = run(
        tmp_path,
        [tool(), agent_run("heartbeat"), agent_run("cron"), agent_run("user", owner=False)],
    )
    blocked, heartbeat, cron, stranger = result["out"]
    assert blocked == {
        "block": True,
        "blockReason": "Stopped by Kelpie: Runaway run: 8 identical read calls",
    }
    for refused in (heartbeat, cron, stranger):
        assert refused["outcome"] == "block"
        assert refused["reason"].startswith("Stopped by Kelpie")
        assert "owner sends it a message" in refused["message"]
    assert path.exists()
    events = [(r["type"], r["event"]) for r in spool_lines(tmp_path)]
    assert sorted(events) == [
        ("stopped", "before_agent_run"),
        ("stopped", "before_agent_run"),
        ("stopped", "before_agent_run"),
        ("stopped", "before_tool_call"),
    ]
    assert "SECRET" not in (tmp_path / "spool.jsonl").read_text()


def test_the_owners_message_resumes(tmp_path: Path) -> None:
    path = marker(tmp_path)
    result = run(tmp_path, [agent_run("user", owner=True), tool()])
    assert result["out"] == [{"outcome": "pass"}, None]
    assert not path.exists()
    assert [(r["type"], r["event"]) for r in spool_lines(tmp_path)] == [
        ("resumed", "before_agent_run")
    ]


def test_session_ends(tmp_path: Path) -> None:
    path = marker(tmp_path)
    end = {"sessionId": SID, "sessionKey": "agent:main:main"}
    result = run(
        tmp_path,
        [
            {"call": ["session_end", {**end, "reason": "restart"}, CTX]},
            tool(),
            {"call": ["session_end", {**end, "reason": "reset"}, CTX]},
            tool(),
        ],
    )
    assert result["out"][1]["block"] is True and result["out"][3] is None
    assert not path.exists()
    types = sorted(r["type"] for r in spool_lines(tmp_path))
    assert types == ["resumed", "session_end", "session_end", "stopped"]


@pytest.mark.parametrize(
    "setup",
    [
        "expired",
        "future",
        "garbage",
        "huge",
        "no reason",
        "symlink",
        "directory",
    ],
)
def test_broken_markers_do_not_stop(tmp_path: Path, setup: str) -> None:
    stop_dir = tmp_path / "stop"
    stop_dir.mkdir()
    path = stop_dir / f"{SID}.json"
    steps: list[dict[str, Any]] = []
    if setup == "expired":
        marker(tmp_path, at=time.time() - 25 * 3600)
    elif setup == "future":
        marker(tmp_path, at=time.time() + 3600)
    elif setup == "garbage":
        path.write_text("{not json")
    elif setup == "huge":
        path.write_text(json.dumps({"at": time.time(), "reason": "x" * 10000}))
    elif setup == "no reason":
        path.write_text(json.dumps({"at": time.time()}))
    elif setup == "symlink":
        target = tmp_path / "elsewhere.json"
        target.write_text(json.dumps({"at": time.time(), "reason": "x"}))
        path.symlink_to(target)
    elif setup == "directory":
        path.mkdir()
    result = run(tmp_path, [*steps, tool(), tool(), agent_run("heartbeat")])
    assert result["out"] == [None, None, {"outcome": "pass"}]
    assert len(result["warnings"]) <= 1  # an unreadable marker is reported once


def test_a_shorter_ttl(tmp_path: Path) -> None:
    marker(tmp_path, at=time.time() - 120)
    assert run(tmp_path, [tool()], stopTtl=60)["out"] == [None]
    assert run(tmp_path, [tool()])["out"][0]["block"] is True


def test_unusable_session_ids(tmp_path: Path) -> None:
    marker(tmp_path)
    steps = [
        {"call": ["before_tool_call", {}, {**CTX, "sessionId": "../stop/" + SID}]},
        {"call": ["before_tool_call", {}, {}]},
        {"call": ["before_tool_call", {}, None]},
        {"call": ["before_agent_run", None, None]},
    ]
    assert run(tmp_path, steps)["out"] == [None, None, None, {"outcome": "pass"}]
