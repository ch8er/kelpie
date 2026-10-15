"""Token accounting over spans (used by rule 11, no_progress).

Which tokens count is decided by :func:`kelpie.core.spans.counted_llm`, the same rule span
totals and budgets use, so a rule never disagrees with them about double counting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from kelpie.core.model import Span
from kelpie.core.spans import counted_llm

SpanIndex = Mapping[tuple[str, str], Span]


def span_index(spans: Sequence[Span]) -> SpanIndex:
    """Spans by (trace id, span id): build it once per snapshot and share it."""
    return {(s.trace_id, s.span_id): s for s in spans}


def outermost_llm_spans(spans: Sequence[Span], index: SpanIndex | None = None) -> list[Span]:
    """LLM spans of ``spans`` whose tokens count.

    An instrumented client can wrap another one (a framework call around a provider call),
    and both spans then report the same tokens: a span is left out when an LLM ancestor in
    the same trace carries usage itself. Ancestry is followed through ``parent_id`` over
    ``index`` (default: ``spans`` themselves), so pass the index of all snapshot spans: a
    span of another session or without one may sit between two LLM spans. A chain that
    cannot be followed to the end (a parent missing from the index) is judged by the
    ancestors that are visible.
    """
    counted = counted_llm(span_index(spans) if index is None else index, spans)
    return [s for s in spans if (s.trace_id, s.span_id) in counted]


def tokens_since(spans: Sequence[Span], since: datetime, index: SpanIndex | None = None) -> int:
    """Input plus output tokens of the counted LLM spans that started at or after ``since``.

    A span whose wrapper started before ``since`` is not counted either way.
    ``input_tokens`` already includes cached tokens.
    """
    return sum(
        s.input_tokens + s.output_tokens
        for s in outermost_llm_spans(spans, index)
        if s.start >= since
    )
