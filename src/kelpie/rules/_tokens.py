"""Token accounting over spans, shared by rules 10 (loop_breaker) and 11 (no_progress)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from kelpie.core.model import Span, SpanKind


def outermost_llm_spans(spans: Sequence[Span]) -> list[Span]:
    """LLM spans that have no LLM ancestor among ``spans`` (same trace, via ``parent_id``).

    An instrumented client can wrap another one (a framework call around a provider call),
    and both spans then report the same tokens. Counting only the outermost span of each
    chain keeps a wrapped call from being counted twice. A chain that cannot be followed to
    the end (the parent is not among ``spans``) is judged by the ancestors that are visible.
    """
    by_id = {(s.trace_id, s.span_id): s for s in spans}

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


def tokens_since(spans: Sequence[Span], since: datetime) -> int:
    """Input plus output tokens of the outermost LLM spans that started at or after ``since``.

    Ancestry is judged over all of ``spans``, so a span whose wrapper started before
    ``since`` is not counted either way. ``input_tokens`` already includes cached tokens.
    """
    return sum(
        s.input_tokens + s.output_tokens for s in outermost_llm_spans(spans) if s.start >= since
    )
