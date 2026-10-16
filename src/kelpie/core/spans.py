"""Span storage protocol, an in-memory implementation and the aggregate helpers.

The SQLite implementation lives in :mod:`kelpie.core.storage`. Spans are keyed by
``(trace_id, span_id)``: appending a span that is already stored is a no-op, so an exporter
that retries a request does not double-count it.

A span's *last time* is its end, or its start while it has no end. Window queries and
retention both use it, so a long session span stays visible while it is still running.

Stores stamp every insert and link update with a growing *version* and count deletes in
``epoch``, so a reader can keep a cache and fetch only what changed (:meth:`changes`).

**Counted tokens.** Two instrumentations can report the same model call: a framework
``chat`` span around a provider ``chat`` span. An LLM span's tokens count unless an LLM
ancestor in the same trace carries usage itself (:func:`counted_llm`); a wrapper without
usage hides nothing. A counted span is priced by its own model, else by the nearest LLM
ancestor's. Stores decide this per trace whenever spans of the trace are stored, so a
parent arriving after its child corrects the child, and keep the result with the span:
totals and usage are then plain sums. A span whose counting ancestor is pruned later stays
uncounted (the ancestor's tokens were the ones counted).
"""

from __future__ import annotations

import threading
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from kelpie.core.model import Span, SpanKind, SpanTotals

SpanKey = tuple[str, str]
MAX_LINK_DEPTH = 32  # parent chains deeper than this are not followed
# An attribution entry seen again is rewritten only when it changed or is this old, so a
# tick does not write every session it sees.
ATTRIBUTION_REFRESH_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class SpanChanges:
    spans: tuple[Span, ...]  # inserted or updated since the version asked for
    version: int  # pass this as ``since`` next time


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Counted tokens for one (session, task, pricing model); ``input_tokens`` includes
    ``cached_tokens``, as in :class:`kelpie.core.model.SpanTotals`."""

    session_id: str | None
    task_id: str | None
    model: str | None
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who and what spend belongs to, remembered beyond what adapters still report
    (finished sessions and archived tasks drop out of snapshots): session -> owner,
    session -> task and task -> labels, as last seen within the span retention."""

    owners: Mapping[str, str] = field(default_factory=dict)
    session_tasks: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


class LlmNode(Protocol):
    """What :func:`counted_llm` reads of a span (a :class:`Span` or a lighter row)."""

    @property
    def trace_id(self) -> str: ...
    @property
    def span_id(self) -> str: ...
    @property
    def parent_id(self) -> str | None: ...
    @property
    def kind(self) -> SpanKind: ...
    @property
    def model(self) -> str | None: ...
    @property
    def input_tokens(self) -> int: ...
    @property
    def output_tokens(self) -> int: ...
    @property
    def cached_tokens(self) -> int: ...


def span_key(span: Span) -> SpanKey:
    return (span.trace_id, span.span_id)


def last_at(span: Span) -> datetime:
    return span.end if span.end is not None and span.end > span.start else span.start


class SpanStore(Protocol):
    epoch: int  # grows whenever spans are deleted: cached reads must start over

    def append_spans(self, spans: Sequence[Span], received_at: datetime) -> int:
        """Store new spans and decide which of their traces' tokens count; returns how
        many were not stored before."""
        ...

    def spans_since(self, after: datetime) -> Sequence[Span]:
        """Spans whose last time is at or after ``after``, in start order."""
        ...

    def tool_spans_since(self, after: datetime) -> Sequence[Span]:
        """Tool spans only (``kind = tool``), as :meth:`spans_since`."""
        ...

    def changes(self, after: datetime, since: int) -> SpanChanges:
        """Spans inserted or updated after version ``since`` whose last time is at or
        after ``after``; ``since=0`` returns them all."""
        ...

    def totals_since(self, after: datetime) -> Sequence[SpanTotals]:
        """Per-session aggregates over the spans :meth:`spans_since` would return."""
        ...

    def usage_since(
        self, now: datetime, windows: Sequence[timedelta]
    ) -> list[tuple[TokenUsage, ...]]:
        """Counted tokens per (session, task, pricing model) of the spans whose last time
        is within each window before ``now``: one result per window."""
        ...

    def record_attribution(
        self,
        sessions: Iterable[tuple[str, str, str | None]],
        labels: Mapping[str, tuple[str, ...]],
        at: datetime,
    ) -> int:
        """Remember ``(session, owner, task)`` triples and task labels seen at ``at``,
        until :meth:`prune` passes them. Returns the number of entries written."""
        ...

    def attribution(self) -> Attribution:
        """Everything :meth:`record_attribution` remembered."""
        ...

    def prune(self, before: datetime, max_rows: int) -> int:
        """Delete spans whose last time is before ``before``, then the oldest ones beyond
        ``max_rows``, and attribution last seen before ``before``. Returns the number of
        span rows deleted."""
        ...

    def count(self) -> int: ...

    def inherit_links(self, trace_ids: Collection[str]) -> int:
        """Fill a missing ``task_id``/``session_id`` from the parent span, for stored spans
        of these traces, down to every descendant. Exporters send a trace in several
        requests (a child usually ends, and is exported, before its parent), so linkage
        within one request is not enough. Returns the number of updates (0: nothing new)."""
        ...


