"""Hermes hook plugin (stub): forwards hook payloads to the Kelpie server.

Hooks: ``post_tool_call`` (heartbeat, polling detection, commit reminders) and
``on_session_end`` (exit contract, WIP commit).
"""

from __future__ import annotations

from typing import Any


def post_tool_call(payload: dict[str, Any]) -> None:
    raise NotImplementedError


def on_session_end(payload: dict[str, Any]) -> None:
    raise NotImplementedError
