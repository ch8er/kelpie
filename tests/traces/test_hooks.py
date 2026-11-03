"""Spans from hook events, and no double counting when OTLP reports the same calls."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import plugin
from kelpie.app import HermesSettings, Settings, build_app
from kelpie.core.model import Span, SpanKind
from kelpie.traces.hooks import (
    SESSION_END,
    SESSION_START,
    TOOL,
    HookEvent,
    is_hook_span,
    merge_hook_spans,
    spans_from_hook_events,
)
from kelpie.traces.otlp import PROTOBUF, decode
from kelpie.traces.semconv import map_spans
from tests.adapters.kanban import Board
from tests.traces.otlp_builders import S, request, sid

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
ARGS = {"command": "gh pr checks 5", "timeout": 30}


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def tool(n: int, at: datetime, **kw: object) -> HookEvent:
    base = HookEvent(
        kind=TOOL,
        at=at,
        key=f"k{n}",
        session_id="run-1",
        task_id="T-1",
        tool="terminal",
        args_hash=plugin.args_hash(ARGS),
        duration=timedelta(seconds=2),
        outcome="ok",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


# --- building spans ---------------------------------------------------------------------


def test_task_session_tool_tree() -> None:
    events = [
        tool(2, ago(minutes=4)),
        HookEvent(SESSION_START, ago(minutes=10), "s", session_id="run-1", task_id="T-1"),
        tool(1, ago(minutes=5), tool_call_id="call_1", attributes={"command": "gh pr"}),
        HookEvent(SESSION_END, ago(minutes=1), "e", session_id="run-1"),
        tool(3, ago(minutes=2), session_id=None, task_id=None, tool=None, args_hash=None),
    ]
    task, session, first, second, bare = spans_from_hook_events(events, prefix="x")
    assert (task.kind, session.kind) == (SpanKind.TASK, SpanKind.SESSION)
    assert task.start == session.start == ago(minutes=10) and task.end is None
    assert session.end == ago(minutes=1) and session.parent_id == task.span_id
    assert session.task_id == "T-1" and session.name == "invoke_agent"
    assert [first.span_id, second.span_id] == ["x-tool-k1", "x-tool-k2"]
    assert first.parent_id == session.span_id and first.trace_id == task.trace_id == "x-task-T-1"
    assert first.start == ago(minutes=5, seconds=2) and first.end == ago(minutes=5)
    assert first.attributes == {
        "command": "gh pr",
        "gen_ai.operation.name": "execute_tool",
        "kelpie.source": "hooks",
        "gen_ai.tool.call.id": "call_1",
    }
    assert bare.parent_id is None and bare.trace_id == "x-task-none" and bare.name == "execute_tool"
    assert all(is_hook_span(s) for s in (task, session, first, bare))


def test_open_session_and_session_without_task() -> None:
    spans = spans_from_hook_events(
        [
            HookEvent(SESSION_END, ago(minutes=9), "e", session_id="run-2"),
            tool(1, ago(minutes=3), session_id="run-2", task_id=None),
            tool(2, ago(minutes=2), session_id=None, task_id="T-3"),
        ],
        prefix="x",
    )
    task, session, in_session, in_task = spans
    assert session.end is None  # a tool call came after the last session end
    assert session.trace_id == "run-2" and session.parent_id is None
    assert in_session.parent_id == session.span_id
    assert in_task.parent_id == task.span_id and in_task.trace_id == "x-task-T-3"


def test_raw_args_hash_like_otlp_and_are_dropped() -> None:
    (span,) = [
        s
        for s in spans_from_hook_events([tool(1, NOW, args=ARGS, args_hash=None)], "x")
        if s.kind is SpanKind.TOOL
    ]
    otlp = map_spans(
        decode(
            request(
                [
                    S(
                        sid(1),
                        attrs={
                            "gen_ai.operation.name": "execute_tool",
                            "gen_ai.tool.name": "terminal",
                            "gen_ai.tool.call.arguments": json.dumps(ARGS),
                        },
                    )
                ]
            ),
            PROTOBUF,
        ).spans
    ).spans[0]
    assert span.args_hash == otlp.args_hash == plugin.args_hash(ARGS)
    assert "gh pr checks" not in repr(span)


def test_negative_duration_starts_at_the_event() -> None:
    (_, _, span) = spans_from_hook_events([tool(1, NOW, duration=timedelta(seconds=-1))], "x")
    assert span.start == span.end == NOW


# --- no double counting -----------------------------------------------------------------


def otlp_span(n: int, start: datetime, **kw: object) -> Span:
    base = Span(
        trace_id="a" * 32,
        span_id=f"{n:016x}",
        name="execute_tool terminal",
        kind=SpanKind.TOOL,
        start=start,
        end=start + timedelta(seconds=2),
        session_id="run-1",
        tool="terminal",
        args_hash=plugin.args_hash(ARGS),
        attributes={"gen_ai.operation.name": "execute_tool"},
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def hook_tools(*events: HookEvent) -> list[Span]:
    return [s for s in spans_from_hook_events(events, "x") if s.kind is SpanKind.TOOL]


def kept(spans: list[Span]) -> list[str]:
    return [s.span_id for s in merge_hook_spans(spans).spans]


def test_same_call_id_keeps_the_otlp_span() -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5), tool_call_id="call_1"))
    otlp = otlp_span(
        1,
        ago(minutes=30),  # timing differs: the id decides
        args_hash=None,
        attributes={"gen_ai.tool.call.id": "call_1"},
    )
    assert kept([otlp, hook]) == [otlp.span_id]


@pytest.mark.parametrize("session", ["otel-conversation-42", None])
def test_same_call_id_merges_whatever_the_session(session: str | None) -> None:
    """Exporters need not tag spans with Kelpie's session id: the call id is enough."""
    (hook,) = hook_tools(
        tool(1, ago(minutes=5), tool_call_id="call_1", attributes={"command": "gh pr checks 5"})
    )
    otlp = otlp_span(
        1, ago(minutes=5), session_id=session, args_hash=None,
        attributes={"gen_ai.tool.call.id": "call_1"},
    )  # fmt: skip
    [merged] = merge_hook_spans([otlp, hook]).spans
    assert merged.span_id == otlp.span_id
    assert merged.session_id == hook.session_id  # rules still see the run
    assert merged.args_hash == hook.args_hash


