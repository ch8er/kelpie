"""Span storage protocol, an in-memory implementation and the aggregate helper.

The SQLite implementation lives in :mod:`kelpie.core.storage`. Spans are keyed by
``(trace_id, span_id)``: appending a span that is already stored is a no-op, so an exporter
that retries a request does not double-count it.

A span's *last time* is its end, or its start while it has no end. Window queries and
retention both use it, so a long session span stays visible while it is still running.

Stores stamp every insert and link update with a growing *version* and count deletes in
``epoch``, so a reader can keep a cache and fetch only what changed (:meth:`changes`).
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from kelpie.core.model import Span, SpanKind, SpanTotals

SpanKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class SpanChanges:
    spans: tuple[Span, ...]  # inserted or updated since the version asked for
    version: int  # pass this as ``since`` next time


def span_key(span: Span) -> SpanKey:
    return (span.trace_id, span.span_id)


def last_at(span: Span) -> datetime:
    return span.end if span.end is not None and span.end > span.start else span.start


class SpanStore(Protocol):
    epoch: int  # grows whenever spans are deleted: cached reads must start over

    def append_spans(self, spans: Sequence[Span], received_at: datetime) -> int:
        """Store new spans; returns how many were not stored before."""
        ...

    def spans_since(self, after: datetime) -> Sequence[Span]:
        """Spans whose last time is at or after ``after``, in start order."""
        ...

    def changes(self, after: datetime, since: int) -> SpanChanges:
        """Spans inserted or updated after version ``since`` whose last time is at or
        after ``after``; ``since=0`` returns them all."""
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
    of its model calls, and counting both would double the session's tokens. An LLM span
    nested in another LLM span (a framework wrapping a provider call, both instrumented)
    is still counted twice; Kelpie does not resolve ancestry here.
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
        self._spans: dict[SpanKey, tuple[Span, int]] = {}  # key -> (span, version)
        self._version = 0
        self.epoch = 0

    def append_spans(self, spans: Sequence[Span], received_at: datetime) -> int:
        added = 0
        with self._lock:
            self._version += 1
            for span in spans:
                if span_key(span) not in self._spans:
                    self._spans[span_key(span)] = (span, self._version)
                    added += 1
        return added

    def spans_since(self, after: datetime) -> Sequence[Span]:
        with self._lock:
            found = [s for s, _ in self._spans.values() if last_at(s) >= after]
        return sorted(found, key=lambda s: s.start)

    def changes(self, after: datetime, since: int) -> SpanChanges:
        with self._lock:
            found = [(v, s) for s, v in self._spans.values() if v > since and last_at(s) >= after]
        found.sort(key=lambda vs: vs[0])
        version = max((v for v, _ in found), default=since)
        return SpanChanges(tuple(s for _, s in found), version)

    def totals_since(self, after: datetime) -> Sequence[SpanTotals]:
        return totals(self.spans_since(after))

    def prune(self, before: datetime, max_rows: int) -> int:
        with self._lock:
            old = [k for k, (s, _) in self._spans.items() if last_at(s) < before]
            for key in old:
                del self._spans[key]
            excess = max(len(self._spans) - max_rows, 0)
            if excess:
                by_age = sorted(self._spans, key=lambda k: last_at(self._spans[k][0]))
                for key in by_age[:excess]:
                    del self._spans[key]
            if old or excess:
                self.epoch += 1
            return len(old) + excess

    def count(self) -> int:
        with self._lock:
            return len(self._spans)

    def inherit_links(self, trace_ids: Collection[str]) -> int:
        wanted = set(trace_ids)
        updated: set[SpanKey] = set()
        with self._lock:
            for _ in range(MAX_LINK_DEPTH):
                changed = False
                for key, (span, _) in list(self._spans.items()):
                    if span.trace_id not in wanted or not span.parent_id:
                        continue
                    if span.task_id and span.session_id:
                        continue
                    found = self._spans.get((span.trace_id, span.parent_id))
                    if found is None:
                        continue
                    parent = found[0]
                    task = span.task_id or parent.task_id
                    session = span.session_id or parent.session_id
                    if (task, session) != (span.task_id, span.session_id):
                        self._version += 1
                        new = replace(span, task_id=task, session_id=session)
                        self._spans[key] = (new, self._version)
                        updated.add(key)
                        changed = True
                if not changed:
                    break
        return len(updated)
