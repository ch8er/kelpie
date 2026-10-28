"""What became of a tool call (kelpie.core.tools, #229)."""

from __future__ import annotations

from datetime import UTC, datetime

from kelpie.core.model import Span, SpanKind
from kelpie.core.tools import (
    BLOCKED,
    ERROR,
    EXIT_CODE_ATTRIBUTE,
    OK,
    RESULT_ATTRIBUTE,
    UNKNOWN,
    exit_code_of,
    result_of,
    tool_result,
)

AT = datetime(2026, 3, 2, tzinfo=UTC)


def test_result_classes() -> None:
    assert tool_result(0) == OK
    assert tool_result(1) == tool_result(127) == tool_result(124) == ERROR
    assert tool_result(0, background=True) == tool_result(1, background=True) == UNKNOWN
    assert tool_result(None) == UNKNOWN
    assert tool_result(0, blocked=True) == tool_result(None, blocked=True) == BLOCKED
    assert tool_result(-1) == ERROR  # a platform's own failure to run it, or a signal
    assert tool_result(-1, background=True, blocked=True) == BLOCKED  # refused, not started


def test_reading_them_back() -> None:
    def span(**attrs: object) -> Span:
        return Span("t", "s", "execute_tool", SpanKind.TOOL, AT, attributes=dict(attrs))

    good = span(**{RESULT_ATTRIBUTE: "error", EXIT_CODE_ATTRIBUTE: 2})
    assert (result_of(good), exit_code_of(good)) == ("error", 2)
    odd = span(**{RESULT_ATTRIBUTE: "weird", EXIT_CODE_ATTRIBUTE: True})
    assert (result_of(odd), exit_code_of(odd)) == (None, None)
    assert (result_of(span()), exit_code_of(span())) == (None, None)