def test_same_call_id_but_another_tool_is_not_a_duplicate() -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5), tool_call_id="call_1"))
    otlp = otlp_span(1, hook.start, tool="read_file", attributes={"gen_ai.tool.call.id": "call_1"})
    assert kept([otlp, hook]) == [otlp.span_id, hook.span_id]


def test_a_call_id_is_matched_once() -> None:
    hooks = hook_tools(
        tool(1, ago(minutes=5), tool_call_id="call_1"),
        tool(2, ago(minutes=4), tool_call_id="call_1"),
    )
    otlp = otlp_span(
        1, ago(minutes=5), session_id="chat-9", attributes={"gen_ai.tool.call.id": "call_1"}
    )
    merged = merge_hook_spans([otlp, *hooks])
    assert merged.merged == 1 and len(merged.spans) == 2


def test_without_session_and_call_id_nothing_matches() -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5)))
    otlp = otlp_span(1, hook.start, session_id=None)
    assert kept([otlp, hook]) == [otlp.span_id, hook.span_id]


def test_different_call_ids_are_different_calls() -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5), tool_call_id="call_2"))
    otlp = otlp_span(1, hook.start, attributes={"gen_ai.tool.call.id": "call_1"})
    assert kept([otlp, hook]) == [otlp.span_id, hook.span_id]


