"""What became of a tool call that ran a command, as its platform recorded it (#229).

A command's exit code is the most reliable account of a run: a platform's own "error" field
can be empty on a failed build, so rules read the exit code. Adapters that know a call's
exit code put it on the tool span (:data:`EXIT_CODE_ATTRIBUTE`) with the call's
:data:`RESULT_ATTRIBUTE`, one of:

- ``ok``: a foreground command that exited 0;
- ``error``: a foreground command that exited non-zero (``127`` not found, ``124`` timed
  out, ``1`` a failed build; a negative code is the platform's own failure to run it, or a
  signal): the exit code is its error class;
- ``blocked``: the platform refused to run it (a policy, an approval it did not get),
  neither a failure nor a success of the command;
- ``unknown``: a command sent to the background (its ``0`` means "started", not "built"),
  or no exit code. Never counted as either.

Only calls that run a command (a shell tool) get them: another tool's exit code may be some
other process's. With them, :data:`COMMAND_WHOLE_ATTRIBUTE` says the span's command (a
redacted prefix) is the whole command, :data:`COMMAND_MULTILINE_ATTRIBUTE` that it has more
lines. Rules read them with :func:`result_of`, :func:`exit_code_of`, :func:`command_whole`
and :func:`command_multiline`; a span without them has no known result
(the span's ``outcome`` stays what the exporter said; a blocked call keeps it too). Which
platform fields mean background or blocked is each adapter's (see its README).
"""

from __future__ import annotations

from kelpie.core.model import Span

EXIT_CODE_ATTRIBUTE = "kelpie.tool.exit_code"
RESULT_ATTRIBUTE = "kelpie.tool.result"
# True when the span's command (a redacted prefix) still ends where the command did: one
# line, nothing cut after it. Without it, the exit code may be a dropped pipe's.
COMMAND_WHOLE_ATTRIBUTE = "kelpie.tool.command_whole"
# True when the command has more lines than its first, which is all a redacted prefix keeps:
# the last line's exit code is not the first's command's.
COMMAND_MULTILINE_ATTRIBUTE = "kelpie.tool.command_multiline"
OK = "ok"
ERROR = "error"
BLOCKED = "blocked"
UNKNOWN = "unknown"
RESULTS = frozenset({OK, ERROR, BLOCKED, UNKNOWN})


def tool_result(exit_code: int | None, *, background: bool = False, blocked: bool = False) -> str:
    """The result class of a call (see the module docs)."""
    if blocked:
        return BLOCKED
    if background or exit_code is None:
        return UNKNOWN
    return OK if exit_code == 0 else ERROR


def result_of(span: Span) -> str | None:
    """The span's result class, if its adapter recorded one."""
    value = span.attributes.get(RESULT_ATTRIBUTE)
    return value if isinstance(value, str) and value in RESULTS else None


def exit_code_of(span: Span) -> int | None:
    """The span's exit code, if its adapter recorded one."""
    value = span.attributes.get(EXIT_CODE_ATTRIBUTE)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def command_whole(span: Span) -> bool:
    """Whether the span's command is the whole command (nothing cut after it)."""
    return span.attributes.get(COMMAND_WHOLE_ATTRIBUTE) is True


def command_multiline(span: Span) -> bool:
    """Whether the span's command has more lines than the one it keeps."""
    return span.attributes.get(COMMAND_MULTILINE_ATTRIBUTE) is True
