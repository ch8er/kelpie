"""Tool-call helpers over spans, shared by rules 2 (wait_condition) and 10 (loop_breaker)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from kelpie.core.model import Span, SpanKind

CallKey = tuple[str, str]

# Condition kind -> substrings of a polled command (rule 2's ``poll_patterns``).
PollPatterns = Mapping[str, tuple[str, ...]]


def command(span: Span) -> str:
    """The command a call ran, if the trace recorded one."""
    return str(span.attributes.get("command") or "")


def call_key(span: Span) -> CallKey | None:
    """What makes two calls "the same": tool plus arguments (hash, else command).

    None when the arguments are unknown: such calls are never considered identical, so a
    trace without argument data cannot make a varied session look like a loop.
    """
    args = span.args_hash or command(span)
    return (span.tool or span.name, args) if args else None


def parse_poll_patterns(raw: object) -> dict[str, tuple[str, ...]]:
    """Validate ``poll_patterns`` config: condition kinds mapped to lists of substrings."""
    if not isinstance(raw, Mapping) or not all(
        isinstance(v, list | tuple) and all(isinstance(p, str) and p for p in v)
        for v in raw.values()
    ):
        raise ValueError("poll_patterns must map condition kinds to lists of strings")
    return {str(k): tuple(v) for k, v in raw.items()}


def poll_kind(patterns: PollPatterns, span: Span) -> str | None:
    """The condition kind a call polls for (its command matches a pattern), if any."""
    text = command(span) or span.args_hash or ""
    return next((k for k, pats in patterns.items() if any(p in text for p in pats)), None)


def tool_calls(spans: Sequence[Span], after: datetime | None = None) -> list[Span]:
    """Tool spans in start order, optionally only those starting after ``after``."""
    calls = [s for s in spans if s.kind is SpanKind.TOOL and (after is None or s.start > after)]
    return sorted(calls, key=lambda s: s.start)


@dataclass(frozen=True, slots=True)
class Streak:
    key: CallKey
    calls: tuple[Span, ...]

    @property
    def length(self) -> int:
        return len(self.calls)


def identical_streak(calls: Sequence[Span]) -> Streak | None:
    """The run of identical calls (same known key) at the end of ``calls``."""
    if not calls or (key := call_key(calls[-1])) is None:
        return None
    run: list[Span] = []
    for span in reversed(calls):
        if call_key(span) != key:
            break
        run.append(span)
    return Streak(key, tuple(reversed(run)))


def error_streak(calls: Sequence[Span]) -> tuple[Span, ...]:
    """The run of failed calls at the end of ``calls``."""
    run: list[Span] = []
    for span in reversed(calls):
        if span.outcome != "error":
            break
        run.append(span)
    return tuple(reversed(run))
