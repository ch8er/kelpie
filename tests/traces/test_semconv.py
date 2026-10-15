"""GenAI semantic conventions -> Span: kinds, tokens, linkage, privacy, args hash."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from kelpie.adapters.hermes import plugin
from kelpie.core.model import SpanKind
from kelpie.traces.args import args_hash, args_hash_of_attribute
from kelpie.traces.otlp import PROTOBUF, decode
from kelpie.traces.semconv import Mapped, MappingOptions, is_kept, map_spans
from tests.traces.otlp_builders import NOW, S, request, sid


def mapped(
    spans: list[S], resource: dict[str, Any] | None = None, options: MappingOptions | None = None
) -> Mapped:
    return map_spans(decode(request(spans, resource), PROTOBUF).spans, options)


def one(attrs: dict[str, Any], name: str = "op", **kw: Any) -> Any:
    result = mapped([S(sid(1), name=name, attrs=attrs, **kw)])
    assert len(result.spans) == 1, result
    return result.spans[0]


LLM = {
    "gen_ai.operation.name": "chat",
    "gen_ai.provider.name": "synthetic",
    "gen_ai.request.model": "model-a",
    "gen_ai.response.model": "model-a-2026",
    "gen_ai.usage.input_tokens": 80_000,
    "gen_ai.usage.output_tokens": 512,
    "gen_ai.usage.cache_read.input_tokens": 76_000,
    "gen_ai.usage.cache_write.input_tokens": 1_000,
}


def test_llm_span_fields() -> None:
    s = one(LLM, start=NOW, duration=timedelta(seconds=3))
    assert s.kind is SpanKind.LLM and s.model == "model-a-2026"
    assert (s.input_tokens, s.output_tokens, s.cached_tokens) == (80_000, 512, 76_000)
    assert s.start == NOW and s.duration == timedelta(seconds=3) and s.outcome == "ok"
    assert s.attributes == {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "synthetic",
        "gen_ai.usage.cache_write.input_tokens": 1_000,
        "service.name": "synthetic-agent",
    }


@pytest.mark.parametrize(
    ("op", "kind"),
    [
        ("chat", SpanKind.LLM),
        ("text_completion", SpanKind.LLM),
        ("generate_content", SpanKind.LLM),
        ("embeddings", SpanKind.LLM),
        ("execute_tool", SpanKind.TOOL),
        ("invoke_agent", SpanKind.SESSION),
        ("invoke_workflow", SpanKind.TASK),
    ],
)
def test_operation_kinds(op: str, kind: SpanKind) -> None:
    assert one({"gen_ai.operation.name": op}).kind is kind


@pytest.mark.parametrize(
    ("attrs", "kind"),
    [
        ({"kelpie.span.kind": "TASK"}, SpanKind.TASK),
        ({"gen_ai.tool.name": "shell"}, SpanKind.TOOL),
        ({"gen_ai.request.model": "m"}, SpanKind.LLM),
        ({"gen_ai.usage.prompt_tokens": 5}, SpanKind.LLM),
        ({"gen_ai.agent.name": "reviewer"}, SpanKind.SESSION),
        ({"gen_ai.operation.name": "retrieval", "gen_ai.usage.input_tokens": 9}, SpanKind.LLM),
    ],
)
def test_kind_without_known_operation(attrs: dict[str, Any], kind: SpanKind) -> None:
    assert one(attrs).kind is kind


def test_non_genai_spans_are_ignored_but_still_link() -> None:
    result = mapped(
        [
            S(sid(1), name="HTTP POST", attrs={"kelpie.task_id": "T-1", "session.id": "s-1"}),
            S(sid(2), name="retrieval", attrs={"gen_ai.operation.name": "retrieval"}),
            S(sid(3), parent=sid(1), attrs={"gen_ai.operation.name": "chat"}),
        ]
    )
    assert result.ignored == 2
    (s,) = result.spans
    assert (s.task_id, s.session_id) == ("T-1", "s-1")


def test_deprecated_token_names() -> None:
    s = one({"gen_ai.usage.prompt_tokens": 10, "gen_ai.usage.completion_tokens": 3})
    assert (s.input_tokens, s.output_tokens) == (10, 3)


@pytest.mark.parametrize("bad", ["many", -5, True, 1.9e30, [1]])
def test_bad_token_values_count_as_zero_or_clamp(bad: Any) -> None:
    s = one({"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": bad})
    assert 0 <= s.input_tokens <= 2**62


def test_outcome() -> None:
    assert one({"gen_ai.operation.name": "chat"}, error=True).outcome == "error"
    assert one({"gen_ai.operation.name": "chat", "error.type": "timeout"}).outcome == "error"
    assert one({"gen_ai.operation.name": "chat"}).outcome == "ok"


# --- linkage ----------------------------------------------------------------------------


def test_linkage_defaults_fallbacks_and_inheritance() -> None:
    chat = {"gen_ai.operation.name": "chat"}
    result = mapped(
        [
            S(sid(1), attrs={"gen_ai.operation.name": "invoke_agent", "kelpie.task_id": "T-1"}),
            S(sid(2), parent=sid(1), attrs={**chat, "gen_ai.conversation.id": "conv-1"}),
            S(sid(3), parent=sid(2), attrs=chat),  # inherits task and session
            S(sid(4), parent=sid(3), attrs={**chat, "kelpie.session_id": "own"}),
            S(sid(5), parent=sid(99), attrs=chat),  # parent not in this request
        ]
    )
    links = [(s.task_id, s.session_id) for s in result.spans]
    assert links == [
        ("T-1", None),
        ("T-1", "conv-1"),
        ("T-1", "conv-1"),
        ("T-1", "own"),
        (None, None),
    ]


def test_linkage_from_resource_and_custom_keys() -> None:
    options = MappingOptions(task_attribute="farm.task", session_attribute="farm.run")
    resource = {"farm.task": "T-9", "farm.run": "run-3", "kelpie.task_id": "ignored"}
    (s,) = mapped([S(sid(1), attrs={"gen_ai.operation.name": "chat"})], resource, options).spans
    assert (s.task_id, s.session_id) == ("T-9", "run-3")
    (s,) = mapped(
        [S(sid(1), attrs={"gen_ai.operation.name": "chat", "farm.task": 7})], {}, options
    ).spans
    assert s.task_id == "7"


def test_parent_cycle_does_not_hang() -> None:
    chat = {"gen_ai.operation.name": "chat"}
    result = mapped([S(sid(1), parent=sid(2), attrs=chat), S(sid(2), parent=sid(1), attrs=chat)])
    assert [(s.task_id, s.session_id) for s in result.spans] == [(None, None), (None, None)]


# --- tools, args hash, privacy ----------------------------------------------------------


def test_tool_span_hashes_arguments_and_never_keeps_them() -> None:
    args = {"command": "pytest -q", "cwd": "/work"}
    s = one(
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "shell",
            "gen_ai.tool.call.id": "call-1",
            "gen_ai.tool.call.arguments": '{"cwd": "/work", "command": "pytest -q"}',
            "gen_ai.tool.call.result": "3 passed",
        }
    )
    assert s.kind is SpanKind.TOOL and s.tool == "shell"
    assert s.args_hash == args_hash(args) == plugin.args_hash(args)
    assert s.attributes["gen_ai.tool.call.id"] == "call-1"
    flat = repr(s)
    assert "pytest" not in flat and "/work" not in flat and "passed" not in flat


def test_structured_and_non_json_arguments() -> None:
    assert args_hash_of_attribute({"a": 1}) == args_hash({"a": 1})
    assert args_hash_of_attribute("not json") == args_hash("not json")
    s = one({"gen_ai.tool.name": "t", "gen_ai.tool.call.arguments": {"b": [1, 2]}})
    assert s.args_hash == args_hash({"b": [1, 2]})
    assert one({"gen_ai.tool.name": "t"}).args_hash is None


@pytest.mark.parametrize(
    "args",
    [
        {"command": "ls", "n": 3},
        {"nested": {"z": 1, "a": [True, None, 1.5]}},
        ["x", 1],
        "plain",
        None,
        {"when": NOW},  # default=str
        {1: "int key"},
    ],
)
def test_hermes_plugin_hash_is_identical(args: Any) -> None:
    assert plugin.args_hash(args) == args_hash(args)


def test_plugin_and_traces_hash_share_source() -> None:
    import inspect

    from kelpie.traces import args as traces_args

    assert inspect.getsource(plugin.args_hash) == inspect.getsource(traces_args.args_hash)


# Content and identity attributes, by the vendor that emits them. None may survive.
CONTENT_BY_VENDOR: dict[str, dict[str, Any]] = {
    "otel-genai": {
        "gen_ai.input.messages": '[{"role": "user", "parts": ["secret plan"]}]',
        "gen_ai.output.messages": "secret answer",
        "gen_ai.system_instructions": "secret system",
        "gen_ai.tool.definitions": "[secret]",
        "gen_ai.tool.call.result": "secret result",
        "gen_ai.prompt": "secret",
        "gen_ai.retrieval.query.text": "secret",
    },
    "openllmetry": {
        "gen_ai.prompt.0.content": "secret",
        "gen_ai.completion.0.content": "secret",
        "traceloop.entity.input": "secret",
        "traceloop.entity.output": "secret",
    },
    "openinference": {
        "llm.input_messages.0.message.content": "secret",
        "input.value": "secret",
        "output.value": "secret",
    },
    "vercel-ai-sdk": {
        "ai.response.text": "secret",
        "ai.response.toolCalls": "secret",
        "ai.response.reasoning": "secret",
        "ai.toolCall.args": "secret",
        "ai.toolCall.result": "secret",
    },
    "langfuse": {"langfuse.observation.input": "secret", "langfuse.observation.output": "x"},
    "mlflow": {"mlflow.spanInputs": "secret", "mlflow.spanOutputs": "secret"},
    "identity": {
        "user.email": "someone@example.invalid",
        "user.id": "someone",
        "user.name": "someone",
        "user.full_name": "someone",
        "organization.id": "someone-org",
        "enduser.id": "someone",
    },
    "errors": {"error.message": "secret in error", "exception.stacktrace": "secret"},
    "unknown-vendor": {"acme.request.body": "secret", "custom_prompt_text": "secret"},
}


@pytest.mark.parametrize("vendor", sorted(CONTENT_BY_VENDOR))
def test_content_and_identity_are_dropped(vendor: str) -> None:
    s = one({"gen_ai.operation.name": "chat", **CONTENT_BY_VENDOR[vendor]})
    assert set(s.attributes) == {"gen_ai.operation.name", "service.name"}
    assert "secret" not in repr(s) and "someone" not in repr(s)


def test_claude_code_content_is_dropped() -> None:
    attrs = {
        "tool_name": "Bash",
        "tool_parameters": '{"command": "cat secret.txt"}',
        "full_command": "cat secret.txt",
        "file_path": "/home/someone/secret.txt",
        "user_prompt": "secret",
        "error": "secret failure text",
        "user.email": "someone@example.invalid",
        "user.account_uuid": "someone",
        "organization.id": "someone-org",
        "duration_ms": 12,
    }
    s = one(attrs, name="claude_code.tool")
    assert set(s.attributes) == {"duration_ms", "service.name"}
    assert "secret" not in repr(s) and "someone" not in repr(s)
    assert s.args_hash == args_hash({"command": "cat secret.txt"})  # tool_parameters wins


@pytest.mark.parametrize(
    ("key", "claude_code", "kept"),
    [
        ("gen_ai.operation.name", False, True),
        ("gen_ai.tool.call.id", False, True),
        ("gen_ai.usage.reasoning.output_tokens", False, True),
        ("gen_ai.response.finish_reasons", False, True),
        ("error.type", False, True),
        ("kelpie.team", False, True),
        ("duration_ms", False, False),  # Claude Code keys only on claude_code.* spans
        ("duration_ms", True, True),
        ("gen_ai.input.messages", False, False),
        ("ai.response.text", True, False),
    ],
)
def test_is_kept(key: str, claude_code: bool, kept: bool) -> None:
    assert is_kept(key, claude_code) is kept


def test_kept_values_are_bounded_scalars() -> None:
    attrs: dict[str, Any] = {f"gen_ai.usage.k{i:03}": i for i in range(100)}
    attrs["gen_ai.operation.name"] = "chat"
    assert len(one(attrs).attributes) == 64
    s = one(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.response.id": "x" * 1000,
            "gen_ai.response.finish_reasons": ["stop", {"a": 1}, *["length"] * 30],
            "gen_ai.usage.cache_write.input_tokens": "lots",  # usage must be numeric
            "gen_ai.agent.name": ["not", "a", "scalar"],
            "kelpie.extra": {"a": 1},
        }
    )
    assert len(s.attributes["gen_ai.response.id"]) == 256
    assert s.attributes["gen_ai.response.finish_reasons"] == ["stop", *["length"] * 15]
    for key in ("gen_ai.usage.cache_write.input_tokens", "gen_ai.agent.name", "kelpie.extra"):
        assert key not in s.attributes


def test_name_is_bounded_and_end_never_before_start() -> None:
    s = one({"gen_ai.operation.name": "chat"}, name="n" * 500, duration=timedelta(seconds=-5))
    assert len(s.name) == 256 and s.end == s.start


# --- Claude Code ------------------------------------------------------------------------


def test_claude_code_spans() -> None:
    resource = {"service.name": "claude-code"}
    result = mapped(
        [
            S(
                sid(1),
                name="claude_code.interaction",
                attrs={"session.id": "cc-1", "user_prompt": "secret"},
            ),
            S(
                sid(2),
                name="claude_code.llm_request",
                parent=sid(1),
                attrs={
                    "model": "synthetic-model",
                    "input_tokens": 12,
                    "output_tokens": 30,
                    "cache_read_tokens": 4000,
                    "cache_creation_tokens": 100,
                    "success": True,
                },
            ),
            S(
                sid(3),
                name="claude_code.tool",
                parent=sid(1),
                attrs={
                    "tool_name": "Bash",
                    "tool_use_id": "toolu_1",
                    "full_command": "make test",
                    "success": "false",
                },
            ),
            S(
                sid(4),
                name="claude_code.tool.execution",
                parent=sid(3),
                attrs={"tool_use_id": "toolu_1"},
            ),
        ],
        resource,
    )
    assert result.ignored == 2  # the interaction root and the execution child
    llm, tool = result.spans
    assert llm.kind is SpanKind.LLM and llm.model == "synthetic-model"
    # Anthropic usage excludes cache reads and writes; stored as semconv (includes them)
    assert (llm.input_tokens, llm.output_tokens, llm.cached_tokens) == (4112, 30, 4000)
    assert llm.session_id == "cc-1" and llm.outcome == "ok"
    assert llm.attributes["cache_creation_tokens"] == 100
    assert tool.kind is SpanKind.TOOL and tool.tool == "Bash" and tool.outcome == "error"
    assert tool.args_hash == args_hash({"full_command": "make test"})
    assert tool.attributes["gen_ai.tool.call.id"] == "toolu_1"
    assert "make test" not in repr(tool) and "secret" not in repr(result)
