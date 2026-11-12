"""Provider limit hits read from traces, and the concurrency they suggest.

Pure: no I/O and no clock. A **limit hit** is a model call (an LLM span) the provider
refused for a usage or rate limit: HTTP 429 in Claude Code's ``status_code``, or an
``error.type`` that names a rate limit (``429``, ``rate_limit_error``, ``RateLimitError``,
``too_many_requests``, ...). An overload (HTTP 529, ``overloaded_error``) is the provider's
own capacity, not the farm's limit, and is not counted. The provider is the span's
``gen_ai.provider.name`` (or ``gen_ai.system``), else the one its model belongs to (a
``provider_for`` callable, :meth:`kelpie.core.spend.SpendConfig.provider_for`), else
:data:`UNKNOWN`.

**Suggested concurrency.** For each hit, the sessions with a model call of the same
provider running at that moment (a call that started at or before the hit and ended no
more than ``CONCURRENT_SLACK`` before it) are counted: the concurrency the limit was reached
at. The suggestion is one less than the lowest of those counts, at least 1: below the
lowest concurrency that ever hit the limit in the window. When the limit was hit with a
single session running, concurrency is not what reaches it (a plan's token limit, a
weekly cap): there is no suggestion and the reason says so. It is a suggestion for a
person (a report line and a metric); Kelpie never changes a farm's concurrency by itself.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from kelpie.core.model import Span, SpanKind

UNKNOWN = "unknown"  # a provider no attribute or model tells
CONCURRENT_SLACK = timedelta(seconds=60)  # a call that ended this soon before a hit counts
MAX_PROVIDER = 64  # characters of a provider name taken from a span
_STATUS_KEYS = ("status_code", "http.response.status_code")
_PROVIDER_KEYS = ("gen_ai.provider.name", "gen_ai.system")
_LIMIT_TYPES = frozenset(
    {
        "429",
        "rate_limit",
        "rate_limit_error",
        "ratelimiterror",
        "rate_limit_exceeded",
        "ratelimitexceeded",
        "too_many_requests",
        "toomanyrequests",
        "resource_exhausted",
        "quota_exceeded",
        "insufficient_quota",
    }
)


def is_limit_hit(span: Span) -> bool:
    """Whether ``span`` is a model call the provider refused for a limit (module doc)."""
    if span.kind is not SpanKind.LLM:
        return False
    attrs = span.attributes
    for key in _STATUS_KEYS:
        value = attrs.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float | str) and str(value).strip() == "429":
            return True
    kind = attrs.get("error.type")
    if isinstance(kind, str):
        token = kind.strip().lower().replace("-", "_").replace(" ", "_")
        return token in _LIMIT_TYPES or token.rsplit(".", 1)[-1] in _LIMIT_TYPES
    return False


def provider_of(span: Span, provider_for: Callable[[str | None], str | None]) -> str:
    """The provider ``span`` called (module doc)."""
    for key in _PROVIDER_KEYS:
        value = span.attributes.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()[:MAX_PROVIDER]
    return provider_for(span.model) or UNKNOWN


@dataclass(frozen=True, slots=True)
class ProviderLimits:
    """One provider's limit hits in the window and what they suggest.

    ``at_hits``: the sessions running at each hit (lowest, highest); ``suggested``: the
    concurrency to stay under, None with ``reason`` when there is none to give."""

    provider: str
    hits: int
    calls: int  # model calls of the provider in the window, hits included
    at_hits: tuple[int, int] | None
    peak: int  # the most sessions with a call of the provider running at once
    suggested: int | None
    reason: str = ""

    def line(self) -> str:
        """One line for a person: the hits, and the suggestion or why there is none."""
        head = f"{self.provider}: {self.hits} limit hit(s) in {self.calls} model call(s)"
        if self.at_hits is not None:
            low, high = self.at_hits
            span = str(low) if low == high else f"{low}-{high}"
            head += f", with {span} session(s) running at once"
        if self.suggested is not None:
            return (
                f"{head}; suggested concurrency {self.suggested} (below the lowest that "
                f"reached the limit; the most seen at once was {self.peak})"
            )
        return f"{head}; {self.reason}" if self.reason else head


def provider_limits(
    spans: Iterable[Span],
    start: datetime,
    end: datetime,
    provider_for: Callable[[str | None], str | None],
) -> tuple[ProviderLimits, ...]:
    """Every provider with a limit hit among the model calls that ended within
    [``start``, ``end``), by provider name. Pure: the same spans give the same answer."""
    calls: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        if span.kind is not SpanKind.LLM:
            continue
        last = span.end or span.start
        if start <= last < end:
            calls[provider_of(span, provider_for)].append(span)
    out = []
    for provider in sorted(calls):
        found = calls[provider]
        hits = sorted((s for s in found if is_limit_hit(s)), key=_order)
        if hits:
            out.append(_limits(provider, found, hits))
    return tuple(out)


def _order(span: Span) -> tuple[datetime, str, str]:
    return (span.end or span.start, span.trace_id, span.span_id)


def _intervals(calls: Sequence[Span]) -> list[tuple[datetime, datetime]]:
    """Per session, the union of its calls' running times, each call's end stretched by
    ``CONCURRENT_SLACK``; a call with no session is a session of its own. Intervals of
    one session never overlap, so the intervals covering a moment are distinct sessions."""
    by_session: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for s in calls:
        key = s.session_id or f"{s.trace_id}/{s.span_id}"
        by_session[key].append((s.start, max(s.end or s.start, s.start) + CONCURRENT_SLACK))
    out: list[tuple[datetime, datetime]] = []
    for spans in by_session.values():
        spans.sort()
        lo, hi = spans[0]
        for a, b in spans[1:]:
            if a <= hi:
                hi = max(hi, b)
            else:
                out.append((lo, hi))
                lo, hi = a, b
        out.append((lo, hi))
    return out


def _limits(provider: str, calls: Sequence[Span], hits: Sequence[Span]) -> ProviderLimits:
    intervals = _intervals(calls)
    starts = sorted(a for a, _ in intervals)
    ends = sorted(b for _, b in intervals)

    def running(at: datetime) -> int:  # sessions whose interval covers ``at``
        return bisect_right(starts, at) - bisect_left(ends, at)

    at_hits = [max(1, running(h.end or h.start)) for h in hits]
    peak = max(running(a) for a in starts)
    low, high = min(at_hits), max(at_hits)
    if low <= 1:
        return ProviderLimits(
            provider,
            len(hits),
            len(calls),
            (low, high),
            max(peak, 1),
            None,
            "the limit was reached with one session running: concurrency is not what "
            "reaches it (a plan's token or weekly limit); no concurrency to suggest",
        )
    return ProviderLimits(provider, len(hits), len(calls), (low, high), peak, low - 1)
