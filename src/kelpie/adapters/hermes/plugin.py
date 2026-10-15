"""Hermes hook plugin: appends one JSON line per tool call and session end to a spool file.

Self-contained on purpose (standard library only, no ``kelpie`` imports): Hermes loads it
from its own plugin directory, usually in a Python environment without Kelpie. Copy this
file to ``~/.hermes/plugins/kelpie/__init__.py`` next to a ``plugin.yaml`` (see README.md).

Hooks (names from ``VALID_HOOKS`` in ``hermes_cli/plugins.py`` of Hermes):

- ``post_tool_call``: keyword arguments ``tool_name``, ``args``, ``result``, ``task_id``,
  ``session_id``, ``tool_call_id``, ``duration_ms``, ``status`` (``"ok"`` | ``"error"``),
  ``error_type``, ``error_message`` and more (``model_tools._emit_post_tool_call_hook``;
  missing ids arrive as ``""``). ``tool_call_id`` lets Kelpie match a call that Hermes
  also reports over OTLP, so it is not counted twice.
- ``on_session_end``: observer, fired per turn. Its kwargs are not documented in the Hermes
  source we could read, so every field is read defensively.

Kanban workers get ``HERMES_KANBAN_TASK`` and ``HERMES_KANBAN_RUN_ID`` in their environment
(``hermes_cli/kanban_db_dispatch.py``); those identify the task and the run.

Privacy: raw arguments and results are never written. A tool call is recorded as the tool
name, a hash of its canonical arguments and, for shell-like tools, a redacted prefix of the
command (see :func:`redact_command`; disable with ``KELPIE_HERMES_SPOOL_COMMANDS=0``), and
the size of its result in characters (``output_chars``), never the result itself.

Hooks are fire-and-forget: nothing here raises, blocks on the network or returns a value.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

SPOOL_ENV = "KELPIE_HERMES_SPOOL"
MAX_BYTES_ENV = "KELPIE_HERMES_SPOOL_MAX_BYTES"
COMMANDS_ENV = "KELPIE_HERMES_SPOOL_COMMANDS"
DEFAULT_SPOOL = "~/.hermes/kelpie/spool.jsonl"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
COMMAND_LIMIT = 200
_COMMAND_KEYS = ("command", "cmd")


def spool_path() -> Path:
    return Path(os.environ.get(SPOOL_ENV) or DEFAULT_SPOOL).expanduser()


def _max_bytes() -> int:
    try:
        return max(1, int(os.environ.get(MAX_BYTES_ENV, DEFAULT_MAX_BYTES)))
    except ValueError:
        return DEFAULT_MAX_BYTES


# Identical to kelpie.traces.args.args_hash (a test compares the source): a call reported by
# this hook and over OTLP must hash the same. Keep both copies in sync.
def args_hash(args: Any) -> str:
    """First 16 hex digits of the SHA-256 of the canonical JSON of ``args``."""
    try:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_KEY_VALUE = re.compile(r"^(-{0,2}[A-Za-z_][\w.-]*)=(.*)$")
_SENSITIVE_KEY = re.compile(r"token|secret|passw|key|auth|credential|cookie", re.IGNORECASE)
_SECRET_FLAGS = frozenset(
    {
        "-p",
        "--token",
        "--password",
        "--passwd",
        "--secret",
        "--api-key",
        "--apikey",
        "--access-token",
        "--auth-token",
    }
)
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
_QUERY_SECRET = re.compile(
    r"([?&][\w-]*(?:token|key|secret|passw|auth)[\w-]*=)[^&\s]+", re.IGNORECASE
)
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
_MASK = "***"
_MAX_TOKENS = 4


def redact_command(command: str) -> str | None:
    """A shell command reduced to what is safe to keep: its first line, without leading
    ``VAR=value`` assignments, at most four tokens, secrets masked.

    Masked: values after ``Bearer``, ``Authorization:``, ``--token``, ``--password`` and
    ``-p``; values of ``key=value`` pairs whose key looks like a credential; URL userinfo;
    any run of 24 or more token characters; and the content of quoted arguments. Best
    effort: the aim is that tokens never reach the spool, while ``gh pr checks 5`` or
    ``pytest -q`` stay recognizable.
    """
    lines = command.strip().splitlines()
    tokens = lines[0].split() if lines else []
    while tokens and _ASSIGN.match(tokens[0]):
        tokens.pop(0)
    out: list[str] = []
    mask_next = 0
    for tok in tokens:
        if mask_next:
            out.append(_MASK)
            mask_next -= 1
            continue
        bare = tok.strip("'\"")
        low = bare.lower()
        if low.startswith("authorization:"):
            glued = len(bare) > len("authorization:")
            out.append("Authorization:" + (_MASK if glued else ""))
            mask_next = 1 if glued else 2  # (scheme and) credential
        elif low == "bearer" or low in _SECRET_FLAGS:
            out.append(bare)
            mask_next = 1
        elif (kv := _KEY_VALUE.match(bare)) and _SENSITIVE_KEY.search(kv.group(1)):
            out.append(f"{kv.group(1)}={_MASK}")
        elif bare.startswith("-p") and len(bare) > 2 and not bare.startswith("--"):
            out.append("-p" + _MASK)  # -psecret
        elif tok[:1] in {"'", '"'}:
            out.append(tok[0] + "…")  # quoted text is free-form: drop it
        else:
            out.append(tok)
    text = " ".join(out[:_MAX_TOKENS])
    text = _URL_USERINFO.sub(rf"\1{_MASK}@", text)
    text = _QUERY_SECRET.sub(rf"\1{_MASK}", text)
    text = _LONG_RUN.sub(_MASK, text)
    return text[:COMMAND_LIMIT] or None


def _command(args: Any) -> str | None:
    if os.environ.get(COMMANDS_ENV, "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not isinstance(args, dict):
        return None
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return redact_command(value)
    return None


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _ids(kwargs: dict[str, Any]) -> dict[str, str | None]:
    env = os.environ
    return {
        # The ``task_id`` keyword of Hermes hooks is its internal id, not a kanban task.
        "task_id": _text(env.get("HERMES_KANBAN_TASK")),
        "run_id": _text(env.get("HERMES_KANBAN_RUN_ID") or kwargs.get("run_id")),
        "session_id": _text(kwargs.get("session_id") or env.get("HERMES_SESSION_ID")),
    }


def _rotate(path: Path, limit: int) -> None:
    try:
        if path.stat().st_size >= limit:
            os.replace(path, path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def append(record: dict[str, Any]) -> None:
    """Append one line to the spool, rotating it to ``<spool>.1`` at the size cap."""
    path = spool_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate(path, _max_bytes())
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    # One O_APPEND write per line: concurrent workers do not interleave short lines.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _duration_ms(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _output_chars(result: Any) -> int | None:
    if result is None:
        return None
    if isinstance(result, str):
        return len(result)
    try:
        return len(json.dumps(result, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return len(str(result))


def post_tool_call(**kwargs: Any) -> None:
    try:
        args = kwargs.get("args", kwargs.get("params"))
        status = str(kwargs.get("status") or "").lower()
        record = {
            "type": "tool",
            "at": time.time(),
            **_ids(kwargs),
            "tool": _text(kwargs.get("tool_name") or kwargs.get("tool")),
            "tool_call_id": _text(kwargs.get("tool_call_id")),
            "args_hash": args_hash(args),
            "command": _command(args),
            "outcome": "error" if status in {"error", "failed", "failure"} else "ok",
            "duration_ms": _duration_ms(kwargs.get("duration_ms")),
            "output_chars": _output_chars(kwargs.get("result")),
        }
        append(record)
    except Exception:  # hooks must never break the agent
        return


def on_session_end(**kwargs: Any) -> None:
    try:
        record = {
            "type": "session_end",
            "at": time.time(),
            **_ids(kwargs),
            "completed": kwargs.get("completed")
            if isinstance(kwargs.get("completed"), bool)
            else None,
            "interrupted": (
                kwargs.get("interrupted") if isinstance(kwargs.get("interrupted"), bool) else None
            ),
        }
        append(record)
    except Exception:
        return


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
