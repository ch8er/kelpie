"""Spans from platform hooks, for platforms that emit no OpenTelemetry, and the rule that
keeps hook spans and OTLP spans from counting the same tool call twice.

An adapter turns its hook records into :class:`HookEvent` (it knows its own ids) and calls
:func:`spans_from_hook_events`, which builds GenAI-convention spans:

- a TOOL span per tool call (``execute_tool <tool>``), ending when the hook fired and
  starting ``duration`` earlier, with the same ``args_hash`` OTLP ingest computes
  (:mod:`kelpie.traces.args`; a hook that sees raw arguments passes them as ``args`` and
  they are hashed and discarded here);
- a SESSION span per session (``invoke_agent``) from its first to its last event; it is
  closed only when the session's last event is a ``session_end``;
- a TASK span per task (``invoke_workflow``) around its sessions, never closed: the board,
  not the hooks, knows when a task is done.

Tool spans are children of their session span, session spans of their task span; spans of
one task share a trace. Every hook span carries ``kelpie.source = "hooks"``.

**No double counting.** A platform may report the same tool call through hooks and OTLP.
:func:`merge_hook_spans` keeps one span per call: the OTLP span (it has timing and tokens),
completed with what only the hook knows (``args_hash`` when OTLP has none, ``command`` and
other hook attributes), so rules that read those still work. Pairs are matched one to one,
within a session and tool: first by tool call id, then, when one side has no call id, by
arguments hash (or no hash on the OTLP side) and a start or end within
:data:`MATCH_WINDOW`, nearest first. Two different call ids are never merged.
"""

from __future__ import annotations

import bisect
import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.model import Span, SpanKind
from kelpie.traces.args import args_hash as hash_args
from kelpie.traces.semconv import TOOL_CALL_ID

SOURCE = "kelpie.source"
HOOKS = "hooks"
MATCH_WINDOW = timedelta(seconds=2)
TOOL, SESSION_START, SESSION_END = "tool", "session_start", "session_end"
_MISSING = object()


@dataclass(frozen=True, slots=True)
class HookEvent:
    """One hook firing, normalized by the adapter.

    ``key`` identifies the event stably across reads (e.g. a digest of the spool line), so
    the same call gets the same span id on every tick.
    """

    kind: str  # TOOL | SESSION_START | SESSION_END
    at: datetime  # when the hook fired; for a tool call, when it finished
    key: str
    session_id: str | None = None
    task_id: str | None = None
    tool: str | None = None
    args_hash: str | None = None
    args: Any = _MISSING  # raw arguments, if the hook has them: hashed, never kept
    duration: timedelta | None = None
    outcome: str | None = None  # "ok" | "error"
    tool_call_id: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def spans_from_hook_events(events: Iterable[HookEvent], prefix: str) -> tuple[Span, ...]:
    """Task, session and tool spans from hook events; ``prefix`` namespaces the span ids
    (e.g. the adapter name). Events may come in any order."""
    ordered = sorted(events, key=lambda e: e.at)
    sessions: dict[str, list[HookEvent]] = defaultdict(list)
    session_task: dict[str, str] = {}
    tasks: dict[str, list[datetime]] = defaultdict(list)
    for e in ordered:
        if e.session_id:
            sessions[e.session_id].append(e)
            if e.task_id:
                session_task.setdefault(e.session_id, e.task_id)
        if e.task_id:
            tasks[e.task_id].append(_start(e))

    def trace(task: str | None, session: str | None) -> str:
        if task:
            return f"{prefix}-task-{task}"
        return session or f"{prefix}-task-none"

    def task_span_id(task: str) -> str:
        return f"{prefix}-task-{_digest(task)}"

    def session_span_id(session: str) -> str:
        return f"{prefix}-session-{_digest(session)}"

    out: list[Span] = []
    for task, starts in tasks.items():
        out.append(
            Span(
                trace_id=trace(task, None),
                span_id=task_span_id(task),
                name="invoke_workflow",
                kind=SpanKind.TASK,
                start=min(starts),
                task_id=task,
                attributes={"gen_ai.operation.name": "invoke_workflow", SOURCE: HOOKS},
            )
        )
    for session, evs in sessions.items():
        task = session_task.get(session)
        last = evs[-1]
        out.append(
            Span(
                trace_id=trace(task, session),
                span_id=session_span_id(session),
                parent_id=task_span_id(task) if task else None,
                name="invoke_agent",
                kind=SpanKind.SESSION,
                start=min(_start(e) for e in evs),
                end=last.at if last.kind == SESSION_END else None,
                task_id=task,
                session_id=session,
                attributes={"gen_ai.operation.name": "invoke_agent", SOURCE: HOOKS},
            )
        )
    for e in ordered:
        if e.kind != TOOL:
            continue
        task = e.task_id or (session_task.get(e.session_id) if e.session_id else None)
        attributes: dict[str, Any] = {
            **e.attributes,
            "gen_ai.operation.name": "execute_tool",
            SOURCE: HOOKS,
        }
        if e.tool_call_id:
            attributes[TOOL_CALL_ID] = e.tool_call_id
        digest = e.args_hash
        if e.args is not _MISSING:
            digest = hash_args(e.args)
        if e.session_id:
            parent: str | None = session_span_id(e.session_id)
        else:
            parent = task_span_id(task) if task else None
        out.append(
            Span(
                trace_id=trace(task, e.session_id),
                span_id=f"{prefix}-tool-{e.key}",
                parent_id=parent,
                name=f"execute_tool {e.tool}" if e.tool else "execute_tool",
                kind=SpanKind.TOOL,
                start=_start(e),
                end=e.at,
                task_id=task,
                session_id=e.session_id,
                tool=e.tool,
                args_hash=digest,
                outcome=e.outcome,
                attributes=attributes,
            )
        )
    return tuple(out)