def has_usage(span: LlmNode) -> bool:
    return span.kind is SpanKind.LLM and (
        span.input_tokens > 0 or span.output_tokens > 0 or span.cached_tokens > 0
    )


def counted_llm[N: LlmNode](
    index: Mapping[SpanKey, N], spans: Iterable[N]
) -> dict[SpanKey, str | None]:
    """The spans among ``spans`` whose tokens count, with the model to price them by.

    A span counts when it is an LLM span with usage and no LLM ancestor in ``index`` (same
    trace, through ``parent_id``, across spans of any kind) carries usage itself. The model
    is the span's own, else the nearest LLM ancestor's. The walk stops at a missing parent,
    after :data:`MAX_LINK_DEPTH` steps, or where the chain loops back (a span that is its
    own ancestor still counts).
    """
    out: dict[SpanKey, str | None] = {}
    for span in spans:
        if not has_usage(span):
            continue
        model, counted = span.model, True
        seen = {span.span_id}
        parent_id = span.parent_id
        while parent_id and parent_id not in seen and len(seen) <= MAX_LINK_DEPTH:
            seen.add(parent_id)
            parent = index.get((span.trace_id, parent_id))
            if parent is None:
                break
            if parent.kind is SpanKind.LLM:
                if has_usage(parent):
                    counted = False
                    break
                model = model or parent.model
            parent_id = parent.parent_id
        if counted:
            out[(span.trace_id, span.span_id)] = model
    return out


def totals(
    spans: Iterable[Span], counted: Collection[SpanKey] | None = None
) -> tuple[SpanTotals, ...]:
    """Aggregate spans per session (spans without a session are left out), sorted by id.

    Tokens and ``llm_calls`` cover counted LLM spans only (see the module docs); an agent
    or workflow span that repeats its calls' usage is not an LLM span and never counts.
    ``counted`` defaults to :func:`counted_llm` over the spans given.
    """
    spans = list(spans)
    keys = counted if counted is not None else counted_llm(_index(spans), spans)
    acc: dict[str, _Acc] = {}
    for span in spans:
        if not span.session_id:
            continue
        a = acc.setdefault(span.session_id, _Acc())
        a.add(span, span_key(span) in keys)
    return tuple(acc[sid].result(sid) for sid in sorted(acc))


def usage(
    spans: Iterable[Span], counted: Mapping[SpanKey, str | None] | None = None
) -> tuple[TokenUsage, ...]:
    """Counted tokens among ``spans`` per (session, task, pricing model); ``counted``
    defaults to :func:`counted_llm` over the spans given."""
    spans = list(spans)
    models = counted if counted is not None else counted_llm(_index(spans), spans)
    acc: dict[tuple[str | None, str | None, str | None], _Usage] = {}
    for span in spans:
        key = span_key(span)
        if key in models:
            acc.setdefault((span.session_id, span.task_id, models[key]), _Usage()).add(span)
    return tuple(acc[k].result(*k) for k in sorted(acc, key=_usage_order))


@dataclass(slots=True)
class _Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def add(self, span: Span) -> None:
        self.calls += 1
        self.input_tokens += span.input_tokens
        self.output_tokens += span.output_tokens
        self.cached_tokens += span.cached_tokens

    def result(self, session: str | None, task: str | None, model: str | None) -> TokenUsage:
        return TokenUsage(
            session,
            task,
            model,
            self.calls,
            self.input_tokens,
            self.output_tokens,
            self.cached_tokens,
        )


def _usage_order(key: tuple[str | None, str | None, str | None]) -> tuple[str, ...]:
    return tuple(v or "" for v in key)


def _index[N: LlmNode](spans: Iterable[N]) -> dict[SpanKey, N]:
    return {(s.trace_id, s.span_id): s for s in spans}


def attribution_stale(seen: datetime, now: datetime) -> bool:
    return (now - seen).total_seconds() >= ATTRIBUTION_REFRESH_SECONDS


