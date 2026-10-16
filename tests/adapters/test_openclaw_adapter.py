"""OpenClaw adapter, read side: sessions and spans from the hooks spool, tasks from the
Workboard, config."""

from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.openclaw import OpenClawAdapter
from kelpie.adapters.openclaw.adapter import NO_TASK_PREFIX, OpenClawError, parse_line
from kelpie.adapters.openclaw.config import load_openclaw
from kelpie.adapters.openclaw.plugin import kelpie_hook
from kelpie.app import ConfigError, build_adapters, load_settings
from kelpie.core.model import SpanKind, Status
from kelpie.core.ports import PlatformAdapter, UnsupportedAction
from kelpie.traces.args import args_hash
from kelpie.traces.hooks import HOOKS, SOURCE
from tests.sim.workboard import Workboard

NOW = datetime.now(UTC).replace(microsecond=0)
MAIN = "agent:main:main"


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


class Spool:
    """Lines written by the real hook script, as the plugin forwards them."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, hook: str, at: datetime, event: dict[str, Any], **ctx: Any) -> None:
        data = {"hook": hook, "at": at.timestamp(), "event": event, "ctx": ctx}
        old = os.environ.get(kelpie_hook.SPOOL_ENV)
        os.environ[kelpie_hook.SPOOL_ENV] = str(self.path)
        try:
            kelpie_hook.main(io.StringIO(json.dumps(data)))
        finally:
            if old is None:
                del os.environ[kelpie_hook.SPOOL_ENV]
            else:
                os.environ[kelpie_hook.SPOOL_ENV] = old

    def tool(
        self,
        session: str,
        at: datetime,
        tool: str = "read",
        params: Any = None,
        *,
        key: str = MAIN,
        agent: str = "main",
        error: bool = False,
        call: str | None = None,
    ) -> None:
        event = {
            "toolName": tool,
            "params": params if params is not None else {"path": "HEARTBEAT.md"},
            "durationMs": 1000,
            "outputChars": 0 if error else 12,
            "toolCallId": call,
        }
        if error:
            event["error"] = "error"
        self.write(
            "after_tool_call",
            at,
            event,
            sessionId=session,
            sessionKey=key,
            agentId=agent,
            runId="r1",
        )


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    return Spool(tmp_path / "kelpie" / "spool.jsonl")


@pytest.fixture
def board(tmp_path: Path) -> Workboard:
    return Workboard(tmp_path / "plugins" / "workboard" / "workboard.sqlite")


def adapter(tmp_path: Path, spool: Spool, **kw: Any) -> OpenClawAdapter:
    kw.setdefault("workboard_db", tmp_path / "plugins" / "workboard" / "workboard.sqlite")
    return OpenClawAdapter(state_dir=tmp_path, spool=spool.path, **kw)


def test_is_a_platform_adapter(tmp_path: Path, spool: Spool) -> None:
    assert isinstance(adapter(tmp_path, spool), PlatformAdapter)


def test_sessions_from_the_spool(tmp_path: Path, spool: Spool, board: Workboard) -> None:
    spool.write(
        "session_start", ago(hours=2), {"sessionId": "s1", "sessionKey": MAIN}, sessionId="s1"
    )
    for i in range(3):
        spool.tool("s1", ago(minutes=30 - i))
    spool.write(
        "session_end",
        ago(minutes=10),
        {"sessionId": "s1", "reason": "reset", "nextSessionId": "s2"},
        sessionId="s1",
        sessionKey=MAIN,
        agentId="main",
    )
    spool.tool("s2", ago(minutes=5))
    spool.write("agent_end", ago(minutes=4), {"success": False}, sessionId="s2", sessionKey=MAIN)
    snap = adapter(tmp_path, spool).snapshot(NOW)
    s1, s2 = sorted(snap.sessions, key=lambda s: s.id)
    assert (s1.id, s1.task_id, s1.owner_id, s1.source) == (
        "s1",
        f"{NO_TASK_PREFIX}{MAIN}",
        "main",
        "openclaw",
    )
    assert (s1.started_at, s1.ended_at, s1.exit_reason) == (ago(hours=2), ago(minutes=10), "reset")
    assert s1.last_heartbeat == ago(minutes=10)
    assert (s2.ended_at, s2.exit_reason, s2.last_heartbeat) == (None, "run failed", ago(minutes=4))
    assert [o.id for o in snap.owners] == ["main"]


def test_a_session_resumed_after_its_end_is_open(tmp_path: Path, spool: Spool) -> None:
    spool.write("session_end", ago(minutes=10), {"reason": "restart"}, sessionId="s1")
    spool.tool("s1", ago(minutes=5))
    (s,) = adapter(tmp_path, spool).snapshot(NOW).sessions
    assert (s.ended_at, s.exit_reason) == (None, None)


def test_sessions_link_to_workboard_cards(tmp_path: Path, spool: Spool, board: Workboard) -> None:
    key = "agent:dev:subagent:workboard-default-c1"
    claim = {"ownerId": "dev", "expiresAt": NOW.timestamp() * 1000 + 6e5}
    board.card(
        "c1",
        "running",
        ago(hours=1),
        title="Nightly heartbeat",
        agent="dev",
        session_key=key,
        claim=claim,
    )
    board.card("c2", "ready", ago(hours=1), agent="ops")
    spool.tool("s1", ago(minutes=2), key=key, agent="dev")
    snap = adapter(tmp_path, spool).snapshot(NOW)
    (s,) = snap.sessions
    assert (s.task_id, s.owner_id) == ("c1", "dev")
    assert {t.id: t.status for t in snap.tasks} == {"c1": Status.IN_PROGRESS, "c2": Status.READY}
    assert {o.id for o in snap.owners} == {"dev"}
    tool = next(sp for sp in snap.spans if sp.kind is SpanKind.TOOL)
    assert tool.task_id == "c1"


def test_cards_sharing_a_session_key(tmp_path: Path, spool: Spool, board: Workboard) -> None:
    """Cards created from the main chat all name agent:main:main: the running one wins."""
    board.card("old", "done", ago(hours=5), session_key=MAIN, completed_at=ago(hours=4))
    board.card("open", "todo", ago(hours=3), session_key=MAIN)
    board.card("live", "running", ago(hours=6), session_key=MAIN)
    board.move("old", "done", ago(minutes=1))  # updated most recently
    spool.tool("s1", ago(minutes=1))
    (s,) = adapter(tmp_path, spool).snapshot(NOW).sessions
    assert s.task_id == "live"
    board.move("live", "done", ago(seconds=30))
    (s,) = adapter(tmp_path, spool).snapshot(NOW).sessions
    assert s.task_id == "open"


def test_llm_spans_keep_their_task_without_a_session_span(
    tmp_path: Path, spool: Spool, board: Workboard
) -> None:
    key = "agent:dev:subagent:workboard-default-c1"
    board.card("c1", "running", ago(hours=1), session_key=key)
    spool.write(
        "llm_output", ago(minutes=1), {"usage": {"input": 10}}, sessionId="s9", sessionKey=key
    )
    (llm,) = adapter(tmp_path, spool).snapshot(NOW).spans
    assert (llm.kind, llm.task_id, llm.session_id, llm.trace_id) == (
        SpanKind.LLM,
        "c1",
        "s9",
        "openclaw-task-c1",
    )


def test_token_spans_can_be_turned_off(tmp_path: Path, spool: Spool) -> None:
    spool.write("llm_output", ago(minutes=1), {"usage": {"input": 10}}, sessionId="s1")
    spans = adapter(tmp_path, spool, token_spans=False).snapshot(NOW).spans
    assert all(sp.kind is not SpanKind.LLM for sp in spans)


def test_a_forged_command_is_redacted_again(tmp_path: Path, spool: Spool) -> None:
    spool.path.parent.mkdir(parents=True)
    line = {"type": "tool", "at": ago(minutes=1).timestamp(), "session_id": "s1"}
    line |= {"tool": "exec", "command": "curl -u admin:hunter2 https://x.test"}
    spool.path.write_text(json.dumps(line) + "\n")
    (tool,) = [
        sp for sp in adapter(tmp_path, spool).snapshot(NOW).spans if sp.kind is SpanKind.TOOL
    ]
    assert tool.attributes["command"] == "curl -u *** ***"


def test_spans(tmp_path: Path, spool: Spool) -> None:
    params = {"command": "curl -u admin:hunter2 https://x.test"}
    spool.tool("s1", ago(minutes=3), "exec", params, call="call_1")
    spool.tool("s1", ago(minutes=2), error=True)
    usage = {"input": 4000, "output": 40, "cacheRead": 12000, "cacheWrite": 100}
    spool.write(
        "llm_output",
        ago(minutes=1),
        {"provider": "anthropic", "model": "claude-x", "usage": usage},
        sessionId="s1",
        sessionKey=MAIN,
        trigger="heartbeat",
    )
    spans = adapter(tmp_path, spool).snapshot(NOW).spans
    by_kind: dict[SpanKind, list[Any]] = {}
    for sp in spans:
        by_kind.setdefault(sp.kind, []).append(sp)
    assert len(by_kind[SpanKind.SESSION]) == 1
    session = by_kind[SpanKind.SESSION][0]
    exec_call, read_call = sorted(by_kind[SpanKind.TOOL], key=lambda sp: sp.start)
    assert (exec_call.tool, exec_call.args_hash, exec_call.outcome) == (
        "exec",
        args_hash(params),
        "ok",
    )
    assert exec_call.attributes["command"] == "curl -u *** ***"
    assert exec_call.attributes["gen_ai.tool.call.id"] == "call_1"
    assert exec_call.duration == timedelta(seconds=1)
    assert read_call.outcome == "error"
    (llm,) = by_kind[SpanKind.LLM]
    assert (llm.input_tokens, llm.output_tokens, llm.cached_tokens) == (16100, 40, 12000)
    assert (llm.model, llm.session_id, llm.parent_id, llm.trace_id) == (
        "claude-x",
        "s1",
        session.span_id,
        session.trace_id,
    )
    assert llm.attributes[SOURCE] == HOOKS
    assert llm.attributes["openclaw.trigger"] == "heartbeat"
    # Stable ids across snapshots: the same line is the same span.
    again = adapter(tmp_path, spool).snapshot(NOW).spans
    assert {sp.span_id for sp in spans} == {sp.span_id for sp in again}


def test_lookback_and_future_lines(tmp_path: Path, spool: Spool) -> None:
    spool.tool("old", ago(hours=30))
    spool.tool("new", ago(minutes=1))
    with spool.path.open("a") as f:
        future = {"type": "tool", "at": (NOW + timedelta(hours=1)).timestamp(), "session_id": "f"}
        f.write(json.dumps(future) + "\n")
        f.write("garbage\n")
        f.write(
            json.dumps({"type": "tool", "at": NOW.timestamp(), "session_id": "x" * 20000}) + "\n"
        )
        f.write(json.dumps({"type": "other", "at": NOW.timestamp(), "session_id": "o"}) + "\n")
    assert [s.id for s in adapter(tmp_path, spool).snapshot(NOW).sessions] == ["new"]


def test_rotated_spool_is_read(tmp_path: Path, spool: Spool) -> None:
    spool.tool("s1", ago(minutes=5))
    spool.path.rename(spool.path.with_name(spool.path.name + ".1"))
    spool.tool("s2", ago(minutes=1))
    assert {s.id for s in adapter(tmp_path, spool).snapshot(NOW).sessions} == {"s1", "s2"}


def test_a_symlinked_spool_is_not_read(tmp_path: Path, spool: Spool) -> None:
    real = tmp_path / "real.jsonl"
    Spool(real).tool("s1", ago(minutes=1))
    spool.path.parent.mkdir(parents=True)
    spool.path.symlink_to(real)
    assert adapter(tmp_path, spool).snapshot(NOW).sessions == ()


def test_without_workboard(tmp_path: Path, spool: Spool, caplog: pytest.LogCaptureFixture) -> None:
    spool.tool("s1", ago(minutes=1))
    a = adapter(tmp_path, spool)
    with caplog.at_level(logging.WARNING):
        assert len(a.snapshot(NOW).sessions) == 1
        a.snapshot(NOW)
    assert sum("Workboard database" in r.message for r in caplog.records) == 1
    off = adapter(tmp_path, spool, workboard_db=None)
    assert off.snapshot(NOW).tasks == ()
    assert off.status_detail(NOW)[0] == "workboard: not read"


def test_an_unreadable_workboard_leaves_sessions(
    tmp_path: Path, spool: Spool, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "plugins" / "workboard" / "workboard.sqlite"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not sqlite" * 100)
    spool.tool("s1", ago(minutes=1))
    a = adapter(tmp_path, spool)
    with pytest.raises(OpenClawError) as err:
        a.board(NOW)
    assert err.value.public_reason == "the OpenClaw Workboard could not be read"
    with caplog.at_level(logging.WARNING):
        snap = a.snapshot(NOW)
        a.snapshot(NOW)
    assert (snap.tasks, [s.id for s in snap.sessions]) == ((), ["s1"])
    assert sum("sessions only" in r.message for r in caplog.records) == 1
    assert a.check() is not None


def test_check(tmp_path: Path, spool: Spool, board: Workboard) -> None:
    assert adapter(tmp_path, spool).check() is None
    assert adapter(tmp_path, spool, workboard_db=tmp_path / "none.sqlite").check() is None
    with sqlite3.connect(board.path) as db:
        db.execute("ALTER TABLE workboard_cards DROP COLUMN agent_id")
    assert "workboard_cards.agent_id" in (adapter(tmp_path, spool).check() or "")


def test_unknown_statuses_are_warned_once(
    tmp_path: Path, spool: Spool, board: Workboard, caplog: pytest.LogCaptureFixture
) -> None:
    board.card("c1", "parked", ago(hours=1))
    a = adapter(tmp_path, spool)
    with caplog.at_level(logging.WARNING):
        a.snapshot(NOW)
        a.snapshot(NOW)
    assert sum("'parked'" in r.message for r in caplog.records) == 1


def test_status_detail(tmp_path: Path, spool: Spool) -> None:
    a = adapter(tmp_path, spool)
    assert a.status_detail(NOW)[1].startswith("spool: missing")
    spool.tool("s1", ago(minutes=3))
    assert a.status_detail(NOW)[1].startswith("spool: last line 3m ago")
    assert OpenClawAdapter(state_dir=tmp_path, spool=None).status_detail(NOW)[1] == (
        "spool: disabled"
    )


def test_task_actions_fall_back_to_alerts(tmp_path: Path, spool: Spool) -> None:
    a = adapter(tmp_path, spool)
    for call in (
        lambda: a.requeue("c1", "x"),
        lambda: a.unblock("c1", "x"),
        lambda: a.comment("c1", "x"),
        lambda: a.wip_commit("c1", "x"),
    ):
        with pytest.raises(UnsupportedAction):
            call()
    assert list(a.events(NOW)) == []


def test_parse_line_rejects_bad_types() -> None:
    old = datetime.min.replace(tzinfo=UTC)
    line = {"type": "tool", "at": 1.0, "session_id": "s", "duration_ms": True, "v": "1"}
    event = parse_line(json.dumps(line), old)
    assert event is not None and (event.duration_ms, event.version) == (0, 0)
    assert parse_line(json.dumps({**line, "at": True}), old) is None
    assert parse_line(json.dumps({**line, "session_id": " "}), old) is None


# --- config ------------------------------------------------------------------------------


def test_config_defaults() -> None:
    s = load_openclaw({})
    assert s.enabled and s.spool == Path("~/.openclaw/kelpie/spool.jsonl")
    assert s.workboard == Path("~/.openclaw/plugins/workboard/workboard.sqlite")
    assert load_openclaw({"token_spans": False}).build().token_spans is False
    s = load_openclaw(
        {"state_dir": "/srv/oc", "workboard": False, "spool": False, "lookback": "2h"}
    )
    assert (s.workboard, s.spool, s.lookback) == (None, None, timedelta(hours=2))
    a = s.build()
    assert (a.workboard_db, a.spool, a.state_dir) == (None, None, Path("/srv/oc"))
    assert load_openclaw({"state_dir": "/srv/oc"}).workboard == Path(
        "/srv/oc/plugins/workboard/workboard.sqlite"
    )


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ([], "must be a table"),
        ({"bogus": 1}, "unknown"),
        ({"enabled": "yes"}, "enabled"),
        ({"token_spans": 1}, "token_spans"),
        ({"state_dir": ""}, "state_dir"),
        ({"workboard": True}, "workboard"),
        ({"spool": 3}, "spool"),
        ({"lookback": "soon"}, "lookback"),
        ({"lookback": "0s"}, "positive"),
    ],
)
def test_config_errors(section: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_openclaw(section)


def test_app_builds_the_adapter(tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(f'[adapters.openclaw]\nstate_dir = "{tmp_path}"\n')
    settings = load_settings(config)
    assert settings.openclaw is not None
    (built,) = build_adapters(settings)
    assert isinstance(built, OpenClawAdapter) and built.name == "openclaw"
    config.write_text("[adapters.openclaw]\nenabled = false\n")
    assert build_adapters(load_settings(config)) == []
    config.write_text("[adapters.openclaw]\nnope = 1\n")
    with pytest.raises(ConfigError, match="nope"):
        load_settings(config)