def _start(e: HookEvent) -> datetime:
    if e.duration is None or e.duration < timedelta(0):
        return e.at
    return e.at - e.duration


def is_hook_span(span: Span) -> bool:
    return span.attributes.get(SOURCE) == HOOKS


@dataclass(frozen=True, slots=True)
class Merged:
    spans: tuple[Span, ...]
    merged: int  # hook spans folded into an OTLP span


def merge_hook_spans(spans: Sequence[Span]) -> Merged:
    """Fold hook tool spans into the OTLP spans that report the same call."""
    hooks = [i for i, s in enumerate(spans) if _hook_tool(s)]
    others = [i for i, s in enumerate(spans) if _otlp_tool(s)]
    if not hooks or not others:
        return Merged(tuple(spans), 0)
    # Bucket candidates so matching stays linear in practice, not hooks x OTLP spans.
    by_call: dict[tuple[str | None, str], list[int]] = defaultdict(list)
    by_tool: dict[tuple[str | None, str | None], _Times] = defaultdict(_Times)
    for o in others:
        if call := _call_id(spans[o]):
            by_call[(spans[o].session_id, call)].append(o)
        by_tool[(spans[o].session_id, spans[o].tool)].add(o, spans[o])
    for times in by_tool.values():
        times.sort()
    pairs: list[tuple[float, int, int]] = []
    for h in hooks:
        hook = spans[h]
        call = _call_id(hook)
        candidates: set[int] = set()
        if (times := by_tool.get((hook.session_id, hook.tool))) is not None:
            candidates.update(times.near(hook))
        if call:
            candidates.update(by_call.get((hook.session_id, call), ()))
        for o in candidates:
            distance = _distance(hook, spans[o])
            if distance is not None:
                pairs.append((distance, h, o))
    pairs.sort()
    taken: dict[int, int] = {}  # otlp index -> hook index
    used: set[int] = set()
    for _, h, o in pairs:
        if h in used or o in taken:
            continue
        used.add(h)
        taken[o] = h
    out: list[Span] = []
    for i, span in enumerate(spans):
        if i in used:
            continue
        out.append(_fold(span, spans[taken[i]]) if i in taken else span)
    return Merged(tuple(out), len(used))


@dataclass(slots=True)
class _Times:
    """OTLP spans of one session and tool, searchable by start and by end."""

    starts: list[tuple[datetime, int]] = field(default_factory=list)
    ends: list[tuple[datetime, int]] = field(default_factory=list)

    def add(self, index: int, span: Span) -> None:
        self.starts.append((span.start, index))
        if span.end is not None:
            self.ends.append((span.end, index))

    def sort(self) -> None:
        self.starts.sort()
        self.ends.sort()

    def near(self, hook: Span) -> set[int]:
        found = _within(self.starts, hook.start)
        if hook.end is not None:
            found |= _within(self.ends, hook.end)
        return found


def _within(times: list[tuple[datetime, int]], at: datetime) -> set[int]:
    lo = bisect.bisect_left(times, (at - MATCH_WINDOW, -1))
    hi = bisect.bisect_right(times, (at + MATCH_WINDOW, 2**62))
    return {index for _, index in times[lo:hi]}


def _hook_tool(s: Span) -> bool:
    return s.kind is SpanKind.TOOL and is_hook_span(s) and bool(s.session_id)


def _otlp_tool(s: Span) -> bool:
    return s.kind is SpanKind.TOOL and not is_hook_span(s) and bool(s.session_id)


def _distance(hook: Span, other: Span) -> float | None:
    """How well two tool spans match: -1 for the same call id, else seconds apart; None
    when they cannot be the same call."""
    if hook.session_id != other.session_id:
        return None
    a, b = _call_id(hook), _call_id(other)
    if a and b:
        return -1.0 if a == b else None
    if hook.tool != other.tool or not hook.tool:
        return None
    if other.args_hash and hook.args_hash != other.args_hash:
        return None
    gaps = [abs(hook.start - other.start)]
    if hook.end is not None and other.end is not None:
        gaps.append(abs(hook.end - other.end))  # a hook without duration has start == end
    gap = min(gaps)
    return gap.total_seconds() if gap <= MATCH_WINDOW else None


def _fold(otlp: Span, hook: Span) -> Span:
    extra = {k: v for k, v in hook.attributes.items() if k not in otlp.attributes}
    extra.pop(SOURCE, None)
    extra.pop("gen_ai.operation.name", None)
    return replace(
        otlp,
        args_hash=otlp.args_hash or hook.args_hash,
        tool=otlp.tool or hook.tool,
        task_id=otlp.task_id or hook.task_id,
        attributes={**otlp.attributes, **extra},
    )


def _call_id(span: Span) -> str | None:
    call = span.attributes.get(TOOL_CALL_ID)
    return str(call) if call not in (None, "") else None
