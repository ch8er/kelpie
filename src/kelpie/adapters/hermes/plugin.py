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
the size of its result in characters (``output_chars``), never the result itself. For the
terminal tool also its exit code (an int), whether it went to the background or Hermes
refused it, and whether the redacted prefix still ends where the command did
(``command_whole``): booleans and a number, nothing of the output.

Hooks are fire-and-forget: nothing here raises, blocks on the network or returns a value.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - not POSIX
    fcntl = None  # type: ignore[assignment]

SPOOL_ENV = "KELPIE_HERMES_SPOOL"
MAX_BYTES_ENV = "KELPIE_HERMES_SPOOL_MAX_BYTES"
COMMANDS_ENV = "KELPIE_HERMES_SPOOL_COMMANDS"
DEFAULT_SPOOL = "~/.hermes/kelpie/spool.jsonl"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
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


# --- shared redaction: begin ---
# Byte-for-byte identical in kelpie.traces.command, the Hermes plugin, the Claude Code hook
# and the OpenClaw hook (tests/traces/test_redaction.py compares them): the hooks run on agent
# hosts without Kelpie installed, and a command reported both by a hook and over OTLP must
# redact the same.
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
# Wrappers that only run the real command: their flags that take a value, and those that
# do not. Any other flag of a wrapper may take one: the rest of the command is masked.
_WRAPPERS = {
    "sudo": (
        frozenset(
            {
                *("-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D", "-T", "-R"),
                *("--user", "--group", "--host", "--prompt", "--close-from", "--other-user"),
                *("--role", "--type", "--chdir", "--command-timeout", "--chroot"),
            }
        ),
        frozenset(
            {
                *("-A", "-b", "-E", "-H", "-i", "-K", "-k", "-n", "-P", "-S", "-s", "-B"),
                *("--askpass", "--background", "--preserve-env", "--set-home", "--login"),
                *("--remove-timestamp", "--reset-timestamp", "--non-interactive"),
                *("--preserve-groups", "--stdin", "--shell", "--bell"),
            }
        ),
    ),
    "time": (
        frozenset({"-o", "-f", "--output", "--format"}),
        frozenset({"-p", "-v", "-a", "-q", "--portability", "--verbose", "--append", "--quiet"}),
    ),
    "env": (
        frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
        frozenset({"-i", "-0", "-v", "--ignore-environment", "--null", "--debug"}),
    ),
    "nice": (frozenset({"-n", "--adjustment"}), frozenset()),
    "nohup": (frozenset(), frozenset()),
    "command": (frozenset(), frozenset({"-p", "-v", "-V"})),
    "exec": (frozenset({"-a"}), frozenset({"-c", "-l"})),
    "rtk": (frozenset(), frozenset()),
}
_MAX_WRAPPED = 8  # wrapper tokens kept on top of _MAX_TOKENS
# Shell builtins that only set up for the command after them (``export X=… && make``,
# ``cd dir && make``): kept as a name with every argument masked, and, like a wrapper's
# tokens, not counted against _MAX_TOKENS, so the command after the separator is kept.
_SETUP = frozenset({"export", "cd", "pushd", "set", "source", ".", "unset", "umask", "ulimit"})
# A setup builtin's argument Kelpie can read for sure (an allowlist): letters, digits and
# ``_ . / ~ : = + , @ % $ -``, no quote, escape, bracket, comment or operator in it. With any
# other argument, where bash ends the builtin's arguments is not clear: the rest is masked.
_SIMPLE_ARG = re.compile(r"^[\w./~:=+,@%$-]*$")
_BLANKS = re.compile(r"[ \t]+")  # what bash splits words on


def _quoted_span(tokens: list[str], i: int) -> int:
    """Index after the argument that starts at ``tokens[i]``: past the token that closes
    the first quote it opens, else the next token. An unclosed quote runs to the end."""
    tok = tokens[i]
    if "\\" in tok and any(q in tok for q in _QUOTES):
        return len(tokens)  # an escaped quote: where the argument ends is not clear
    kinds = {c for c in tok if c in _QUOTES}
    if len(kinds) > 1:
        return len(tokens)  # 'x'"y: counting one kind would end it in the wrong place
    quote = next(iter(kinds), None)
    if quote is None or tok.count(quote) % 2 == 0:
        return i + 1
    for j in range(i + 1, len(tokens)):
        if any(q in tokens[j] for q in _QUOTES if q != quote):
            return len(tokens)  # another kind of quote inside: not clear either
        if tokens[j].count(quote) % 2 == 1:
            return j + 1
    return len(tokens)


