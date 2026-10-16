"""OpenClaw stop: markers the plugin enforces, the abort of the run in flight through a (fake)
``openclaw gateway call sessions.abort``, and what the adapter reports."""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.openclaw import OpenClawAdapter
from kelpie.adapters.openclaw import stop as oc_stop
from kelpie.adapters.openclaw.adapter import OpenClawError
from kelpie.adapters.openclaw.config import load_openclaw
from kelpie.core.actions import ActionResult
from kelpie.core.ports import SupportsStop, UnsupportedAction
from tests.adapters.test_openclaw_adapter import MAIN, Spool

SID = "1ce1fbe0-c1fc-4572-91c4-394ad62e4a40"

FAKE_CLI = """#!{python}
import json, os, sys, time
log = os.environ["OPENCLAW_FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "env": sorted(os.environ)}}) + "\\n")
answers = json.loads(os.environ.get("OPENCLAW_FAKE_ANSWERS", "[]"))
n = sum(1 for _ in open(log)) - 1
answer = answers[n] if n < len(answers) else {{"ok": True, "status": "aborted"}}
if answer == "sleep":
    time.sleep(30)
if answer == "fail":
    print("error: gateway closed (1006): token=SECRET", file=sys.stderr)
    sys.exit(1)
print("[gateway] connecting")
print(json.dumps(answer, indent=2))
"""


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``openclaw`` on PATH; its calls go to ``<tmp>/cli.log``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "openclaw"
    exe.write_text(FAKE_CLI.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "cli.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OPENCLAW_FAKE_LOG", str(log))
    monkeypatch.setenv("KELPIE_SECRET_TOKEN", "do-not-pass")
    return log


def calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def answers(monkeypatch: pytest.MonkeyPatch, *items: Any) -> None:
    monkeypatch.setenv("OPENCLAW_FAKE_ANSWERS", json.dumps(list(items)))


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    return Spool(tmp_path / "kelpie" / "spool.jsonl")


def adapter(tmp_path: Path, spool: Spool, **kw: Any) -> OpenClawAdapter:
    kw.setdefault("stop_dir", tmp_path / "kelpie" / "stop")
    return OpenClawAdapter(state_dir=tmp_path, workboard_db=None, spool=spool.path, **kw)


def live(spool: Spool, session: str = SID, key: str = MAIN) -> None:
    for i in range(3):
        spool.tool(session, datetime.now(UTC) - timedelta(seconds=30 - i), key=key)


# --- markers -----------------------------------------------------------------------------


def test_marker_round_trip(tmp_path: Path) -> None:
    d = tmp_path / "stop"
    path = oc_stop.write_marker(d, SID, "loop\x00 found\n" + "x" * 900, 1792191669.5)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert data["at"] == 1792191669.5
    assert data["reason"].startswith("loop found") and len(data["reason"]) == oc_stop.MAX_REASON
    assert oc_stop.read_marker(d, SID) == datetime.fromtimestamp(1792191669.5, UTC)
    assert oc_stop.read_marker(d, "../etc") is None
    with pytest.raises(ValueError):
        oc_stop.write_marker(d, "a/b", "x", 1.0)


def test_a_symlinked_marker_is_not_read(tmp_path: Path) -> None:
    d = tmp_path / "stop"
    d.mkdir()
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"at": 1.0, "reason": "x"}))
    (d / f"{SID}.json").symlink_to(target)
    assert oc_stop.read_marker(d, SID) is None
    oc_stop.write_marker(d, SID, "x", 2.0)  # replaces the link, not its target
    assert json.loads(target.read_text())["at"] == 1.0


# --- abort --------------------------------------------------------------------------------


def test_abort_command_and_environment(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "gw-token")
    assert oc_stop.abort_session("openclaw", MAIN, "run-1") == "aborted"
    (call,) = calls(cli)
    assert call["argv"][:4] == ["gateway", "call", "sessions.abort", "--params"]
    assert json.loads(call["argv"][4]) == {"key": MAIN, "clearQueued": True}
    assert call["argv"][5] == "--json"
    assert "OPENCLAW_GATEWAY_TOKEN" in call["env"]
    assert "KELPIE_SECRET_TOKEN" not in call["env"]


