"""A simulated OpenClaw Gateway for end-to-end scenarios.

- **The spool** is written by the real hook script (:mod:`kelpie.adapters.openclaw.plugin.
  kelpie_hook`), fed the reduced events the plugin forwards, so the lines are what a Gateway
  with the Kelpie plugin (and conversation access) writes. Calls in the past (a loop that ran
  all night) carry their own time.
- **The Workboard** is :class:`tests.sim.workboard.Workboard` under the state directory.
- **A fake ``openclaw`` executable** answers ``gateway call health`` and ``gateway call
  sessions.abort`` and records every call (arguments and the names of the environment
  variables it got, never their values) in ``openclaw-calls.jsonl``. A file ``openclaw-fail``
  next to it makes every call fail as an unreachable Gateway does.
- :meth:`Gateway.plugin_refuses` runs the real ``index.js`` under Node (when installed) to
  ask whether the plugin would refuse a session's next tool call with the markers on disk.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from kelpie.adapters.openclaw.plugin import kelpie_hook
from tests.sim.workboard import Workboard

PLUGIN = Path(kelpie_hook.__file__).parent
CALLS = "openclaw-calls.jsonl"
FAIL = "openclaw-fail"
_LOCK = threading.Lock()  # the hook reads os.environ: one writer at a time

FAKE_CLI = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, {calls!r}), "a") as f:
    f.write(json.dumps({{"argv": args, "env": sorted(os.environ)}}) + "\\n")
if os.path.exists(os.path.join(here, {fail!r})):
    print(json.dumps({{"ok": False, "error": {{"type": "gateway_transport_error",
        "message": "Gateway not reachable at ws://127.0.0.1:18789 (ECONNREFUSED)."}}}}))
    sys.exit(1)
if args[:3] == ["gateway", "call", "health"]:
    print(json.dumps({{"ok": True, "plugins": {{"loaded": ["workboard", "kelpie"]}}}}))
elif args[:3] == ["gateway", "call", "sessions.abort"]:
    print("[gateway] connecting")
    print(json.dumps({{"ok": True, "abortedRunId": "run", "status": "aborted"}}, indent=2))
else:
    sys.exit(2)
"""

HARNESS = """
import plugin from %(index)s;
const handlers = {};
plugin.register({ pluginConfig: %(config)s, logger: { warn: () => {} },
  on: (name, fn) => { handlers[name] = fn; } });
const out = %(sessions)s.map((sessionId) =>
  handlers.before_tool_call({ toolName: "read", params: {} }, { sessionId }) ?? null);
console.log(JSON.stringify(out));
setTimeout(() => process.exit(0), 300);
"""


class Gateway:
    def __init__(self, root: Path) -> None:
        self.state = root / "openclaw"
        self.state.mkdir(parents=True)
        self.spool_path = self.state / "kelpie" / "spool.jsonl"
        self.stop_dir = self.state / "kelpie" / "stop"
        self.board = Workboard(self.state / "plugins" / "workboard" / "workboard.sqlite")
        bin_dir = root / "openclaw-bin"
        bin_dir.mkdir()
        self.cli = bin_dir / "openclaw"
        self.cli.write_text(FAKE_CLI.format(python=sys.executable, calls=CALLS, fail=FAIL))
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)

    # --- config and the fake CLI -------------------------------------------------------

    def adapter_config(self) -> dict[str, Any]:
        return {
            "state_dir": str(self.state),
            "spool": str(self.spool_path),
            "stop_dir": str(self.stop_dir),
            "cli": str(self.cli),
            "cli_timeout": "10s",
            "lookback": "24h",
        }

    def unreachable(self) -> None:
        (self.cli.parent / FAIL).write_text("")

    def calls(self) -> list[dict[str, Any]]:
        path = self.cli.parent / CALLS
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def markers(self) -> dict[str, dict[str, Any]]:
        if not self.stop_dir.is_dir():
            return {}
        return {p.stem: json.loads(p.read_text()) for p in self.stop_dir.glob("*.json")}

    # --- the plugin's hooks, through the real hook script --------------------------------

    @contextmanager
    def _hook_env(self) -> Iterator[None]:
        with _LOCK:
            old = os.environ.get(kelpie_hook.SPOOL_ENV)
            os.environ[kelpie_hook.SPOOL_ENV] = str(self.spool_path)
            try:
                yield
            finally:
                if old is None:
                    del os.environ[kelpie_hook.SPOOL_ENV]
                else:
                    os.environ[kelpie_hook.SPOOL_ENV] = old

    def _forward(self, hook: str, at: datetime, event: dict[str, Any], ctx: dict[str, Any]) -> None:
        data = {"hook": hook, "at": at.timestamp(), "plugin": 2, "gate": True, "event": event}
        data["ctx"] = ctx
        with self._hook_env():
            kelpie_hook.main(io.StringIO(json.dumps(data)))

    def tool_call(
        self,
        session_id: str,
        session_key: str,
        run_id: str,
        tool: str,
        params: dict[str, Any],
        *,
        at: datetime,
        error: bool = False,
        agent: str = "main",
        call_id: str | None = None,
    ) -> None:
        """One ``after_tool_call``, as the plugin forwards it."""
        event: dict[str, Any] = {
            "toolName": tool,
            "params": params,
            "runId": run_id,
            "toolCallId": call_id,
            "durationMs": 40,
        }
        if error:
            event["error"] = "error"
        else:
            event["outputChars"] = 120
        ctx = {"sessionId": session_id, "sessionKey": session_key, "agentId": agent}
        self._forward("after_tool_call", at, event, {**ctx, "runId": run_id})

    def run_end(self, session_id: str, session_key: str, run_id: str, *, at: datetime) -> None:
        ctx = {"sessionId": session_id, "sessionKey": session_key, "runId": run_id}
        event = {"runId": run_id, "success": True, "durationMs": 900}
        self._forward("agent_end", at, event, {**ctx, "agentId": "main", "trigger": "heartbeat"})

    # --- the real plugin --------------------------------------------------------------------

    def plugin_refuses(self, *session_ids: str) -> list[bool] | None:
        """Whether the real ``index.js`` refuses each session's next tool call; None without
        ``node``."""
        node = shutil.which("node")
        if node is None:
            return None
        script = self.state / "harness.mjs"
        config = {"spool": str(self.state / "plugin-spool.jsonl"), "stopDir": str(self.stop_dir)}
        config["python"] = sys.executable
        script.write_text(
            HARNESS
            % {
                "index": json.dumps((PLUGIN / "index.js").as_uri()),
                "config": json.dumps(config),
                "sessions": json.dumps(list(session_ids)),
            }
        )
        done = subprocess.run(
            [node, str(script)], capture_output=True, text=True, timeout=60, check=True
        )
        answers = json.loads(done.stdout.strip().splitlines()[0])
        return [bool(a and a.get("block")) for a in answers]
