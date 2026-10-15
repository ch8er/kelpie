"""Repeated-call detection over tool spans, shared by rules 2 (wait_condition) and 10."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kelpie.core.model import Span, SpanKind


def call_key(span: Span) -> tuple[str, str]:
    """What makes two tool calls "the same": tool plus arguments hash (or command)."""
    args = span.args_hash or str(span.attributes.get("command", ""))
    return (span.tool or span.name, args)


def tool_calls(spans: Sequence[Span], after: datetime | None = None) -> list[Span]:
    """Tool spans in start order, optionally only those starting after ``after``."""
    calls = [s for s in spans if s.kind is SpanKind.TOOL and (after is None or s.start > after)]
    return sorted(calls, key=lambda s: s.start)


@dataclass(frozen=True, slots=True)
class Streak:
    key: tuple[str, str]
    calls: tuple[Span, ...]

    @property
    def length(self) -> int:
        return len(self.calls)


def identical_streak(calls: Sequence[Span]) -> Streak | None:
    """The run of identical calls at the end of ``calls``."""
    if not calls:
        return None
    key = call_key(calls[-1])
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
