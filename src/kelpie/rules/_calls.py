"""Tool-call helpers over spans, shared by rules 2 (wait_condition) and 10 (loop_breaker)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kelpie.core.model import Span, SpanKind

CallKey = tuple[str, str]


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
