"""``kelpie doctor`` checks for ``[adapters.claude_code]``.

Each check is ``(status, name, detail, fix)`` with status ``PASS``, ``FAIL`` or ``SKIP``;
:mod:`kelpie.doctor` renders them. Read-only: nothing is created or written, including the
stop directory, which is only tested for being writable.
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters._spool import open_regular
from kelpie.adapters.claude_code.adapter import (
    FUTURE_SKEW,
    ClaudeCodeAdapter,
    ClaudeCodeError,
    format_age,
)

CheckTuple = tuple[str, str, str, str]

FRESH = timedelta(hours=1)  # the newest spool line must be younger than this
# How a registered Kelpie hook command is recognized: the installed copy, or the file run
# straight from a Kelpie checkout.
HOOK_MARKS = ("kelpie/hook.py", "claude_code/hook.py")
# Tool events: a matcher narrower than every tool means some tools are not seen.
TOOL_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure"})
ALL_TOOLS = (None, "", "*", ".*")  # matchers that cover every tool (matchers are regexes)
# Events the hook must be registered for; PreToolUse only matters when stop is on.
EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
    "StopFailure",
    "SessionEnd",
)
STOP_EVENT = "PreToolUse"
MAX_SETTINGS = 1024 * 1024
QUICKSTART = "docs/quickstart-claude-code.md"

LISTS, HOOKS, REGISTERED, STOP = (
    "claude code task lists",
    "claude code hooks fire",
    "claude code hook registered",
    "claude code stop markers",
)


def _ok(name: str, detail: str) -> CheckTuple:
    return ("PASS", name, detail, "")


def _skip(name: str, detail: str) -> CheckTuple:
    return ("SKIP", name, detail, "")


def _fail(name: str, detail: str, fix: str) -> CheckTuple:
    return ("FAIL", name, detail, fix)


def check_lists(adapter: ClaudeCodeAdapter) -> tuple[CheckTuple, int | None]:
    """The check, and how many watched tasks are in progress (None when unknown)."""
    if not adapter.task_lists:
        return _skip(LISTS, "no task_lists configured: sessions only, no board tasks"), None
    fix = (
        "set CLAUDE_CODE_TASK_LIST_ID=<name> on the farm's sessions and the same name in "
        f"task_lists; check tasks_dir (now {adapter.tasks_dir})"
    )
    if (problem := adapter.check()) is not None:
        return _fail(LISTS, problem, fix), None
    try:
        counts = adapter.list_counts()
    except ClaudeCodeError as exc:
        return _fail(LISTS, str(exc), fix), None
    detail = ", ".join(
        f"{name}: {total} tasks, {busy} in progress" for name, (total, busy) in counts.items()
    )
    return _ok(LISTS, f"{detail} ({adapter.tasks_dir})"), sum(b for _, b in counts.values())


def check_hooks(adapter: ClaudeCodeAdapter, now: datetime, running: int | None) -> CheckTuple:
    spool = adapter.spool
    if spool is None:
        return _skip(HOOKS, "spool disabled")
    fix = f"install and register the hook ({QUICKSTART}, step 4) and start a session"

    def quiet(detail: str) -> CheckTuple:
        if running == 0:
            return _skip(
                HOOKS, f"{detail}; no watched task is in progress, start one and run doctor again"
            )
        return _fail(HOOKS, detail, fix)

    if not spool.exists():
        return quiet(f"spool missing ({spool})")
    newest = adapter.last_spool_event(until=now + FUTURE_SKEW)
    if newest is None:
        return quiet(f"spool has no readable lines ({spool})")
    age = now - newest.at
    if age > FRESH:
        return quiet(f"newest spool line is {format_age(age)} old ({spool})")
    if adapter.stop_dir is not None and newest.version < 1:
        return _fail(
            HOOKS,
            f"the newest spool line comes from a hook without stop support ({spool})",
            f"copy the current hook.py to the agent hosts ({QUICKSTART}, step 4)",
        )
    return _ok(HOOKS, f"newest spool line {format_age(age)} old ({spool})")


def _registered(settings: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    """Hook events whose command runs the Kelpie hook, and those of them on a tool event
    whose matcher is narrower than every tool (``*`` or empty)."""
    hooks = settings.get("hooks")
    found: set[str] = set()
    narrow: set[str] = set()
    if not isinstance(hooks, Mapping):
        return found, narrow
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else ():
            if not isinstance(group, Mapping):
                continue
            entries = group.get("hooks")
            for entry in entries if isinstance(entries, list) else ():
                command = entry.get("command") if isinstance(entry, Mapping) else None
                if isinstance(command, str) and any(m in command for m in HOOK_MARKS):
                    matcher = group.get("matcher")
                    if str(event) in TOOL_EVENTS and matcher not in ALL_TOOLS:
                        narrow.add(str(event))
                    else:
                        found.add(str(event))
    return found, narrow - found


def _read_settings(path: Path) -> Any:
    """The parsed settings file. Agents can write ``~/.claude``: opened without following a
    symlink or blocking on a FIFO, regular files only, a bounded read. Raises OSError or
    ValueError (the message never carries the file's content)."""
    fd = open_regular(path)
    try:
        raw = os.read(fd, MAX_SETTINGS + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_SETTINGS:
        raise ValueError("too large to read")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError) as exc:
        raise ValueError(f"not valid JSON ({type(exc).__name__})") from None


def check_registered(adapter: ClaudeCodeAdapter) -> CheckTuple:
    """Whether the user settings next to ``tasks_dir`` register the hook for every event.

    Hooks may also live in a project's ``.claude/settings.json``, which Kelpie cannot find:
    without any Kelpie hook in the user settings the check is skipped, not failed. Only the
    ``hooks`` and ``disableAllHooks`` keys are looked at; nothing else of the file (which can
    hold credentials) reaches the output."""
    path = adapter.tasks_dir.parent / "settings.json"
    try:
        settings = _read_settings(path)
    except FileNotFoundError:
        return _skip(REGISTERED, f"{path} not found; hooks may be in project settings")
    except (OSError, ValueError) as exc:
        if path.is_symlink():
            why = "a symlink, not followed (agents can write next to it)"
        elif isinstance(exc, ValueError):
            why = str(exc)
        else:
            why = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
        return _skip(REGISTERED, f"{path} not readable: {why}")
    if not isinstance(settings, Mapping):
        return _skip(REGISTERED, f"{path} is not a settings object")
    fix = f"register the hook for every event listed in {QUICKSTART}, step 4"
    if settings.get("disableAllHooks") is True:
        return _fail(REGISTERED, f"disableAllHooks is set in {path}: no hook runs", fix)
    found, narrow = _registered(settings)
    if not found and not narrow:
        return _skip(REGISTERED, f"no Kelpie hook in {path}; hooks may be in project settings")
    wanted = (*EVENTS, STOP_EVENT) if adapter.stop_dir is not None else EVENTS
    missing = [e for e in wanted if e not in found]
    if missing:
        why = " (needed to stop sessions)" if missing == [STOP_EVENT] else ""
        if narrow & set(missing):
            why += f"; {', '.join(sorted(narrow & set(missing)))} only for some tools (matcher)"
        return _fail(REGISTERED, f"not registered for {', '.join(missing)}{why} in {path}", fix)
    return _ok(REGISTERED, f"{len(found)} events in {path}")


def check_stop_dir(adapter: ClaudeCodeAdapter) -> CheckTuple:
    path = adapter.stop_dir
    if path is None:
        return _skip(STOP, "stop_dir = false: stop actions become alerts")
    fix = (
        f"run Kelpie as the user that runs Claude Code, or set stop_dir (and "
        f"KELPIE_CLAUDE_STOP_DIR in the sessions) to a directory both can use (now {path})"
    )
    if path.is_symlink():
        return _fail(STOP, f"{path} is a symlink; markers are not written through one", fix)
    if path.exists():
        if not path.is_dir():
            return _fail(STOP, f"{path} is not a directory", fix)
        if not os.access(path, os.W_OK | os.X_OK):
            return _fail(STOP, f"{path} is not writable by this user", fix)
        return _ok(STOP, f"{path} writable")
    parent = next((p for p in path.parents if p.exists()), None)
    if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        return _fail(STOP, f"{path} does not exist and cannot be created here", fix)
    return _ok(STOP, f"{path} will be created on the first stop")


def run(adapter: ClaudeCodeAdapter, now: datetime) -> tuple[list[CheckTuple], int | None]:
    """The checks, and how many watched tasks are in progress (None when unknown)."""
    lists, running = check_lists(adapter)
    checks = [
        lists,
        check_hooks(adapter, now, running),
        check_registered(adapter),
        check_stop_dir(adapter),
    ]
    return checks, running


def skipped(why: str) -> list[CheckTuple]:
    return [_skip(name, why) for name in (LISTS, HOOKS, REGISTERED, STOP)]
