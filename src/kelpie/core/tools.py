"""What became of a tool call, as its platform recorded it (incident 20, #229).

A shell command's exit code is the only reliable account of a run: the Hermes terminal tool
returns ``{"output", "exit_code", "error"}`` and its ``error`` is null on a failed build too,
so Kelpie never reads it. Adapters that know a call's exit code put it on the tool span
(:data:`EXIT_CODE_ATTRIBUTE`) with the call's :data:`RESULT_ATTRIBUTE`, one of:

- ``ok``: a foreground command that exited 0;
- ``error``: a foreground command that exited non-zero (``127`` not found, ``124`` timed
  out, ``1`` a failed build): the exit code is its error class;
- ``blocked``: the platform refused to run it (Hermes: ``status: "blocked"``, exit code
  ``-1``), neither a failure nor a success of the tool;
- ``unknown``: a background command (its ``0`` means "started", not "built"), or no exit
  code. Never counted as either.

Rules read them with :func:`result_of` and :func:`exit_code_of`; a span without them has
no known result (the span's ``outcome`` stays what the exporter said).
"""

from __future__ import annotations

from kelpie.core.model import Span

EXIT_CODE_ATTRIBUTE = "kelpie.tool.exit_code"
RESULT_ATTRIBUTE = "kelpie.tool.result"
OK = "ok"
ERROR = "error"
BLOCKED = "blocked"
UNKNOWN = "unknown"
RESULTS = frozenset({OK, ERROR, BLOCKED, UNKNOWN})
BLOCKED_EXIT = -1  # Hermes' exit code for a command it refused to run


def tool_result(exit_code: int | None, *, background: bool = False, blocked: bool = False) -> str:
    """The result class of a call (see the module docs)."""
    if blocked or exit_code == BLOCKED_EXIT:
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