def test_abort_falls_back_to_the_run(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Background runs (skill-workshop review) are not under the key: seen live."""
    answers(monkeypatch, {"ok": True, "status": "no-active-run"}, {"ok": True, "status": "aborted"})
    assert oc_stop.abort_session("openclaw", MAIN, "run-1") == "aborted"
    first, second = calls(cli)
    assert json.loads(first["argv"][4])["key"] == MAIN
    assert json.loads(second["argv"][4]) == {"runId": "run-1", "clearQueued": True}


def test_a_reduced_key_is_not_sent(cli: Path) -> None:
    assert oc_stop.abort_session("openclaw", "agent:main:telegram:direct:~ab12", "r1") == "aborted"
    (call,) = calls(cli)
    assert json.loads(call["argv"][4]) == {"runId": "r1", "clearQueued": True}


@pytest.mark.parametrize(
    "answer", ["fail", {"ok": False, "error": {"message": "agent not found"}}, "not json"]
)
def test_abort_failures(cli: Path, monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    answers(monkeypatch, answer)
    with pytest.raises(oc_stop.OpenClawCliError) as err:
        oc_stop.abort_session("openclaw", MAIN, None)
    assert err.value.public_reason is not None and "SECRET" not in err.value.public_reason


def test_abort_timeout_kills_the_command(cli: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers(monkeypatch, "sleep")
    started = time.monotonic()
    with pytest.raises(oc_stop.OpenClawCliError, match="timed out"):
        oc_stop.abort_session("openclaw", MAIN, None, timeout=0.5)
    assert time.monotonic() - started < 10


def test_a_missing_cli(tmp_path: Path) -> None:
    with pytest.raises(oc_stop.OpenClawCliError, match="not found"):
        oc_stop.abort_session(str(tmp_path / "nope"), MAIN, None)


# --- adapter.stop ------------------------------------------------------------------------


def test_is_supports_stop(tmp_path: Path, spool: Spool) -> None:
    assert isinstance(adapter(tmp_path, spool), SupportsStop)


def test_stop_writes_the_marker_and_aborts(tmp_path: Path, spool: Spool, cli: Path) -> None:
    live(spool)
    a = adapter(tmp_path, spool)
    said = a.stop(SID, "Runaway run: 8 identical read calls in 10 minutes")
    assert isinstance(said, str) and said.startswith("its current run was aborted")
    assert oc_stop.read_marker(tmp_path / "kelpie" / "stop", SID) is not None
    (call,) = calls(cli)
    assert json.loads(call["argv"][4])["key"] == MAIN
    again = a.stop(SID, "again")  # idempotent: the marker is rewritten
    assert isinstance(again, str)
    marker = json.loads((tmp_path / "kelpie" / "stop" / f"{SID}.json").read_text())
    assert marker["reason"] == "again"


def test_stop_without_abort(
    tmp_path: Path, spool: Spool, cli: Path, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    live(spool)
    said = adapter(tmp_path, spool, cli=None).stop(SID, "loop")
    assert isinstance(said, str) and "not aborted (cli = false)" in said
    assert calls(cli) == []
    answers(monkeypatch, "fail")
    with caplog.at_level(logging.WARNING):
        said = adapter(tmp_path, spool).stop(SID, "loop")
    assert isinstance(said, str) and "could not be aborted" in said and "SECRET" not in said
    assert oc_stop.read_marker(tmp_path / "kelpie" / "stop", SID) is not None


def test_stop_refusals(tmp_path: Path, spool: Spool) -> None:
    a = adapter(tmp_path, spool)
    with pytest.raises(OpenClawError):
        a.stop("../x", "loop")
    with pytest.raises(UnsupportedAction, match="not in the hooks spool"):
        a.stop(SID, "loop")
    live(spool)
    with pytest.raises(UnsupportedAction, match="stop_dir = false"):
        adapter(tmp_path, spool, stop_dir=None).stop(SID, "loop")
    spool.path.parent.mkdir(parents=True, exist_ok=True)
    old = {"type": "tool", "at": time.time(), "session_id": "s-old", "v": 1}
    with spool.path.open("a") as f:
        f.write(json.dumps(old) + "\n")
    with pytest.raises(UnsupportedAction, match="without stop support"):
        a.stop("s-old", "loop")


def test_an_ended_session_is_left_alone(tmp_path: Path, spool: Spool, cli: Path) -> None:
    live(spool)
    spool.write("session_end", datetime.now(UTC), {"reason": "reset"}, sessionId=SID)
    assert adapter(tmp_path, spool).stop(SID, "loop") is ActionResult.NOTHING_TO_DO
    assert calls(cli) == []
    assert not (tmp_path / "kelpie" / "stop").exists()


def test_a_marker_that_cannot_be_written(tmp_path: Path, spool: Spool, cli: Path) -> None:
    """An agent can put a directory where its marker goes: the run is still aborted."""
    live(spool)
    (tmp_path / "kelpie" / "stop" / f"{SID}.json").mkdir(parents=True)
    with pytest.raises(OpenClawError) as err:
        adapter(tmp_path, spool).stop(SID, "loop")
    assert err.value.public_reason == (
        "the stop marker could not be written (its current run was aborted)"
    )
    assert len(calls(cli)) == 1


def test_an_end_followed_by_a_lift_is_still_an_end(tmp_path: Path, spool: Spool, cli: Path) -> None:
    """The plugin writes session_end, then resumed, on a reset: the session is over."""
    live(spool)
    now = datetime.now(UTC)
    spool.write("session_end", now, {"reason": "reset"}, sessionId=SID, sessionKey=MAIN)
    spool.write("kelpie_resumed", now, {"event": "session_end"}, sessionId=SID, sessionKey=MAIN)
    (s,) = adapter(tmp_path, spool).snapshot(now).sessions
    assert (s.ended_at is not None, s.exit_reason) == (True, "reset")
    assert adapter(tmp_path, spool).stop(SID, "loop") is ActionResult.NOTHING_TO_DO
    assert calls(cli) == []


def test_a_key_taken_by_a_newer_session_is_not_aborted(
    tmp_path: Path, spool: Spool, cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live(spool)
    spool.tool("newer", datetime.now(UTC))  # the same key, under a new session id
    answers(monkeypatch, {"ok": True, "status": "aborted"})
    adapter(tmp_path, spool).stop(SID, "loop")
    (call,) = calls(cli)
    assert json.loads(call["argv"][4]) == {"runId": "r1", "clearQueued": True}


def test_what_the_stop_says_depends_on_conversation_access(
    tmp_path: Path, spool: Spool, cli: Path
) -> None:
    live(spool)
    said = adapter(tmp_path, spool).stop(SID, "loop")
    assert isinstance(said, str) and "no conversation access" in said
    assert "operator's message" not in said
    spool.write("agent_end", datetime.now(UTC), {"success": True}, sessionId="other")
    said = adapter(tmp_path, spool).stop(SID, "loop")
    assert isinstance(said, str) and "until the operator messages it" in said


def test_a_plugin_not_yet_loaded_is_refused(tmp_path: Path, spool: Spool) -> None:
    """The hook stamps v2, but the Gateway still runs an index.js without stops."""
    spool.path.parent.mkdir(parents=True)
    line = {"type": "tool", "at": time.time(), "session_id": SID, "v": 2}
    spool.path.write_text(json.dumps(line) + "\n")
    with pytest.raises(UnsupportedAction, match="not yet loaded"):
        adapter(tmp_path, spool).stop(SID, "loop")


# --- what the session shows -------------------------------------------------------------


def test_stopped_and_resumed(tmp_path: Path, spool: Spool) -> None:
    live(spool)
    oc_stop.write_marker(tmp_path / "kelpie" / "stop", SID, "loop", time.time())
    spool.write("kelpie_stopped", datetime.now(UTC), {"event": "before_tool_call"}, sessionId=SID)
    (s,) = adapter(tmp_path, spool).snapshot(datetime.now(UTC)).sessions
    assert s.exit_reason == "stopped by kelpie"
    spool.write("kelpie_resumed", datetime.now(UTC), {"event": "before_agent_run"}, sessionId=SID)
    (s,) = adapter(tmp_path, spool).snapshot(datetime.now(UTC)).sessions
    assert s.exit_reason is None


def test_a_stop_not_enforced_is_reported(
    tmp_path: Path, spool: Spool, caplog: pytest.LogCaptureFixture
) -> None:
    now = datetime.now(UTC)
    oc_stop.write_marker(
        tmp_path / "kelpie" / "stop", SID, "loop", (now - timedelta(minutes=10)).timestamp()
    )
    spool.tool(SID, now - timedelta(minutes=1))
    a = adapter(tmp_path, spool)
    with caplog.at_level(logging.WARNING):
        (s,) = a.snapshot(now).sessions
        a.snapshot(now)
    assert s.exit_reason == "stop not enforced"
    assert sum("still calls tools" in r.message for r in caplog.records) == 1


def test_a_call_begun_before_the_stop_is_not_a_miss(tmp_path: Path, spool: Spool) -> None:
    now = datetime.now(UTC)
    oc_stop.write_marker(
        tmp_path / "kelpie" / "stop", SID, "loop", (now - timedelta(minutes=3)).timestamp()
    )
    spool.write(  # a 5 minute call that ended a minute ago
        "after_tool_call",
        now - timedelta(minutes=1),
        {"toolName": "exec", "params": {}, "durationMs": 300000},
        sessionId=SID,
    )
    (s,) = adapter(tmp_path, spool).snapshot(now).sessions
    assert s.exit_reason is None


# --- config ------------------------------------------------------------------------------


def test_stop_config() -> None:
    s = load_openclaw({})
    assert (s.stop_dir, s.cli, s.cli_timeout) == (
        Path("~/.openclaw/kelpie/stop"),
        "openclaw",
        timedelta(seconds=20),
    )
    s = load_openclaw({"stop_dir": False, "cli": False, "cli_timeout": "5s", "stop_ttl": "2h"})
    a = s.build()
    assert (a.stop_dir, a.cli, a.cli_timeout, a.stop_ttl) == (None, None, 5.0, timedelta(hours=2))
    assert load_openclaw({}).stop_ttl == timedelta(hours=24)
    bad_ones = ({"cli": ""}, {"cli_timeout": "2m"}, {"cli_timeout": "0s"}, {"stop_ttl": "0s"})
    for bad in (*bad_ones, {"stop_dir": 1}):
        with pytest.raises(ValueError):
            load_openclaw(bad)