def _wrapper_flag(
    tok: str, valued: frozenset[str], plain: frozenset[str]
) -> tuple[str, bool, bool]:
    """A wrapper's flag as kept, whether the next argument is its value, and whether it is
    unknown (so its value, or what follows, cannot be told apart from the command)."""
    if tok == "--":
        return tok, False, False
    if tok[:2] == "--" or tok[1:2] == "-":
        known = tok in valued or tok in plain
        return (tok if _FLAG.match(tok) else _MASK), tok not in plain, not known
    for k in range(1, len(tok)):  # a cluster: -iu root, -Eu root, -uroot
        flag = "-" + tok[k]
        if flag in plain:
            continue
        last = k == len(tok) - 1
        if flag in valued:
            return (tok, True, False) if last else (tok[: k + 1] + _MASK, False, False)
        return tok[: k + 1] + ("" if last else _MASK), True, True
    return tok, False, False


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
    (``redis-cli auth hunter2``) is still kept. A setup builtin (``export``, ``cd``,
    ``source``, ``set``, ...) keeps its name and one ``***`` for all its arguments, and its
    tokens do not count against the four, so ``export X=… && ./sim.sh test`` keeps the
    command it sets up: ``export *** && sim.sh test``.
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
    setup = False  # in a setup builtin's arguments (export X=… &&): all masked
    extra = 0  # wrapper and setup tokens, not counted against _MAX_TOKENS
    while i < len(tokens) and len(out) < _MAX_TOKENS + extra:
        tok = tokens[i]
        end = _quoted_span(tokens, i)
        name, eq, _ = tok.partition("=")
        if wrapper is not None:
            before = len(out)
            if _ASSIGN.match(tok):
                out.append(f"{name}={_MASK}")  # env FOO=bar
            elif eq and tok[:1] == "-" and _FLAG.match(name):
                out.append(f"{name}={_MASK}")
            elif tok[:1] == "-" and tok[1:].isdigit() and len(tok) < 5:
                out.append(tok)  # nice -5
            elif tok[:1] == "-" and len(tok) > 1 and end == i + 1:
                kept, value, unknown = _wrapper_flag(tok, *wrapper)
                out.append(kept)
                if value and end < len(tokens):
                    out.append(_MASK)  # its value: sudo -u root
                    end = _quoted_span(tokens, end)
                if unknown:  # whatever comes next may be its value
                    wrapper, words = None, False
            else:
                wrapper, exe = None, True  # the wrapped command: read it as an executable
                continue
            extra = min(extra + len(out) - before, _MAX_WRAPPED)
            i = end
            continue
        if tok.startswith("#"):
            break
        if tok in _SEPARATORS and end == i + 1:
            out.append(tok)
            if setup:  # the separator after a setup builtin is its own, too
                extra = min(extra + 1, _MAX_WRAPPED)
            exe, words, setup = True, False, False
        elif setup:
            if out[-1] != _MASK:  # one mask for all its arguments
                out.append(_MASK)
                extra = min(extra + 1, _MAX_WRAPPED)
            glued = tok.endswith(";")  # export X=1; make: the separator glued to it
            arg = tok[:-1] if glued else tok
            if end != i + 1 or not (arg == _MASK or _SIMPLE_ARG.match(arg)):  # *** redacted
                break  # $(…), 'x'"y", x\;, ;#: where its arguments end is not clear
            if glued and len(out) < _MAX_TOKENS + extra:  # within the cap, as any token
                out.append(";")
                extra = min(extra + 1, _MAX_WRAPPED)
                exe, setup = True, False
        elif end > i + 1 or any(q in tok for q in _QUOTES):
            out.append(_MASK)
            exe = words = False
        elif exe:
            base = tok.rstrip("/").rsplit("/", 1)[-1]
            ok = (bool(_EXE.match(base)) and not _ASSIGN.match(base)) or base == "."
            out.append(base if ok else _MASK)
            exe, words = False, ok
            if ok and base in _WRAPPERS:
                wrapper = _WRAPPERS[base]
                extra = min(extra + 1, _MAX_WRAPPED)
            elif ok and base in _SETUP:
                setup, words = True, False
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


def _shape(args: dict[str, Any]) -> dict[str, bool]:
    """How far the redacted command (a prefix) speaks for the command's exit code:
    ``command_complete`` when the command is one line, no separator is glued to a word
    (``test;swiftlint`` masks it) and the prefix shows every separator the line has (none
    was dropped with the words after it); ``command_whole`` when the redaction also reached
    the line's end (a separator after it would be kept too) and its last word is not masked;
    ``command_multiline`` when the command has more lines (the last decides the exit code,
    and only the first is kept)."""
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            lines = value.strip().splitlines()
            if len(lines) > 1:
                return {"command_multiline": True}
            line = lines[0]
            kept = redact_command(value)
            if not kept or len(line) > _MAX_LINE - 2 or _glued(line) or _substituted(line):
                return {}
            words, shown = _words(line), kept.split()
            if _separators(words) != _separators(shown):
                return {}
            if shown[-1] in _SEPARATOR_WORDS and words[-1] != shown[-1]:
                return {}  # cut right after a separator: the command after it was dropped
            out = {"command_complete": True}
            if _MASK not in kept.split()[-1] and redact_command(f"{line} ;") == f"{kept} ;":
                out["command_whole"] = True
            return out
    return {}