@pytest.mark.parametrize(
    ("shift", "duplicate"), [(0.0, True), (1.9, True), (-2.0, True), (2.5, False)]
)
def test_fallback_on_tool_args_and_start(shift: float, duplicate: bool) -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5), tool_call_id="call_1"))
    otlp = otlp_span(1, hook.start + timedelta(seconds=shift))  # no call id on the OTLP side
    expected = [otlp.span_id] if duplicate else [otlp.span_id, hook.span_id]
    assert kept([otlp, hook]) == expected


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": "run-9"},
        {"tool": "read_file"},
        {"args_hash": "ffffffffffffffff"},
        {"session_id": None},
        {"kind": SpanKind.LLM},
    ],
)
def test_lookalikes_are_not_duplicates(change: dict[str, object]) -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5)))
    otlp = otlp_span(1, hook.start, **change)
    assert kept([otlp, hook]) == [otlp.span_id, hook.span_id]


def test_hook_spans_without_args_or_session_are_kept() -> None:
    (no_args,) = hook_tools(tool(1, ago(minutes=5), args_hash=None))
    (no_session,) = hook_tools(tool(2, ago(minutes=5), session_id=None))
    otlp = otlp_span(1, no_args.start)
    assert kept([otlp, no_args, no_session]) == [otlp.span_id, no_args.span_id, no_session.span_id]


def test_only_hooks_or_only_otlp_pass_through() -> None:
    hooks = hook_tools(tool(1, NOW), tool(2, NOW))
    assert kept(hooks) == [s.span_id for s in hooks]
    assert merge_hook_spans([]).spans == ()


def test_merged_span_keeps_what_only_the_hook_knows() -> None:
    (hook,) = hook_tools(
        tool(1, ago(minutes=5), tool_call_id="call_1", attributes={"command": "gh pr checks 5"})
    )
    otlp = otlp_span(
        1,
        hook.start,
        args_hash=None,
        task_id=None,
        attributes={"gen_ai.tool.call.id": "call_1", "gen_ai.operation.name": "execute_tool"},
        input_tokens=0,
    )
    result = merge_hook_spans([otlp, hook])
    (span,) = result.spans
    assert result.merged == 1
    assert span.span_id == otlp.span_id and not is_hook_span(span)
    assert span.args_hash == hook.args_hash and span.task_id == "T-1"
    assert span.attributes["command"] == "gh pr checks 5"
    assert span.attributes["gen_ai.operation.name"] == "execute_tool"


def test_otlp_without_args_hash_matches_on_tool_and_time() -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5)))
    otlp = otlp_span(1, hook.start + timedelta(seconds=1), args_hash=None)
    (span,) = merge_hook_spans([otlp, hook]).spans
    assert span.args_hash == hook.args_hash


def test_matching_is_one_to_one_nearest_first() -> None:
    a, b = hook_tools(tool(1, ago(minutes=5)), tool(2, ago(minutes=5, seconds=-1)))
    otlp = otlp_span(1, b.start)  # nearest to b
    result = merge_hook_spans([otlp, a, b])
    assert result.merged == 1
    assert [s.span_id for s in result.spans] == [otlp.span_id, a.span_id]


def test_hook_without_duration_matches_on_end() -> None:
    (hook,) = hook_tools(tool(1, ago(minutes=5), duration=None))  # start == end
    otlp = otlp_span(1, ago(minutes=5, seconds=10), end=ago(minutes=5, seconds=-1))
    assert merge_hook_spans([otlp, hook]).merged == 1


def test_otlp_cannot_claim_to_be_a_hook_span() -> None:
    from kelpie.traces.semconv import map_spans

    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "terminal",
        "kelpie.source": "hooks",
        "kelpie.session_id": "run-1",
    }
    (span,) = map_spans(decode(request([S(sid(1), attrs=attrs)]), PROTOBUF).spans).spans
    assert "kelpie.source" not in span.attributes and not is_hook_span(span)


