"""Claude Code hook for Kelpie: appends one JSON line per hook event to a spool file.

Self-contained on purpose (standard library only, no ``kelpie`` imports): Claude Code runs it
as a command hook, usually in a Python without Kelpie installed. Copy this file to
``~/.claude/kelpie/hook.py`` and register it for the events below in ``settings.json``
(see README.md). It reads the hook input (JSON) from stdin and always exits 0. It never
changes what the agent does, except to enforce a stop (below): only then does it write to
stdout.

Events (field names verified against the hook input schema of Claude Code 2.1.301):

- ``SessionStart`` (``source``, ``model``, ``agent_type``) -> ``session_start``
- ``UserPromptSubmit`` (``source``; the prompt itself is never read) -> ``prompt``
- ``PostToolUse`` / ``PostToolUseFailure`` (``tool_name``, ``tool_input``, ``tool_use_id``,
  ``duration_ms``; ``tool_response`` or ``error``) -> ``tool``
- ``Stop`` -> ``turn_end``; ``StopFailure`` (``error``) -> ``turn_end`` with the error kind
- ``SessionEnd`` (``reason``) -> ``session_end``

Every event carries ``session_id`` (the Claude Code session, the same id its OpenTelemetry
spans carry as ``session.id``) and, from a subagent, ``agent_id``. From the environment of the
session: ``CLAUDE_CODE_TASK_LIST_ID`` (the shared task list; else the session id, which is
Claude Code's own default list outside a team), ``KELPIE_TASK_ID`` (a task the
launcher assigned) and ``KELPIE_AGENT`` (the agent's name on the farm).

Task list updates. For a ``TaskUpdate`` tool call the line also carries the task id, the
status and the owner the session set (``task``, ``task_status``, ``task_owner``), never the
subject or description: that is how Kelpie knows which task a session works on and what it
declared at the end.

Stop. Kelpie stops a session by writing ``<stop dir>/<session_id>.json`` (``stop_dir``,
default ``~/.claude/kelpie/stop``). While that marker holds (``KELPIE_CLAUDE_STOP_TTL``,
default 24 hours), every ``PreToolUse`` of the session is answered with ``continue: false``
and a ``deny`` (the tool does not run and the turn ends), and so is a prompt that a machine
submits (``UserPromptSubmit`` with a ``source`` other than ``user``: ``-p``, SDK, loop and
schedule wakeups, peer messages). A prompt a human types clears the marker and goes through,
and so does the end of the session. Only then does the hook write to stdout.

Privacy: prompts, tool arguments, results and messages are never written. A tool call is the
tool name, a hash of its arguments, for ``Bash`` a redacted prefix of the command (see
:func:`redact_command`; disable with ``KELPIE_CLAUDE_SPOOL_COMMANDS=0``) and the size of its
result in characters (``output_chars``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

SPOOL_ENV = "KELPIE_CLAUDE_SPOOL"
MAX_BYTES_ENV = "KELPIE_CLAUDE_SPOOL_MAX_BYTES"
COMMANDS_ENV = "KELPIE_CLAUDE_SPOOL_COMMANDS"
DEFAULT_SPOOL = "~/.claude/kelpie/spool.jsonl"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_INPUT = 16 * 1024 * 1024  # a hook input larger than this is not read
MAX_ID = 128  # ids and short enums are cut to this many characters
_COMMAND_KEYS = ("command", "cmd")
STOP_DIR_ENV = "KELPIE_CLAUDE_STOP_DIR"
STOP_TTL_ENV = "KELPIE_CLAUDE_STOP_TTL"
DEFAULT_STOP_DIR = "~/.claude/kelpie/stop"
DEFAULT_STOP_TTL = 24 * 3600  # seconds a stop marker holds unless a human clears it
MAX_MARKER = 4096  # bytes; a larger marker file is ignored
MAX_REASON = 500
FUTURE_SKEW = 300  # seconds; a marker dated further ahead is ignored
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # used with fullmatch
# Spool format version, in every line. 1: the hook enforces stop markers (PreToolUse).
HOOK_VERSION = 1
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
TASK_TOOL = "TaskUpdate"
TASK_STATUSES = frozenset({"pending", "in_progress", "completed", "deleted"})

EVENTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "prompt",
    "PostToolUse": "tool",
    "PostToolUseFailure": "tool",
    "Stop": "turn_end",
    "StopFailure": "turn_end",
    "SessionEnd": "session_end",
}


def spool_path() -> Path:
    return Path(os.environ.get(SPOOL_ENV) or DEFAULT_SPOOL).expanduser()


def _max_bytes() -> int:
    try:
        return max(1, int(os.environ.get(MAX_BYTES_ENV, DEFAULT_MAX_BYTES)))
    except ValueError:
        return DEFAULT_MAX_BYTES


# Identical to kelpie.traces.args.args_hash (a test compares results): a call reported by
# this hook and over OTLP must hash the same.
def args_hash(args: Any) -> str:
    """First 16 hex digits of the SHA-256 of the canonical JSON of ``args``."""
    try:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


# --- shared redaction: begin ---
# Byte-for-byte identical in kelpie.traces.command, the Hermes plugin and the Claude Code
# hook (tests/traces/test_redaction.py compares them): the hooks run on agent hosts without
# Kelpie installed, and a command reported both by a hook and over OTLP must redact the same.
COMMAND_LIMIT = 200
_MAX_TOKENS = 4
_MAX_LINE = 4096  # characters of the first line that are looked at
_MASK = "***"
_QUOTES = ("'", '"', "`")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_EXE = re.compile(r"^[A-Za-z0-9_][\w.+-]{0,63}$")
_WORD = re.compile(r"^(?:[a-z][a-z0-9_-]{0,23}|[0-9]{1,10})$")
_FLAG = re.compile(r"^(?:-[A-Za-z0-9]|--[A-Za-z][A-Za-z0-9-]{0,40})$")
_SEPARATORS = frozenset({"|", "||", "|&", "&&", ";", "&"})
_PYTHON_MODULES = frozenset({"pytest", "unittest", "pip", "mypy", "ruff", "black", "build"})
# Wrappers that only run the real command, with the flags of theirs that take a value.
_WRAPPERS = {
    "sudo": frozenset(
        {
            *("-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D"),
            *("--user", "--group", "--host", "--prompt", "--close-from", "--other-user"),
            *("--role", "--type", "--chdir"),
        }
    ),
    "time": frozenset({"-o", "-f", "--output", "--format"}),
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "rtk": frozenset(),
}
_MAX_WRAPPED = 8  # wrapper tokens kept on top of _MAX_TOKENS
_BLANKS = re.compile(r"[ \t]+")  # what bash splits words on


def _quoted_span(tokens: list[str], i: int) -> int:
    """Index after the argument that starts at ``tokens[i]``: past the token that closes
    the first quote it opens, else the next token. An unclosed quote runs to the end."""
    tok = tokens[i]
    if "\\" in tok and any(q in tok for q in _QUOTES):
        return len(tokens)  # an escaped quote: where the argument ends is not clear
    quote = next((c for c in tok if c in _QUOTES), None)
    if quote is None or tok.count(quote) % 2 == 0:
        return i + 1
    for j in range(i + 1, len(tokens)):
        if tokens[j].count(quote) % 2 == 1:
            return j + 1
    return len(tokens)


def redact_command(command: str) -> str | None:
    """A shell command reduced to a prefix that is safe to keep, e.g. ``gh pr checks 5``.

    An allowlist, not a list of secrets: of the first line (its first 4096 characters),
    without leading ``VAR=value`` assignments, at most four arguments are kept, and only
    these: the executable without its directory; plain lowercase words and numbers right
    after it (subcommands: ``gh pr checks 5``, ``npm run build``); flag names (``-q``,
    ``--watch``; a cluster or glued value keeps its first letter, ``-p***``); a pipe or
    list separator and the next executable. Everything else is ``***``: flag values,
    ``--flag=value`` values, paths, URLs, hosts, ``user:pass``, quoted text, variables. Once
    a flag or a masked argument appears, later plain words are masked too: they may be its
    value. A secret passed as a lowercase word right after the executable
    (``redis-cli auth hunter2``) is still kept.
    """
    lines = command.strip().splitlines()
    if not lines:
        return None
    tokens = [t for t in _BLANKS.split(lines[0][:_MAX_LINE]) if t]
    if len(lines[0]) > _MAX_LINE and tokens:
        tokens[-1] = _MASK  # the cut may have split it
    i = 0
    while i < len(tokens) and _ASSIGN.match(tokens[i]):
        i = _quoted_span(tokens, i)
    out: list[str] = []
    exe = True  # the next argument is an executable
    words = False  # plain words are still kept (right after the executable)
    wrapper = None  # flags of the wrapper whose options come next (sudo -u x make)
    extra = 0  # wrapper tokens, not counted against _MAX_TOKENS
    while i < len(tokens) and len(out) < _MAX_TOKENS + extra:
        tok = tokens[i]
        end = _quoted_span(tokens, i)
        name, eq, _ = tok.partition("=")
        if wrapper is not None:
            before = len(out)
            if _ASSIGN.match(tok):
                out.append(f"{name}={_MASK}")  # env FOO=bar
            elif _FLAG.match(tok):
                out.append(tok)
                if tok in wrapper and end < len(tokens):
                    out.append(_MASK)  # its value: sudo -u root
                    end = _quoted_span(tokens, end)
            elif eq and _FLAG.match(name):
                out.append(f"{name}={_MASK}")
            elif tok[:1] == "-" and tok[1:2].isalpha() and tok[1:2] != "-":
                out.append(tok[:2] + _MASK)
            else:
                wrapper, exe = None, True  # the wrapped command: read it as an executable
                continue
            extra = min(extra + len(out) - before, _MAX_WRAPPED)
            i = end
            continue
        if tok.startswith("#"):
            break
        if end > i + 1 or any(q in tok for q in _QUOTES):
            out.append(_MASK)
            exe = words = False
        elif tok in _SEPARATORS:
            out.append(tok)
            exe, words = True, False
        elif exe:
            base = tok.rstrip("/").rsplit("/", 1)[-1]
            ok = bool(_EXE.match(base)) and not _ASSIGN.match(base)
            out.append(base if ok else _MASK)
            exe, words = False, ok
            if ok and base in _WRAPPERS:
                wrapper = _WRAPPERS[base]
                extra = min(extra + 1, _MAX_WRAPPED)
        elif _FLAG.match(tok):
            out.append(tok)
            words = False
        elif eq and _FLAG.match(name):
            out.append(f"{name}={_MASK}")
            words = False
        elif tok[:1] == "-" and tok[1:2].isalpha() and tok[1:2] != "-":
            out.append(tok[:2] + _MASK)  # -la, -sSu, -pSECRET: a cluster or a glued value
            words = False
        elif words and _WORD.match(tok):
            out.append(tok)
        elif out and out[-1] == "-m" and tok in _PYTHON_MODULES:
            out.append(tok)  # python -m pytest
        else:
            out.append(_MASK)
            words = False
        i = end
    return " ".join(out)[:COMMAND_LIMIT] or None


# --- shared redaction: end ---


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


def _id(value: Any) -> str | None:
    """A short identifier or enum value; anything else (or empty) gives ``None``."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text[:MAX_ID] if text else None


