"""Map decoded OTLP spans onto :class:`kelpie.core.model.Span`.

Follows the OpenTelemetry GenAI semantic conventions
(https://github.com/open-telemetry/semantic-conventions-genai, ``docs/gen-ai``), with
aliases for older attribute names and for Claude Code, which exports its own span names.

**Kind.** From ``gen_ai.operation.name``:

=====================================================  ========
``chat``, ``text_completion``, ``generate_content``,   LLM
``embeddings``
``execute_tool``                                       TOOL
``invoke_agent``                                       SESSION
``invoke_workflow``                                    TASK
=====================================================  ========

Without a known operation: an explicit ``kelpie.span.kind`` attribute (``task``,
``session``, ``llm``, ``tool``) wins; then Claude Code's ``claude_code.llm_request`` (LLM)
and ``claude_code.tool`` (TOOL); then a span with ``gen_ai.tool.name`` is a TOOL, one with a
model or token usage an LLM call, one with ``gen_ai.agent.name``/``id`` a SESSION. Anything
else (HTTP spans, ``claude_code.interaction``, memory and retrieval operations without
token usage) is not a GenAI step Kelpie reasons about: it is used for task and session
linkage within the request and then dropped (counted as ``ignored``).

**Fields.** ``model``: ``gen_ai.response.model``, else ``gen_ai.request.model``. Tokens:
``gen_ai.usage.input_tokens`` / ``output_tokens`` (deprecated ``prompt_tokens`` /
``completion_tokens`` accepted), cached = ``gen_ai.usage.cache_read.input_tokens``; cache
writes stay in ``attributes`` as ``gen_ai.usage.cache_write.input_tokens``. Tokens are
stored as reported: the conventions count cached tokens inside ``input_tokens``, while
Claude Code reports Anthropic usage, where ``input_tokens`` excludes cache reads.
``tool``: ``gen_ai.tool.name``. ``outcome``: ``error`` when the span status is ERROR, when
``error.type`` is set, or when Claude Code's ``success`` is false; else ``ok``.

**Linkage.** ``task_id`` comes from the configured task attribute (default
``kelpie.task_id``), ``session_id`` from the configured session attribute (default
``kelpie.session_id``), else ``gen_ai.conversation.id``, else ``session.id`` (Claude Code).
Span attributes are looked up first, then resource attributes, so an agent host can set
them once with ``OTEL_RESOURCE_ATTRIBUTES``. A span without a link inherits its nearest
ancestor's within the same request.

**Privacy.** Kelpie never stores message content. Dropped before anything is kept:
input/output messages, system instructions, tool definitions, tool call arguments and
results, retrieval and memory text, prompts and completions in any older or
vendor-specific form (``gen_ai.prompt.*``, ``gen_ai.completion.*``, ``llm.input_messages``,
``input.value``, ...), Claude Code's ``user_prompt``, ``full_command`` and ``file_path``,
error and exception messages, and user identity (``user.email``, ``user.account_*``,
``enduser.*``). Tool call arguments are reduced to ``args_hash`` (see
:mod:`kelpie.traces.args`) before they are discarded. Everything else that is not mapped to
a field is kept in ``attributes``: at most 64 keys, strings cut to 256 characters, lists to
16 scalar items, nested maps dropped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.model import Span, SpanKind
from kelpie.traces.args import args_hash, args_hash_of_attribute
from kelpie.traces.otlp import RawSpan

DEFAULT_TASK_ATTRIBUTE = "kelpie.task_id"
DEFAULT_SESSION_ATTRIBUTE = "kelpie.session_id"
SESSION_FALLBACKS = ("gen_ai.conversation.id", "session.id")
TOOL_CALL_ID = "gen_ai.tool.call.id"
KIND_ATTRIBUTE = "kelpie.span.kind"

OPERATIONS: Mapping[str, SpanKind] = {
    "chat": SpanKind.LLM,
    "text_completion": SpanKind.LLM,
    "generate_content": SpanKind.LLM,
    "embeddings": SpanKind.LLM,
    "execute_tool": SpanKind.TOOL,
    "invoke_agent": SpanKind.SESSION,
    "invoke_workflow": SpanKind.TASK,
}
CLAUDE_CODE_SPANS: Mapping[str, SpanKind] = {
    "claude_code.llm_request": SpanKind.LLM,
    "claude_code.tool": SpanKind.TOOL,
}

# Attribute keys in preference order. Claude Code aliases apply to ``claude_code.*`` only.
MODEL = ("gen_ai.response.model", "gen_ai.request.model")
INPUT_TOKENS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
OUTPUT_TOKENS = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
CACHED_TOKENS = ("gen_ai.usage.cache_read.input_tokens",)
TOOL_NAME = ("gen_ai.tool.name",)
ARGUMENTS = "gen_ai.tool.call.arguments"
CC_MODEL = ("model",)
CC_INPUT = ("input_tokens",)
CC_OUTPUT = ("output_tokens",)
CC_CACHED = ("cache_read_tokens",)
CC_TOOL = ("tool_name",)
CC_TOOL_CALL_ID = "tool_use_id"
CC_ARGS = ("full_command", "file_path")  # only present with OTEL_LOG_TOOL_DETAILS=1

MAX_ATTRIBUTES = 64
MAX_VALUE_CHARS = 256
MAX_LIST_ITEMS = 16
MAX_NAME_CHARS = 256
KEPT_RESOURCE = ("service.name",)

_PRIVATE_KEYS = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.definitions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.prompt",
        "gen_ai.completion",
        "gen_ai.prompt.variable",
        "gen_ai.retrieval.documents",
        "gen_ai.retrieval.query.text",
        "gen_ai.memory.query.text",
        "gen_ai.memory.records",
        "gen_ai.evaluation.explanation",
        "input.value",
        "output.value",
        "user_prompt",
        "full_command",
        "file_path",
        "tool_input",
        "tool_output",
        "error",
        "error.message",
        "exception.message",
        "exception.stacktrace",
        "user.email",
        "user.account_uuid",
        "user.account_id",
    }
)
# Prefixes of content-bearing keys: indexed prompts/completions (OpenLLMetry), message
# lists (OpenInference), tool parameters, end-user identity.
_PRIVATE_PREFIXES = (
    "gen_ai.prompt.",
    "gen_ai.completion.",
    "llm.input_messages",
    "llm.output_messages",
    "llm.prompts",
    "llm.prompt_template",
    "tool.parameters",
    "tool_call.",
    "enduser.",
)
# Any dotted segment with one of these names marks content, whatever the vendor prefix.
_PRIVATE_SEGMENTS = frozenset(
    {"messages", "content", "prompt", "prompts", "completion", "completions", "arguments"}
)
_SEGMENT = re.compile(r"[._]")
_KINDS = frozenset(k.value for k in SpanKind)


def is_private(key: str) -> bool:
    """True for attributes that may carry message content or personal data."""
    if key in _PRIVATE_KEYS or key.startswith(_PRIVATE_PREFIXES):
        return True
    return any(part in _PRIVATE_SEGMENTS for part in _SEGMENT.split(key.lower()))


@dataclass(frozen=True, slots=True)
class MappingOptions:
    task_attribute: str = DEFAULT_TASK_ATTRIBUTE
    session_attribute: str = DEFAULT_SESSION_ATTRIBUTE


@dataclass(slots=True)
class Mapped:
    spans: list[Span] = field(default_factory=list)
    ignored: int = 0  # decoded but not a GenAI step


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _time(ns: int) -> datetime:
    return _EPOCH + timedelta(microseconds=ns // 1000)


def map_spans(raw: Sequence[RawSpan], options: MappingOptions | None = None) -> Mapped:
    """Turn decoded spans from one request into Kelpie spans (never raises on bad values)."""
    opts = options or MappingOptions()
    by_id = {(r.trace_id, r.span_id): r for r in raw}
    links: dict[tuple[str, str], tuple[str | None, str | None]] = {}

    def link(r: RawSpan) -> tuple[str | None, str | None]:
        # Walk up to the nearest ancestor that has each id; iterative, cycle-safe.
        chain: list[RawSpan] = []
        seen: set[tuple[str, str]] = set()
        current: RawSpan | None = r
        task: str | None = None
        session: str | None = None
        while current is not None:
            key = (current.trace_id, current.span_id)
            if key in links:
                task, session = links[key]
                break
            if key in seen:
                break
            seen.add(key)
            chain.append(current)
            own_task, own_session = _own_link(current, opts)
            if own_task and own_session:
                task, session = own_task, own_session
                break
            parent = by_id.get((current.trace_id, current.parent_id or ""))
            current = parent
        # Resolve from the top of the chain down so each span gets its nearest ancestor.
        for span in reversed(chain):
            own_task, own_session = _own_link(span, opts)
            task = own_task or task
            session = own_session or session
            links[(span.trace_id, span.span_id)] = (task, session)
        return links[(r.trace_id, r.span_id)]

    out = Mapped()
    for r in raw:
        task_id, session_id = link(r)
        span = _span(r, task_id, session_id)
        if span is None:
            out.ignored += 1
        else:
            out.spans.append(span)
    return out


def _lookup(r: RawSpan, key: str) -> Any:
    value = r.attributes.get(key)
    if value in (None, ""):
        value = r.resource.get(key)
    return None if value in (None, "") else value


def _own_link(r: RawSpan, opts: MappingOptions) -> tuple[str | None, str | None]:
    task = _lookup(r, opts.task_attribute)
    session = None
    for key in (opts.session_attribute, *SESSION_FALLBACKS):
        if (session := _lookup(r, key)) is not None:
            break
    return (None if task is None else str(task)), (None if session is None else str(session))


def _first(attrs: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def _tokens(attrs: Mapping[str, Any], keys: Sequence[str]) -> int:
    value = _first(attrs, keys)
    try:
        n = int(value) if value is not None and not isinstance(value, bool | list | dict) else 0
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(n, 2**62))


def _kind(r: RawSpan, claude_code: bool) -> SpanKind | None:
    attrs = r.attributes
    op = attrs.get("gen_ai.operation.name")
    if isinstance(op, str) and op in OPERATIONS:
        return OPERATIONS[op]
    explicit = attrs.get(KIND_ATTRIBUTE)
    if isinstance(explicit, str) and explicit.lower() in _KINDS:
        return SpanKind(explicit.lower())
    if claude_code:
        return CLAUDE_CODE_SPANS.get(r.name)
    if _first(attrs, TOOL_NAME) is not None:
        return SpanKind.TOOL
    usage = (*INPUT_TOKENS, *OUTPUT_TOKENS, *CACHED_TOKENS)
    if _first(attrs, MODEL) is not None or _first(attrs, usage) is not None:
        return SpanKind.LLM
    if _first(attrs, ("gen_ai.agent.name", "gen_ai.agent.id")) is not None:
        return SpanKind.SESSION
    return None


def _span(r: RawSpan, task_id: str | None, session_id: str | None) -> Span | None:
    claude_code = r.name.startswith("claude_code.")
    kind = _kind(r, claude_code)
    if kind is None:
        return None
    attrs = r.attributes
    model_keys = (*MODEL, *CC_MODEL) if claude_code else MODEL
    tool_keys = (*TOOL_NAME, *CC_TOOL) if claude_code else TOOL_NAME
    model = _first(attrs, model_keys)
    tool = _first(attrs, tool_keys)
    digest: str | None = None
    if kind is SpanKind.TOOL:
        if ARGUMENTS in attrs:
            digest = args_hash_of_attribute(attrs[ARGUMENTS])
        elif claude_code and (details := {k: attrs[k] for k in CC_ARGS if k in attrs}):
            digest = args_hash(details)
    failed = (
        r.error
        or _first(attrs, ("error.type",)) is not None
        or (claude_code and str(attrs.get("success", "")).lower() == "false")
    )
    consumed = {*model_keys, *tool_keys, ARGUMENTS, KIND_ATTRIBUTE}
    consumed.update(INPUT_TOKENS, OUTPUT_TOKENS, CACHED_TOKENS)
    if claude_code:
        consumed.update((*CC_INPUT, *CC_OUTPUT, *CC_CACHED))
    attributes = _kept(attrs, consumed)
    if claude_code and (call_id := attrs.get(CC_TOOL_CALL_ID)) not in (None, ""):
        attributes.setdefault(TOOL_CALL_ID, str(call_id)[:MAX_VALUE_CHARS])
    for key in KEPT_RESOURCE:
        if key in r.resource and len(attributes) < MAX_ATTRIBUTES:
            attributes.setdefault(key, _bounded(r.resource[key]))
    start = _time(r.start_ns)
    end = _time(r.end_ns) if r.end_ns else None
    return Span(
        trace_id=r.trace_id,
        span_id=r.span_id,
        parent_id=r.parent_id,
        name=r.name[:MAX_NAME_CHARS] or str(kind),
        kind=kind,
        start=start,
        end=None if end is None else max(end, start),
        task_id=task_id,
        session_id=session_id,
        model=None if model is None else str(model)[:MAX_VALUE_CHARS],
        tool=None if tool is None else str(tool)[:MAX_VALUE_CHARS],
        args_hash=digest,
        input_tokens=_tokens(attrs, (*INPUT_TOKENS, *CC_INPUT) if claude_code else INPUT_TOKENS),
        output_tokens=_tokens(
            attrs, (*OUTPUT_TOKENS, *CC_OUTPUT) if claude_code else OUTPUT_TOKENS
        ),
        cached_tokens=_tokens(
            attrs, (*CACHED_TOKENS, *CC_CACHED) if claude_code else CACHED_TOKENS
        ),
        outcome="error" if failed else "ok",
        attributes=attributes,
    )


def _kept(attrs: Mapping[str, Any], consumed: set[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(attrs):
        if len(out) >= MAX_ATTRIBUTES:
            break
        if key in consumed or is_private(key):
            continue
        value = _bounded(attrs[key])
        if value is not None:
            out[key[:MAX_VALUE_CHARS]] = value
    return out


def _bounded(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        items = [_bounded(v) for v in value[:MAX_LIST_ITEMS] if not isinstance(v, list | dict)]
        return [v for v in items if v is not None]
    return None  # maps are structured payloads, often content: dropped
