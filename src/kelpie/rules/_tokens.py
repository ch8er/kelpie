"""Token accounting over spans (used by rule 11, no_progress)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from kelpie.core.model import Span, SpanKind

SpanIndex = Mapping[tuple[str, str], Span]


def span_index(spans: Sequence[Span]) -> SpanIndex:
    """Spans by (trace id, span id): build it once per snapshot and share it."""
    return {(s.trace_id, s.span_id): s for s in spans}


def outermost_llm_spans(spans: Sequence[Span], index: SpanIndex | None = None) -> list[Span]:
    """LLM spans of ``spans`` that have no LLM ancestor in the same trace.

    An instrumented client can wrap another one (a framework call around a provider call),
    and both spans then report the same tokens. Counting only the outermost span of each
    chain keeps a wrapped call from being counted twice. Ancestry is followed through
    ``parent_id`` over ``index`` (default: ``spans`` themselves), so pass the index of all
    snapshot spans: a span of another session or without one may sit between two LLM spans.
    A chain that cannot be followed to the end (a parent missing from the index) is judged
    by the ancestors that are visible.
    """
    by_id = span_index(spans) if index is None else index

    def has_llm_ancestor(span: Span) -> bool:
        seen = {span.span_id}
        parent_id = span.parent_id
        while parent_id is not None and parent_id not in seen:
            seen.add(parent_id)
            parent = by_id.get((span.trace_id, parent_id))
            if parent is None:
                return False
            if parent.kind is SpanKind.LLM:
                return True
            parent_id = parent.parent_id
        return False

    return [s for s in spans if s.kind is SpanKind.LLM and not has_llm_ancestor(s)]


def tokens_since(spans: Sequence[Span], since: datetime, index: SpanIndex | None = None) -> int:
    """Input plus output tokens of the outermost LLM spans that started at or after ``since``.

    A span whose wrapper started before ``since`` is not counted either way.
    ``input_tokens`` already includes cached tokens.
    """
    return sum(
        s.input_tokens + s.output_tokens
        for s in outermost_llm_spans(spans, index)
        if s.start >= since
    )
