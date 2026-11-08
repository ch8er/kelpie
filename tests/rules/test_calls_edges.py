"""Edges of the tool-call helpers (``kelpie.rules._calls``) pinned by mutation testing."""

from datetime import UTC, datetime, timedelta

from kelpie.core.model import Span, SpanKind
from kelpie.rules._calls import poll_kind, tool_calls

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)


def tool(sid: str, at: datetime, command: str | None = None, args_hash: str | None = None) -> Span:
    attributes = {"command": command} if command else {}
    return Span(
        "TR-1",
        sid,
        "bash",
        SpanKind.TOOL,
        at,
        tool="bash",
        args_hash=args_hash,
        attributes=attributes,
    )


def test_poll_kind_falls_back_to_the_arguments_without_a_command() -> None:
    patterns = {"ci_status": ("gh run watch",)}
    assert poll_kind(patterns, tool("A", NOW, args_hash="gh run watch 42")) == "ci_status"
    assert poll_kind(patterns, tool("A", NOW)) is None


def test_tool_calls_after_is_strict() -> None:
    at, later = tool("A", NOW), tool("B", NOW + M)
    assert tool_calls([later, at], after=NOW) == [later]
    assert tool_calls([later, at]) == [at, later]
