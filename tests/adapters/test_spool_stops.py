"""``spool_stops = false`` (#302 option 2): Claude Code and OpenClaw spool lines alone never
stop a run; rule 10 alerts on them, and OTLP evidence of the same run still stops it."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.claude_code import ClaudeCodeAdapter
from kelpie.adapters.claude_code.config import load_claude_code
from kelpie.adapters.openclaw.config import load_openclaw
from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Snapshot, Span, SpanKind
from kelpie.core.spans import UNVERIFIED_ATTRIBUTE
from kelpie.remote.protocol import decode_snapshot, encode_snapshot, namespace_snapshot
from kelpie.rules.loop_breaker import LoopBreaker
from kelpie.traces.hooks import merge_hook_spans
from tests.adapters.test_openclaw_adapter import Spool
from tests.adapters.test_openclaw_adapter import adapter as openclaw_adapter

NOW = datetime.now(UTC).replace(microsecond=0)
VICTIM = "0b5c6f7e-0000-4000-8000-0000000000a1"


def _at(seconds: float) -> datetime:
    return NOW - timedelta(seconds=seconds)


def claude_spool(path: Path, calls: int = 10, session: str = VICTIM) -> None:
    """``calls`` identical Bash calls naming ``session``, as any process on the host can
    append them; then a fresh prompt so the session is live."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(
            json.dumps({"type": "session_start", "at": _at(600).timestamp(), "session_id": session})
            + "\n"
        )
        for i in range(calls):
            rec = {
                "type": "tool",
                "at": _at(300 - 20 * i).timestamp(),
                "session_id": session,
                "tool": "Bash",
                "tool_call_id": f"toolu_{i}",
                "args_hash": "ab" * 8,
                "outcome": "ok",
                "duration_ms": 500,
            }
            f.write(json.dumps(rec) + "\n")


def otlp_calls(calls: int = 10, session: str = VICTIM) -> list[Span]:
    """Claude Code's own OTLP tool spans for the same calls (same ``tool_use_id``)."""
    return [
        Span(
            trace_id="otlp",
            span_id=f"o{i}",
            name="claude_code.tool",
            kind=SpanKind.TOOL,
            start=_at(300 - 20 * i) - timedelta(seconds=1),
            end=_at(300 - 20 * i),
            session_id=session,
            tool="Bash",
            args_hash="ab" * 8,
            outcome="ok",
            attributes={"gen_ai.tool.call.id": f"toolu_{i}"},
        )
        for i in range(calls)
    ]


def evaluate(snapshot: Snapshot, extra: list[Span] | None = None) -> list[Action]:
    spans = merge_hook_spans((*(extra or ()), *snapshot.spans)).spans
    return list(LoopBreaker().evaluate(replace(snapshot, spans=spans)))


def stops(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a.kind is ActionKind.STOP]


def tools(snapshot: Snapshot) -> list[Span]:
    return [s for s in snapshot.spans if s.kind is SpanKind.TOOL]


def claude(tmp_path: Path, **kw: Any) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(
        tasks_dir=tmp_path / "tasks",
        spool=tmp_path / "spool.jsonl",
        process_check=lambda pid, recorded: None,
        **kw,
    )


# --- config ---------------------------------------------------------------------------


@pytest.mark.parametrize("load", [load_claude_code, load_openclaw])
def test_spool_stops_defaults_to_true_and_takes_a_bool(load: Any) -> None:
    assert load({}).spool_stops is True
    assert load({"spool_stops": False}).spool_stops is False
    assert load({"spool_stops": False}).build().spool_stops is False
    with pytest.raises(ValueError, match="spool_stops must be true or false"):
        load({"spool_stops": "no"})


# --- Claude Code ----------------------------------------------------------------------


def test_claude_code_spool_lines_stop_by_default(tmp_path: Path) -> None:
    claude_spool(tmp_path / "spool.jsonl")
    snapshot = claude(tmp_path).snapshot(NOW)
    assert not any(UNVERIFIED_ATTRIBUTE in s.attributes for s in tools(snapshot))
    assert [a.params["session_id"] for a in stops(evaluate(snapshot))] == [VICTIM]


def test_claude_code_spool_lines_alone_only_alert_with_spool_stops_off(tmp_path: Path) -> None:
    claude_spool(tmp_path / "spool.jsonl")
    snapshot = claude(tmp_path, spool_stops=False).snapshot(NOW)
    found = tools(snapshot)
    assert len(found) == 10 and all(s.attributes[UNVERIFIED_ATTRIBUTE] is True for s in found)
    actions = evaluate(snapshot)
    assert stops(actions) == []
    alert = next(a for a in actions if a.kind is ActionKind.ALERT)
    assert alert.params["session_id"] == VICTIM and alert.params["unverified"] is True
    assert "Not stopped" in alert.reason