@dataclass(slots=True)
class _Acc:
    total: SpanTotals | None = None

    def add(self, span: Span, counted: bool) -> None:
        t = self.total or SpanTotals(session_id=span.session_id or "")
        end = last_at(span)
        self.total = replace(
            t,
            task_id=max(filter(None, (t.task_id, span.task_id)), default=None),
            spans=t.spans + 1,
            llm_calls=t.llm_calls + counted,
            tool_calls=t.tool_calls + (span.kind is SpanKind.TOOL),
            tool_errors=t.tool_errors + (span.kind is SpanKind.TOOL and span.outcome == "error"),
            input_tokens=t.input_tokens + counted * span.input_tokens,
            output_tokens=t.output_tokens + counted * span.output_tokens,
            cached_tokens=t.cached_tokens + counted * span.cached_tokens,
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
        self._traces: dict[str, set[str]] = {}  # trace id -> span ids
        self._counted: dict[SpanKey, str | None] = {}  # counted span -> pricing model
        self._sessions: dict[str, tuple[str, str | None, datetime]] = {}
        self._labels: dict[str, tuple[tuple[str, ...], datetime]] = {}
        self._version = 0
        self.epoch = 0

    def append_spans(self, spans: Sequence[Span], received_at: datetime) -> int:
        added = 0
        touched: set[str] = set()
        with self._lock:
            self._version += 1
            for span in spans:
                key = span_key(span)
                if key not in self._spans:
                    self._spans[key] = (span, self._version)
                    self._traces.setdefault(span.trace_id, set()).add(span.span_id)
                    touched.add(span.trace_id)
                    added += 1
            for trace_id in touched:
                self._recount(trace_id)
        return added

    def _recount(self, trace_id: str) -> None:
        trace = {(trace_id, sid): self._spans[(trace_id, sid)][0] for sid in self._traces[trace_id]}
        counted = counted_llm(trace, trace.values())
        for key in trace:
            if key in counted:
                self._counted[key] = counted[key]
            else:
                self._counted.pop(key, None)

    def spans_since(self, after: datetime) -> Sequence[Span]:
        with self._lock:
            found = [s for s, _ in self._spans.values() if last_at(s) >= after]
        return sorted(found, key=lambda s: s.start)

    def tool_spans_since(self, after: datetime) -> Sequence[Span]:
        return [s for s in self.spans_since(after) if s.kind is SpanKind.TOOL]

    def changes(self, after: datetime, since: int) -> SpanChanges:
        with self._lock:
            found = [(v, s) for s, v in self._spans.values() if v > since and last_at(s) >= after]
        found.sort(key=lambda vs: vs[0])
        version = max((v for v, _ in found), default=since)
        return SpanChanges(tuple(s for _, s in found), version)

    def totals_since(self, after: datetime) -> Sequence[SpanTotals]:
        found = self.spans_since(after)
        with self._lock:
            counted = {k for k in map(span_key, found) if k in self._counted}
        return totals(found, counted)

    def usage_since(
        self, now: datetime, windows: Sequence[timedelta]
    ) -> list[tuple[TokenUsage, ...]]:
        if not windows:
            return []
        cutoffs = [now - w for w in windows]
        earliest = min(cutoffs)
        with self._lock:
            found = [
                (span, model)
                for key, model in self._counted.items()
                if last_at(span := self._spans[key][0]) >= earliest
            ]
        out = []
        for cutoff in cutoffs:
            mine = {span_key(s): (s, m) for s, m in found if last_at(s) >= cutoff}
            out.append(usage((s for s, _ in mine.values()), {k: m for k, (_, m) in mine.items()}))
        return out

    def record_attribution(
        self,
        sessions: Iterable[tuple[str, str, str | None]],
        labels: Mapping[str, tuple[str, ...]],
        at: datetime,
    ) -> int:
        written = 0
        with self._lock:
            for session_id, owner, task in sessions:
                old = self._sessions.get(session_id)
                if old is None or old[:2] != (owner, task) or attribution_stale(old[2], at):
                    self._sessions[session_id] = (owner, task, at)
                    written += 1
            for task_id, names in labels.items():
                prev = self._labels.get(task_id)
                if prev is None or prev[0] != names or attribution_stale(prev[1], at):
                    self._labels[task_id] = (names, at)
                    written += 1
        return written

    def attribution(self) -> Attribution:
        with self._lock:
            return Attribution(
                owners={k: v[0] for k, v in self._sessions.items()},
                session_tasks={k: v[1] for k, v in self._sessions.items() if v[1]},
                labels={k: v[0] for k, v in self._labels.items()},
            )

    def prune(self, before: datetime, max_rows: int) -> int:
        with self._lock:
            old = [k for k, (s, _) in self._spans.items() if last_at(s) < before]
            for key in old:
                self._delete(key)
            excess = max(len(self._spans) - max_rows, 0)
            if excess:
                by_age = sorted(self._spans, key=lambda k: last_at(self._spans[k][0]))
                for key in by_age[:excess]:
                    self._delete(key)
            if old or excess:
                self.epoch += 1
            for sid in [k for k, v in self._sessions.items() if v[2] < before]:
                del self._sessions[sid]
            for tid in [k for k, v in self._labels.items() if v[1] < before]:
                del self._labels[tid]
            return len(old) + excess

    def _delete(self, key: SpanKey) -> None:
        del self._spans[key]
        self._counted.pop(key, None)
        ids = self._traces.get(key[0])
        if ids is not None:
            ids.discard(key[1])
            if not ids:
                del self._traces[key[0]]

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