def _env(name: str) -> str | None:
    return _id(os.environ.get(name))


def _output_chars(result: Any) -> int | None:
    if result is None:
        return None
    if isinstance(result, str):
        return len(result)
    try:
        return len(json.dumps(result, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return len(str(result))


def _duration_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(value)


def _task_update(args: Any) -> dict[str, str | None]:
    """Task id, status and owner of a ``TaskUpdate`` call; no free text."""
    if not isinstance(args, dict):
        return {}
    status = _id(args.get("status"))
    return {
        "task": _id(args.get("taskId")),
        "task_status": status if status in TASK_STATUSES else None,
        "task_owner": _id(args.get("owner")),
    }


def record(data: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """The spool line for one hook input; ``None`` for events Kelpie does not record."""
    kind = EVENTS.get(str(data.get("hook_event_name")))
    session = _id(data.get("session_id"))
    if kind is None or session is None:
        return None
    rec: dict[str, Any] = {
        "type": kind,
        "at": time.time() if now is None else now,
        "session_id": session,
        "v": HOOK_VERSION,
        "agent_id": _id(data.get("agent_id")),
        # Claude Code's own fallback when no list is set (outside a team): the session id.
        "task_list": _env("CLAUDE_CODE_TASK_LIST_ID") or session,
        "kelpie_task": _env("KELPIE_TASK_ID"),
        "agent": _env("KELPIE_AGENT"),
    }
    event = data.get("hook_event_name")
    if kind == "session_start":
        rec.update(
            source=_id(data.get("source")),
            model=_id(data.get("model")),
            agent_type=_id(data.get("agent_type")),
        )
    elif kind == "prompt":
        rec["source"] = _id(data.get("source"))
    elif kind == "tool":
        tool = _id(data.get("tool_name"))
        args = data.get("tool_input")
        failed = event == "PostToolUseFailure"
        rec.update(
            tool=tool,
            tool_call_id=_id(data.get("tool_use_id")),
            args_hash=args_hash(args),
            command=_command(args),
            outcome="error" if failed else "ok",
            duration_ms=_duration_ms(data.get("duration_ms")),
            output_chars=None if failed else _output_chars(data.get("tool_response")),
        )
        if tool == TASK_TOOL and not failed:
            rec.update(_task_update(args))
    elif kind == "turn_end":
        rec["error"] = _id(data.get("error")) if event == "StopFailure" else None
    elif kind == "session_end":
        rec["reason"] = _id(data.get("reason"))
    return rec


# Never follow a symlink, never block on a FIFO (with no reader, opening one fails: ENXIO).
_NO_WAIT = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _rotate(path: Path, limit: int) -> None:
    """Move a full spool to ``<spool>.1``. Under a lock, and only if the spool is still full
    once the lock is held, so two sessions never rotate twice (losing the older ``.1``)."""
    try:
        if path.stat().st_size < limit:
            return
    except FileNotFoundError:
        return
    lock = os.open(path.with_name(path.name + ".lock"), os.O_WRONLY | os.O_CREAT | _NO_WAIT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_size >= limit:
                os.replace(path, path.with_name(path.name + ".1"))
    finally:
        os.close(lock)  # releases the lock


def append(rec: dict[str, Any]) -> None:
    """Append one line to the spool, rotating it to ``<spool>.1`` at the size cap."""
    path = spool_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _rotate(path, _max_bytes())
    line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
    # One O_APPEND write per line: concurrent sessions do not interleave short lines.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NO_WAIT
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def stop_dir() -> Path:
    return Path(os.environ.get(STOP_DIR_ENV) or DEFAULT_STOP_DIR).expanduser()


def _stop_ttl() -> float:
    try:
        return max(1.0, float(os.environ.get(STOP_TTL_ENV, DEFAULT_STOP_TTL)))
    except ValueError:
        return float(DEFAULT_STOP_TTL)


def marker_path(session_id: str) -> Path | None:
    """Where Kelpie's stop marker for a session lives; ``None`` for an unusable id."""
    return stop_dir() / f"{session_id}.json" if _SESSION_ID.fullmatch(session_id) else None


def clean_reason(reason: str) -> str:
    text = _CONTROL.sub(" ", reason).strip() or "no reason given"
    return text if len(text) <= MAX_REASON else text[: MAX_REASON - 3] + "..."


def active_stop(session_id: str, now: float) -> str | None:
    """The reason of the session's stop marker while it holds, else ``None``."""
    path = marker_path(session_id)
    if path is None:
        return None
    try:
        # O_NOFOLLOW: a symlink planted in place of a marker is not followed.
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
    except OSError:
        return None
    try:
        raw = os.read(fd, MAX_MARKER + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_MARKER:
        return None
    data = json.loads(raw.decode("utf-8", "replace"))
    at = data.get("at") if isinstance(data, dict) else None
    reason = data.get("reason") if isinstance(data, dict) else None
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not isinstance(reason, str):
        return None
    if not now - _stop_ttl() <= at <= now + FUTURE_SKEW:
        return None
    return clean_reason(reason)


def clear_stop(session_id: str) -> None:
    path = marker_path(session_id)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink()


def stop_output(event: str, reason: str, session_id: str) -> dict[str, Any]:
    """What the hook answers Claude Code while a stop marker holds."""
    short = f"Stopped by Kelpie: {reason}."
    # How to resume goes only to the human (stopReason, a blocked prompt's reason); the model
    # reads permissionDecisionReason and is not told how to lift its own stop.
    text = (
        f"{short} To resume, a human types a new prompt in this session "
        f"or removes {marker_path(session_id)}."
    )
    out: dict[str, Any] = {"continue": False, "stopReason": text}
    if event == "PreToolUse":
        out["hookSpecificOutput"] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": short,
        }
    else:
        out.update(decision="block", reason=text)
    return out


def _stop_check(data: dict[str, Any], now: float) -> dict[str, Any] | None:
    """Apply a stop marker to this hook input; the answer to print, if any."""
    event = data.get("hook_event_name")
    session = _id(data.get("session_id"))
    if session is None:
        return None
    human = event == "UserPromptSubmit" and data.get("source") == "user"
    if human or event == "SessionEnd":
        clear_stop(session)  # a human resumes the session, or it is over
        return None
    if event not in ("PreToolUse", "UserPromptSubmit"):
        return None
    reason = active_stop(session, now)
    if reason is None:
        return None
    with contextlib.suppress(Exception):  # the stop matters more than its record
        append(
            {
                "type": "stopped",
                "at": now,
                "session_id": session,
                "v": HOOK_VERSION,
                "agent_id": _id(data.get("agent_id")),
                "event": event,
            }
        )
    return stop_output(str(event), reason, session)


def _answer(text: str, stdout: Any = None) -> None:
    """Write the answer unbuffered: nothing is left to flush at exit, so a closed reader
    cannot turn into a non-zero exit."""
    if stdout is not None:
        stdout.write(text)
        return
    data = text.encode("utf-8")
    while data:
        data = data[os.write(1, data) :]


def main(stdin: Any = None, stdout: Any = None) -> int:
    try:
        raw = (stdin or sys.stdin).read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return 0
        data = json.loads(raw)
        if not isinstance(data, dict):
            return 0
        now = time.time()
        try:
            answer = _stop_check(data, now)
        except Exception:  # an unreadable marker must not keep the event from the spool
            answer = None
        if answer is not None:
            _answer(json.dumps(answer) + "\n", stdout)
            return 0
        if (rec := record(data, now)) is not None:
            append(rec)
    except Exception:  # a hook must never break the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
