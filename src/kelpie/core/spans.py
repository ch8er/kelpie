"""Span storage protocol, an in-memory implementation and the aggregate helper.

The SQLite implementation lives in :mod:`kelpie.core.storage`. Spans are keyed by
``(trace_id, span_id)``: appending a span that is already stored is a no-op, so an exporter
that retries a request does not double-count it.

A span's *last time* is its end, or its start while it has no end. Window queries and
retention both use it, so a long session span stays visible while it is still running.
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from kelpie.core.model import Span, SpanKind, SpanTotals

SpanKey = tuple[str, str]


def span_key(span: Span) -> SpanKey:
    return (span.trace_id, span.span_id)


def last_at(span: Span) -> datetime:
    return span.end if span.end is not None and span.end > span.start else span.start


class SpanStore(Protocol):
    def append_spans(self, spans: Sequence[Span], received_at: datetime) -> int:
        """Store new spans; returns how many were not stored before."""
        ...

    def spans_since(self, after: datetime) -> Sequence[Span]:
        """Spans whose last time is at or after ``after``, in start order."""
        ...

    def totals_since(self, after: datetime) -> Sequence[SpanTotals]:
        """Per-session aggregates over the spans :meth:`spans_since` would return."""
        ...

    def prune(self, before: datetime, max_rows: int) -> int:
        """Delete spans whose last time is before ``before``, then the oldest ones beyond
        ``max_rows``. Returns the number of rows deleted."""
        ...

    def count(self) -> int: ...

    def inherit_links(self, trace_ids: Collection[str]) -> int:
        """Fill a missing ``task_id``/``session_id`` from the parent span, for stored spans
        of these traces, down to every descendant. Exporters send a trace in several
        requests (a child usually ends, and is exported, before its parent), so linkage
        within one request is not enough. Returns the number of updates (0: nothing new)."""
        ...


MAX_LINK_DEPTH = 32  # parent chains deeper than this are not followed


def totals(spans: Iterable[Span]) -> tuple[SpanTotals, ...]:
    """Aggregate spans per session (spans without a session are left out), sorted by id.

    Tokens are summed over LLM spans only: an agent or workflow span may repeat the usage
    of its model calls, and counting both would double the session's tokens.
    """
    acc: dict[str, _Acc] = {}
    for span in spans:
        if not span.session_id:
            continue
        a = acc.setdefault(span.session_id, _Acc())
        a.add(span)
    return tuple(acc[sid].result(sid) for sid in sorted(acc))


@dataclass(slots=True)
class _Acc:
    total: SpanTotals | None = None

    def add(self, span: Span) -> None:
        t = self.total or SpanTotals(session_id=span.session_id or "")
        end = last_at(span)
        llm = span.kind is SpanKind.LLM
        self.total = replace(
            t,
            task_id=max(filter(None, (t.task_id, span.task_id)), default=None),
            spans=t.spans + 1,
            llm_calls=t.llm_calls + (span.kind is SpanKind.LLM),
            tool_calls=t.tool_calls + (span.kind is SpanKind.TOOL),
            tool_errors=t.tool_errors + (span.kind is SpanKind.TOOL and span.outcome == "error"),
            input_tokens=t.input_tokens + llm * span.input_tokens,
            output_tokens=t.output_tokens + llm * span.output_tokens,
            cached_tokens=t.cached_tokens + llm * span.cached_tokens,
            first_start=min(span.start, t.first_start or span.start),
            last_end=max(end, t.last_end or end),
        )

    def result(self, session_id: str) -> SpanTotals:
        return self.total or SpanTotals(session_id=session_id)


class MemorySpanStore:
    """Span store for ``storage.path = ":memory:"`` and tests. Bounded by :meth:`prune`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: dict[SpanKey, Span] = {}

    def append_spans(self, spans: Sequence[Span], received_at: datetime) -> int:
        added = 0
        with self._lock:
            for span in spans:
                if span_key(span) not in self._spans:
                    self._spans[span_key(span)] = span
                    added += 1
        return added

    def spans_since(self, after: datetime) -> Sequence[Span]:
        with self._lock:
            found = [s for s in self._spans.values() if last_at(s) >= after]
        return sorted(found, key=lambda s: s.start)

    def totals_since(self, after: datetime) -> Sequence[SpanTotals]:
        return totals(self.spans_since(after))

    def prune(self, before: datetime, max_rows: int) -> int:
        with self._lock:
            old = [k for k, s in self._spans.items() if last_at(s) < before]
            for key in old:
                del self._spans[key]
            excess = len(self._spans) - max_rows
            if excess > 0:
                oldest = sorted(self._spans, key=lambda k: last_at(self._spans[k]))[:excess]
                for key in oldest:
                    del self._spans[key]
            return len(old) + max(excess, 0)

    def count(self) -> int:
        with self._lock:
            return len(self._spans)

    def inherit_links(self, trace_ids: Collection[str]) -> int:
        wanted = set(trace_ids)
        updated: set[SpanKey] = set()
        with self._lock:
            for _ in range(MAX_LINK_DEPTH):
                changed = False
                for key, span in list(self._spans.items()):
                    if span.trace_id not in wanted or not span.parent_id:
                        continue
                    if span.task_id and span.session_id:
                        continue
                    parent = self._spans.get((span.trace_id, span.parent_id))
                    if parent is None:
                        continue
                    task = span.task_id or parent.task_id
                    session = span.session_id or parent.session_id
                    if (task, session) != (span.task_id, span.session_id):
                        self._spans[key] = replace(span, task_id=task, session_id=session)
                        updated.add(key)
                        changed = True
                if not changed:
                    break
        return len(updated)