def _words(line: str) -> list[str]:
    """A line's words, a plain word's glued ``;`` (``cd ios;``) as a word of its own, as the
    redaction reads it."""
    out = []
    for word in line.split():
        if len(word) > 1 and word.endswith(";") and _SIMPLE_ARG.match(word[:-1]):
            out += [word[:-1], ";"]
        else:
            out.append(word)
    return out


def _separators(words: list[str]) -> list[str]:
    return [w for w in words if w in _SEPARATOR_WORDS]


def _substituted(line: str) -> bool:
    """Whether a separator may be inside a command substitution (``$(ls | head)``)."""
    return any(s in line for s in ("$(", "<(", ">(", "`")) and bool(_SEPARATOR_CHARS.search(line))


_SEPARATOR_CHARS = re.compile(r"[;|&]")
_SEPARATOR_WORDS = frozenset({";", "|", "||", "&", "&&", "|&"})
_REDIRECT = re.compile(r"\d*>&\d*|&>>?|\d*>\|")  # 2>&1, &>log, >| : not separators


def _glued(line: str) -> bool:
    """Whether a separator character is part of a word (``a;b``, ``test;swiftlint``,
    ``'x|y'``); a plain word's trailing ``;`` is a separator of its own (:func:`_words`)."""
    for word in _words(line):
        if word in _SEPARATOR_WORDS:
            continue
        if _SEPARATOR_CHARS.search(_REDIRECT.sub("", word)):
            return True
    return False


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
    """Move a full spool to ``<spool>.1``, under ``<spool>.lock`` (which ``kelpie spool
    scrub`` also takes) and only if it is still full once the lock is held."""
    try:
        if path.stat().st_size < limit:
            return
    except FileNotFoundError:
        return
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    lock = os.open(path.with_name(path.name + ".lock"), flags, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        with contextlib.suppress(FileNotFoundError):
            if path.stat().st_size >= limit:
                os.replace(path, path.with_name(path.name + ".1"))
    finally:
        os.close(lock)


def append(record: dict[str, Any]) -> None:
    """Append one line to the spool, rotating it to ``<spool>.1`` at the size cap."""
    path = spool_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _rotate(path, _max_bytes())
    line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
    # One O_APPEND write per line: concurrent workers do not interleave short lines. Never
    # through a symlink, never blocking on a FIFO (with no reader, opening one fails).
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
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


MAX_RESULT_PARSE = 1 << 20  # characters of a tool result read for its exit code


TERMINAL_TOOL = "terminal"  # Hermes' shell tool: its exit code is the command's
MAX_EXIT_CODE = 2**31  # a larger one is no exit code


def _result_fields(tool: str | None, args: Any, result: Any) -> dict[str, Any]:
    """For the terminal tool only (other tools' exit codes are not the call's: ``process``
    reports the code of the job it polls or kills): the exit code its result reports
    (``exit_code``, an int), whether Hermes refused the call (``status: "blocked"``, or
    ``"pending_approval"``: it never ran; an exit code -1 alone is also a degraded backend or
    a fatal error, a failure), whether the command went to the background (asked for, or
    promoted from the foreground for a long timeout: its 0 means started), and whether the
    redacted command still ends where the command did (:func:`_shape`: a pipe or a second
    line the redaction dropped decides the exit code). The
    result's ``error`` key is not read: it is null on a failed build too. Nothing of the
    output is kept."""
    if tool != TERMINAL_TOOL:
        return {}
    out: dict[str, Any] = {}
    if isinstance(args, dict):
        if args.get("background") is True:
            out["background"] = True
        out.update(_shape(args))
    data = result
    if isinstance(result, str):
        if len(result) > MAX_RESULT_PARSE or not result.lstrip().startswith("{"):
            return out
        try:
            data = json.loads(result)
        except (ValueError, RecursionError):
            return out
    if not isinstance(data, dict):
        return out
    code = data.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool) and abs(code) <= MAX_EXIT_CODE:
        out["exit_code"] = code
    if data.get("status") in ("blocked", "pending_approval"):
        out["blocked"] = True
    if data.get("promoted_from_foreground") or ("pid" in data and "session_id" in data):
        out["background"] = True  # a background spawn: started, not done
    return out


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
        record.update(_result_fields(record["tool"], args, kwargs.get("result")))
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