def test_wait_condition_still_sees_polling_with_otlp_enabled() -> None:
    """Hermes reports a CI poll loop through hooks (with the command) and OTLP (without);
    after merging, rule 2 still recognizes the polling."""
    from kelpie.core.actions import ActionKind
    from kelpie.core.model import Session, Snapshot
    from kelpie.rules.wait_condition import WaitCondition

    events = [
        tool(
            n,
            ago(minutes=9 - n),
            tool_call_id=f"call_{n}",
            attributes={"command": "gh pr checks 5"},
        )
        for n in range(8)
    ]
    hooks = hook_tools(*events)
    otlp = [
        otlp_span(
            n + 1,
            h.start,
            args_hash=None,
            attributes={"gen_ai.tool.call.id": f"call_{n}"},
        )
        for n, h in enumerate(hooks)
    ]
    merged = merge_hook_spans([*otlp, *hooks])
    assert merged.merged == 8 and len(merged.spans) == 8
    session = Session("run-1", "T-1", "reviewer", ago(minutes=30), last_heartbeat=NOW)
    snapshot = Snapshot(now=NOW, sessions=(session,), spans=merged.spans)
    rule = WaitCondition(poll_patterns={"ci_status": ("gh pr checks",)})
    actions = rule.evaluate(snapshot)
    assert [a.kind for a in actions] == [ActionKind.COMMENT]
    assert actions[0].params["condition_kind"] == "ci_status"


# --- end to end: Hermes spool + OTLP ingest ---------------------------------------------


def test_hermes_hooks_and_otlp_are_not_double_counted(tmp_path: Path) -> None:
    board = Board(tmp_path / "kanban.db")
    board.task("t_1", "running", NOW - timedelta(hours=1), assignee="dev")
    spool = tmp_path / "spool.jsonl"
    at = NOW - timedelta(minutes=5)
    lines = [
        {"type": "tool", "at": at.timestamp(), "task_id": "t_1", "run_id": "7",
         "tool": "terminal", "tool_call_id": "call_1", "args_hash": plugin.args_hash(ARGS),
         "outcome": "ok", "duration_ms": 1000},
        {"type": "tool", "at": at.timestamp() + 60, "task_id": "t_1", "run_id": "7",
         "tool": "terminal", "tool_call_id": "call_2", "args_hash": plugin.args_hash(ARGS),
         "outcome": "ok", "duration_ms": 1000},
    ]  # fmt: skip
    spool.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    settings = Settings(
        db_path=tmp_path / "kelpie.db", hermes=HermesSettings(db_path=board.path, spool=spool)
    )
    app = build_app(settings, clock=lambda: NOW)
    assert app.span_store is not None
    # Hermes also exports the first call over OTLP.
    otlp_call = S(
        sid(1),
        start=at - timedelta(seconds=1),
        attrs={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "terminal",
            "gen_ai.tool.call.id": "call_1",
            "kelpie.session_id": "hermes-run-7",
        },
    )
    app.span_store.append_spans(map_spans(decode(request([otlp_call]), PROTOBUF).spans).spans, NOW)
    tools = [s for s in app.loop.snapshot_source().spans if s.kind is SpanKind.TOOL]
    assert len(tools) == 2
    assert {s.attributes["gen_ai.tool.call.id"] for s in tools} == {"call_1", "call_2"}
    assert [is_hook_span(s) for s in tools] == [False, True]  # call_1 from OTLP
    app.close()


def test_a_merged_span_keeps_the_hooks_exit_code_and_result() -> None:
    from kelpie.core.tools import EXIT_CODE_ATTRIBUTE, RESULT_ATTRIBUTE, exit_code_of, result_of

    attrs = {EXIT_CODE_ATTRIBUTE: 127, RESULT_ATTRIBUTE: "error"}
    (hook,) = hook_tools(tool(1, ago(minutes=5), tool_call_id="call_1", attributes=attrs))
    otlp = otlp_span(1, hook.start, attributes={"gen_ai.tool.call.id": "call_1"})
    (span,) = merge_hook_spans([otlp, hook]).spans
    assert (result_of(span), exit_code_of(span)) == ("error", 127)
