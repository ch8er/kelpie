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
``completion_tokens`` accepted), cached = ``gen_ai.usage.cache_read.input_tokens`` (or
OpenLLMetry's ``gen_ai.usage.cache_read_input_tokens``); cache writes stay in
``attributes`` as ``gen_ai.usage.cache_write.input_tokens``. Input tokens
follow the conventions and include cached tokens. Claude Code reports Anthropic usage,
where ``input_tokens`` excludes cache reads and writes, so for ``claude_code.*`` spans
Kelpie stores ``input_tokens + cache_read_tokens + cache_creation_tokens``.
``tool``: ``gen_ai.tool.name``. ``outcome``: ``error`` when the span status is ERROR, when
``error.type`` is set, or when Claude Code's ``success`` is false; else ``ok``.

**Linkage.** ``task_id`` comes from the configured task attribute (default
``kelpie.task_id``), ``session_id`` from the configured session attribute (default
``kelpie.session_id``), else ``gen_ai.conversation.id``, else ``session.id`` (Claude Code).
Span attributes are looked up first, then resource attributes, so an agent host can set
them once with ``OTEL_RESOURCE_ATTRIBUTES``. A span without a link inherits its nearest
ancestor's within the same request; after storing, ingest also fills links from stored
parents and into stored children (:meth:`kelpie.core.spans.SpanStore.inherit_links`),
because exporters send a trace in several requests. Spans that are dropped (not GenAI
steps) are not stored, so that cross-request inheritance does not pass through them.

**Privacy.** Kelpie keeps only attributes on an allowlist (:func:`is_kept`): GenAI request
parameters, response ids and finish reasons, usage counters, tool name, call id and type,
agent and conversation ids, ``error.type``, ``server.address``/``port``,
``service.name``, ``session.id``, ``kelpie.*`` and, on Claude Code spans, its numeric and
status fields. Everything else is dropped, whatever the vendor calls it: message content,
prompts, completions, tool arguments and results, reasoning text, error messages, user and
organization identity. Tool call arguments (``gen_ai.tool.call.arguments``, Claude Code's
``tool_parameters`` or ``full_command``/``file_path``) are reduced to ``args_hash`` (see
:mod:`kelpie.traces.args`) before they are discarded. Kept values are scalars (strings cut
to 256 characters) or, for finish reasons, short lists; at most 64 keys.

**Tool output and commands** (huge tool outputs, :mod:`kelpie.traces.outputs`). A tool span
records how big its result was, never the result: ``kelpie.tool.output_tokens`` from
Claude Code's ``result_tokens``, and ``kelpie.tool.output_chars``, the length of
``gen_ai.tool.call.result`` (or OpenInference ``output.value``) before it is dropped. A
shell command in the arguments (``command``/``cmd`` in ``gen_ai.tool.call.arguments`` or
``tool_parameters``, or Claude Code's ``full_command``) is reduced to its grouping prefix,
``kelpie.tool.command_prefix`` (``make test``, ``xcodebuild``; redacted first, see
:mod:`kelpie.traces.command`; off with ``[traces] commands = false``), and
``kelpie.tool.filter`` names an output filter the full command already pipes through
(``rtk``, ``xcbeautify``, ``tail``, ...); the command itself is dropped. Kelpie computes
these four itself: values an exporter sends under the same names are dropped.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.model import Span, SpanKind
from kelpie.traces.args import args_hash, args_hash_of_attribute
from kelpie.traces.command import command_of, command_prefix, detect_filter, redact_command
from kelpie.traces.otlp import RawSpan

log = logging.getLogger(__name__)

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
CACHED_TOKENS = ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_read_input_tokens")
TOOL_NAME = ("gen_ai.tool.name",)
ARGUMENTS = "gen_ai.tool.call.arguments"
CC_MODEL = ("model",)
CC_INPUT = ("input_tokens",)
CC_OUTPUT = ("output_tokens",)
CC_CACHED = ("cache_read_tokens",)
CC_CACHE_WRITE = ("cache_creation_tokens",)
CC_TOOL = ("tool_name",)
CC_TOOL_CALL_ID = "tool_use_id"
CC_PARAMETERS = "tool_parameters"  # JSON of the tool input, with OTEL_LOG_TOOL_DETAILS=1
CC_ARGS = ("full_command", "file_path")  # only present with OTEL_LOG_TOOL_DETAILS=1

CC_COMMAND = "full_command"
CC_RESULT_TOKENS = "result_tokens"
RESULTS = ("gen_ai.tool.call.result", "output.value")  # content: measured, never kept
OUTPUT_TOKENS_ATTRIBUTE = "kelpie.tool.output_tokens"
OUTPUT_CHARS_ATTRIBUTE = "kelpie.tool.output_chars"
FILTER_ATTRIBUTE = "kelpie.tool.filter"
COMMAND_ATTRIBUTE = "kelpie.tool.command_prefix"
TOOL_DETAILS = frozenset(
    {OUTPUT_TOKENS_ATTRIBUTE, OUTPUT_CHARS_ATTRIBUTE, FILTER_ATTRIBUTE, COMMAND_ATTRIBUTE}
)

MAX_ATTRIBUTES = 64
MAX_VALUE_CHARS = 256
MAX_LIST_ITEMS = 16
MAX_NAME_CHARS = 256
KEPT_RESOURCE = ("service.name",)

# The allowlist. Anything not named here is dropped, so a new vendor's content attribute
# can never slip through.
KEPT_KEYS = frozenset(
    {
        "gen_ai.system",
        "gen_ai.provider.name",
        "gen_ai.operation.name",
        "gen_ai.output.type",
        "gen_ai.request.model",
        "gen_ai.request.max_tokens",
        "gen_ai.request.temperature",
        "gen_ai.request.top_p",
        "gen_ai.request.top_k",
        "gen_ai.request.frequency_penalty",
        "gen_ai.request.presence_penalty",
        "gen_ai.request.seed",
        "gen_ai.request.choice.count",
        "gen_ai.request.stream",
        "gen_ai.request.reasoning.level",
        "gen_ai.response.id",
        "gen_ai.response.model",
        "gen_ai.response.finish_reasons",
        "gen_ai.response.time_to_first_chunk",
        "gen_ai.tool.name",
        "gen_ai.tool.call.id",
        "gen_ai.tool.type",
        "gen_ai.agent.id",
        "gen_ai.agent.name",
        "gen_ai.conversation.id",
        "error.type",
        "server.address",
        "server.port",
        "service.name",
        "session.id",
    }
)
KEPT_LISTS = frozenset({"gen_ai.response.finish_reasons"})
NUMERIC_PREFIXES = ("gen_ai.usage.",)  # any usage counter, numbers only
KEPT_PREFIXES = ("kelpie.",)  # set by the operator on purpose
# Claude Code span attributes (code.claude.com/docs/en/monitoring-usage) that carry no
# content: timings, counters, status. Only on ``claude_code.*`` spans.
CLAUDE_CODE_KEYS = frozenset(
    {
        "success",
        "duration_ms",
        "ttft_ms",
        "attempt",
        "status_code",
        "stop_reason",
        "speed",
        "effort",
        "result_tokens",
        "cache_creation_tokens",
        "request_id",
        "span.type",
        "query_source",
        "decision",
        "source",
    }
)
_KINDS = frozenset(k.value for k in SpanKind)


# Set by Kelpie itself (hook spans carry ``kelpie.source = "hooks"``); never taken from OTLP.
RESERVED_KEYS = frozenset({"kelpie.source", *TOOL_DETAILS})


def is_kept(key: str, claude_code: bool = False) -> bool:
    """True for attributes on the allowlist (values are checked separately)."""
    if key in RESERVED_KEYS:
        return False
    return (
        key in KEPT_KEYS
        or key.startswith(NUMERIC_PREFIXES)
        or key.startswith(KEPT_PREFIXES)
        or (claude_code and key in CLAUDE_CODE_KEYS)
    )


@dataclass(frozen=True, slots=True)
class MappingOptions:
    task_attribute: str = DEFAULT_TASK_ATTRIBUTE
    session_attribute: str = DEFAULT_SESSION_ATTRIBUTE
    commands: bool = True  # keep a redacted prefix of shell commands on tool spans


@dataclass(slots=True)
class Mapped:
    spans: list[Span] = field(default_factory=list)
    ignored: int = 0  # decoded but not a GenAI step
    rejected: Counter[str] = field(default_factory=Counter)  # reason -> spans


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _time(ns: int) -> datetime:
    return _EPOCH + timedelta(microseconds=ns // 1000)


def map_spans(raw: Sequence[RawSpan], options: MappingOptions | None = None) -> Mapped:
    """Turn decoded spans from one request into Kelpie spans. Never raises: a span that
    cannot be mapped is counted in ``rejected``."""
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
        try:
            task_id, session_id = link(r)
            span = _span(r, task_id, session_id)
        except OverflowError:
            out.rejected["invalid_time"] += 1
            continue
        except Exception:  # a bug or an input we did not foresee: never fail the request
            log.debug("cannot map span %s/%s", r.trace_id, r.span_id, exc_info=True)
            out.rejected["malformed"] += 1
            continue
        if span is None:
            out.ignored += 1
        else:
            if not opts.commands and COMMAND_ATTRIBUTE in span.attributes:
                kept = {k: v for k, v in span.attributes.items() if k != COMMAND_ATTRIBUTE}
                span = replace(span, attributes=kept)
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
    return (None if task is None else _id(task)), (None if session is None else _id(session))


def _id(value: Any) -> str:
    return str(value)[:MAX_VALUE_CHARS]


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


def _args_hash(attrs: Mapping[str, Any], claude_code: bool) -> str | None:
    if ARGUMENTS in attrs:
        return args_hash_of_attribute(attrs[ARGUMENTS])
    if not claude_code:
        return None
    if CC_PARAMETERS in attrs:
        return args_hash_of_attribute(attrs[CC_PARAMETERS])
    details = {k: attrs[k] for k in CC_ARGS if k in attrs}
    return args_hash(details) if details else None


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
    digest = _args_hash(attrs, claude_code) if kind is SpanKind.TOOL else None
    failed = (
        r.error
        or _first(attrs, ("error.type",)) is not None
        or (claude_code and str(attrs.get("success", "")).lower() == "false")
    )
    if claude_code:
        cached = _tokens(attrs, (*CACHED_TOKENS, *CC_CACHED))
        written = _tokens(attrs, CC_CACHE_WRITE)
        reported = _tokens(attrs, (*INPUT_TOKENS, *CC_INPUT))
        # semconv input tokens include cached ones; Anthropic usage does not
        input_tokens = reported if _first(attrs, INPUT_TOKENS) else reported + cached + written
    else:
        cached = _tokens(attrs, CACHED_TOKENS)
        input_tokens = _tokens(attrs, INPUT_TOKENS)
    consumed = {*model_keys, *tool_keys, KIND_ATTRIBUTE, *INPUT_TOKENS, *OUTPUT_TOKENS}
    consumed.update(CACHED_TOKENS)
    attributes = _kept(attrs, consumed, claude_code)
    if kind is SpanKind.TOOL:
        attributes.update(_tool_details(attrs, claude_code))
    if claude_code and (call_id := attrs.get(CC_TOOL_CALL_ID)) not in (None, ""):
        attributes.setdefault(TOOL_CALL_ID, str(call_id)[:MAX_VALUE_CHARS])
    for key in KEPT_RESOURCE:
        value = _bounded(r.resource.get(key))
        if value is not None and len(attributes) < MAX_ATTRIBUTES:
            attributes.setdefault(key, value)
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
        input_tokens=input_tokens,
        output_tokens=_tokens(
            attrs, (*OUTPUT_TOKENS, *CC_OUTPUT) if claude_code else OUTPUT_TOKENS
        ),
        cached_tokens=cached,
        outcome="error" if failed else "ok",
        attributes=attributes,
    )


def _tool_details(attrs: Mapping[str, Any], claude_code: bool) -> dict[str, Any]:
    """Output size, redacted command and filter of a tool call (see the module docs)."""
    out: dict[str, Any] = {}
    if claude_code and (tokens := _count(attrs.get(CC_RESULT_TOKENS))) is not None:
        out[OUTPUT_TOKENS_ATTRIBUTE] = tokens
    result = _first(attrs, RESULTS)
    if result is not None:
        out[OUTPUT_CHARS_ATTRIBUTE] = _size(result)
    raw = _raw_command(attrs, claude_code)
    if raw:
        if (prefix := command_prefix(redact_command(raw))) is not None:
            out[COMMAND_ATTRIBUTE] = prefix
        if (used := detect_filter(raw)) is not None:
            out[FILTER_ATTRIBUTE] = used
    return out


def _raw_command(attrs: Mapping[str, Any], claude_code: bool) -> str | None:
    keys = (ARGUMENTS, CC_PARAMETERS) if claude_code else (ARGUMENTS,)
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                value = json.loads(value)
        if (found := command_of(value)) is not None:
            return found
    if claude_code and isinstance(full := attrs.get(CC_COMMAND), str) and full:
        return full
    return None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        n = int(value)
    except (ValueError, OverflowError):
        return None
    return max(0, min(n, 2**62))


def _size(value: Any) -> int:
    """Characters of a tool result (structured results as compact JSON)."""
    if isinstance(value, str):
        return len(value)
    try:
        return len(json.dumps(value, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _kept(attrs: Mapping[str, Any], consumed: set[str], claude_code: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(attrs):
        if len(out) >= MAX_ATTRIBUTES:
            break
        if key in consumed or not is_kept(key, claude_code):
            continue
        value = attrs[key]
        if key.startswith(NUMERIC_PREFIXES):
            value = (
                value if isinstance(value, int | float) and not isinstance(value, bool) else None
            )
        elif key in KEPT_LISTS and isinstance(value, list):
            value = [v for v in (_bounded(x) for x in value) if v is not None][:MAX_LIST_ITEMS]
        else:
            value = _bounded(value)
        if value is not None:
            out[key[:MAX_VALUE_CHARS]] = value
    return out


def _bounded(value: Any) -> Any:
    """A scalar, cut to size; anything structured is dropped."""
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if isinstance(value, bool | int | float):
        return value
    return None