def test_claude_code_otlp_evidence_still_stops_with_spool_stops_off(tmp_path: Path) -> None:
    """The run's own OTLP spans (under its per-agent token) are evidence of their own."""
    claude_spool(tmp_path / "spool.jsonl")
    snapshot = claude(tmp_path, spool_stops=False).snapshot(NOW)
    actions = evaluate(snapshot, otlp_calls())
    assert [a.params["session_id"] for a in stops(actions)] == [VICTIM]


def test_claude_code_spool_stops_off_marks_tool_lines_only(tmp_path: Path) -> None:
    claude_spool(tmp_path / "spool.jsonl", calls=1)
    spans = claude(tmp_path, spool_stops=False).snapshot(NOW).spans
    assert any(s.kind is SpanKind.SESSION for s in spans)
    assert all(
        UNVERIFIED_ATTRIBUTE not in s.attributes for s in spans if s.kind is not SpanKind.TOOL
    )


# --- OpenClaw -------------------------------------------------------------------------


def openclaw_loop(spool: Spool, session: str = "oc-session-1") -> None:
    for i in range(10):
        spool.tool(session, _at(300 - 20 * i), "exec", {"command": "make check"})


@pytest.fixture
def spool(tmp_path: Path) -> Spool:
    return Spool(tmp_path / "kelpie" / "spool.jsonl")


def test_openclaw_spool_lines_stop_by_default(tmp_path: Path, spool: Spool) -> None:
    openclaw_loop(spool)
    snapshot = openclaw_adapter(tmp_path, spool).snapshot(NOW)
    assert not any(UNVERIFIED_ATTRIBUTE in s.attributes for s in tools(snapshot))
    assert [a.params["session_id"] for a in stops(evaluate(snapshot))] == ["oc-session-1"]


def test_openclaw_spool_lines_alone_only_alert_with_spool_stops_off(
    tmp_path: Path, spool: Spool
) -> None:
    openclaw_loop(spool)
    snapshot = openclaw_adapter(tmp_path, spool, spool_stops=False).snapshot(NOW)
    found = tools(snapshot)
    assert len(found) == 10 and all(s.attributes[UNVERIFIED_ATTRIBUTE] is True for s in found)
    # LLM spans from the spool are not tool evidence and stay unmarked
    assert all(
        UNVERIFIED_ATTRIBUTE not in s.attributes
        for s in snapshot.spans
        if s.kind is not SpanKind.TOOL
    )
    actions = evaluate(snapshot)
    assert stops(actions) == []
    assert any(a.params.get("unverified") is True for a in actions if a.kind is ActionKind.ALERT)


def test_openclaw_sessionless_otlp_spans_do_not_turn_spool_lines_into_a_stop(
    tmp_path: Path, spool: Spool
) -> None:
    """``diagnostics-otel`` spans carry no session: a marked line with the same call id folds
    into them but never moves them into the session it names."""
    for i in range(10):
        spool.tool("oc-session-1", _at(300 - 20 * i), "exec", {"command": "make"}, call=f"c{i}")
    snapshot = openclaw_adapter(tmp_path, spool, spool_stops=False).snapshot(NOW)
    otlp = [
        Span(
            trace_id="otlp",
            span_id=f"o{i}",
            name="execute_tool exec",
            kind=SpanKind.TOOL,
            start=_at(300 - 20 * i) - timedelta(seconds=1),
            end=_at(300 - 20 * i),
            tool="exec",
            outcome="ok",
            attributes={"gen_ai.tool.call.id": f"c{i}"},
        )
        for i in range(10)
    ]
    result = merge_hook_spans((*otlp, *snapshot.spans))
    assert result.merged == 10
    merged = result.spans
    assert all(s.session_id is None for s in merged if s.kind is SpanKind.TOOL)
    actions = list(LoopBreaker().evaluate(replace(snapshot, spans=merged)))
    assert stops(actions) == []


def test_the_mark_survives_the_agent_to_hub_wire(tmp_path: Path) -> None:
    """With ``kelpie agent`` the adapter runs on the agent host; the hub's rule 10 must see
    the mark."""
    claude_spool(tmp_path / "spool.jsonl", calls=2)
    snapshot = claude(tmp_path, spool_stops=False).snapshot(NOW)
    wire = json.loads(json.dumps(encode_snapshot(snapshot)))
    hub = namespace_snapshot("host-a", "claude-code", decode_snapshot(wire))
    found = tools(hub)
    assert len(found) == 2 and all(s.attributes[UNVERIFIED_ATTRIBUTE] is True for s in found)
