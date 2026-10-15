"""Hermes hook plugin: appends one JSON line per tool call and session end to a spool file.

Self-contained on purpose (standard library only, no ``kelpie`` imports): Hermes loads it
from its own plugin directory, usually in a Python environment without Kelpie. Copy this
file to ``~/.hermes/plugins/kelpie/__init__.py`` next to a ``plugin.yaml`` (see README.md).

Hooks (names from ``VALID_HOOKS`` in ``hermes_cli/plugins.py`` of Hermes):

- ``post_tool_call``: keyword arguments ``tool_name``, ``args``, ``result``, ``task_id``,
  ``session_id``, ``duration_ms``, ``status`` (``"ok"`` | ``"error"``), ``error_type``,
  ``error_message`` and more (``model_tools._emit_post_tool_call_hook``).
- ``on_session_end``: observer, fired per turn. Its kwargs are not documented in the Hermes
  source we could read, so every field is read defensively.

Kanban workers get ``HERMES_KANBAN_TASK`` and ``HERMES_KANBAN_RUN_ID`` in their environment
(``hermes_cli/kanban_db_dispatch.py``); those identify the task and the run.

Privacy: raw arguments and results are never written. A tool call is recorded as the tool
name, a hash of its canonical arguments and, for shell-like tools, the first 200
characters of the command (disable with ``KELPIE_HERMES_SPOOL_COMMANDS=0``).

Hooks are fire-and-forget: nothing here raises, blocks on the network or returns a value.
"""

from __future__ import annotations

import hashlib
import json
import os
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


def args_hash(args: Any) -> str:
    """First 16 hex digits of the SHA-256 of the canonical JSON of ``args``."""
    try:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


def _command(args: Any) -> str | None:
    if os.environ.get(COMMANDS_ENV, "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not isinstance(args, dict):
        return None
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value[:COMMAND_LIMIT]
    return None


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _ids(kwargs: dict[str, Any]) -> dict[str, str | None]:
    env = os.environ
    return {
        "task_id": _text(env.get("HERMES_KANBAN_TASK") or kwargs.get("task_id")),
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


def post_tool_call(**kwargs: Any) -> None:
    try:
        args = kwargs.get("args", kwargs.get("params"))
        status = str(kwargs.get("status") or "").lower()
        record = {
            "type": "tool",
            "at": time.time(),
            **_ids(kwargs),
            "tool": _text(kwargs.get("tool_name") or kwargs.get("tool")),
            "args_hash": args_hash(args),
            "command": _command(args),
            "outcome": "error" if status in {"error", "failed", "failure"} else "ok",
            "duration_ms": _duration_ms(kwargs.get("duration_ms")),
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
