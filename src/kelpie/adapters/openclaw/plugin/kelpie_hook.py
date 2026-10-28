"""OpenClaw hook for Kelpie: appends one JSON line per OpenClaw plugin hook to a spool file.

Self-contained on purpose (standard library only, no ``kelpie`` imports, no syntax newer than
Python 3.8): the Kelpie OpenClaw plugin (``index.js`` next to this file) runs it with the
host's ``python3`` for each hook it forwards, usually where Kelpie is not installed. The
plugin writes one JSON object to stdin, ``{"hook": <name>, "at": <epoch seconds>, "event":
{...}, "ctx": {...}}``, already reduced to the fields below (prompts, messages, tool results
and assistant text never leave the Gateway process). This script always exits 0 and writes
nothing to stdout.

Hooks (field names checked against OpenClaw 2026.10.4, ``hook-runner-global-*.d.ts``, and
seen in a live Gateway run):

- ``session_start`` (``sessionId``, ``sessionKey``, ``resumedFrom``) -> ``session_start``
- ``after_tool_call`` (``toolName``, ``params``, ``toolCallId``, ``error``, ``durationMs``;
  the plugin adds ``outputChars``, the size of the result, and for arguments over 1 MiB sends
  ``paramsDigest``, its own hash of them, instead) -> ``tool``
- ``llm_output`` (``provider``, ``model``, ``usage``; needs the plugin's conversation access)
  -> ``llm``: the run's token usage
- ``agent_end`` (``success``, ``durationMs``; needs conversation access) -> ``run_end``
- ``session_end`` (``reason``, ``nextSessionId``) -> ``session_end``
- ``kelpie_stopped`` (the plugin refused a tool call or a run because of a stop marker; the
  ``event`` it refused) -> ``stopped``
- ``kelpie_resumed`` (the plugin lifted a stop: the owner sent a message, or the session was
  reset; the ``event``) -> ``resumed``

Every line carries the OpenClaw ``sessionId`` (``session_id``: Kelpie's session id, it changes
on ``/new``, reset and daily rollover), ``sessionKey`` (``session_key``, e.g.
``agent:main:main``; Workboard cards link to it; see :func:`safe_session_key`), ``agentId``
(``agent``) and ``runId`` (``run_id``) when OpenClaw supplies them. ``llm`` and ``run_end``
lines also carry the run's ``trigger`` (``user``, ``heartbeat``, ``cron``).

Privacy: prompts, tool arguments and results, messages and assistant text are never written.
A tool call is the tool name, a hash of its arguments, for ``exec``-like tools a redacted
prefix of the command (see :func:`redact_command`; disable with
``KELPIE_OPENCLAW_SPOOL_COMMANDS=0``), whether it failed, and the size of its result in
characters (``output_chars``).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
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

SPOOL_ENV = "KELPIE_OPENCLAW_SPOOL"
MAX_BYTES_ENV = "KELPIE_OPENCLAW_SPOOL_MAX_BYTES"
COMMANDS_ENV = "KELPIE_OPENCLAW_SPOOL_COMMANDS"
DEFAULT_SPOOL = "~/.openclaw/kelpie/spool.jsonl"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
MAX_INPUT = 16 * 1024 * 1024  # a hook input larger than this is not read
MAX_ID = 128  # ids and short enums are cut to this many characters
FUTURE_SKEW = 300  # seconds; an ``at`` further ahead (or missing) is replaced by now
_COMMAND_KEYS = ("command", "cmd")
# Spool format version, in every line. 2: the plugin enforces stop markers.
HOOK_VERSION = 2

EVENTS = {
    "session_start": "session_start",
    "after_tool_call": "tool",
    "llm_output": "llm",
    "agent_end": "run_end",
    "session_end": "session_end",
    "kelpie_stopped": "stopped",
    "kelpie_resumed": "resumed",
}
USAGE_KEYS = ("input", "output", "cacheRead", "cacheWrite")


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
    depth = 0  # brackets open in them ($(…), ${…}): a separator inside is still theirs
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
        if tok in _SEPARATORS and end == i + 1 and not (setup and depth):
            out.append(tok)
            if setup:  # the separator after a setup builtin is its own, too
                extra = min(extra + 1, _MAX_WRAPPED)
            exe, words, setup = True, False, False
        elif setup:
            depth += tok.count("(") + tok.count("{") - tok.count(")") - tok.count("}")
            if out[-1] != _MASK:  # one mask for all its arguments
                out.append(_MASK)
                extra = min(extra + 1, _MAX_WRAPPED)
            plain = "\\" not in tok and not any(q in tok for q in _QUOTES)  # not x\; nor 'x;'
            if end == i + 1 and tok.endswith(";") and plain and not depth:
                out.append(";")  # export X=1; make: the separator glued to the argument
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
                setup, words, depth = True, False, 0
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


# Session keys that name no person: the agent's main session (and its heartbeat lane),
# subagents, cron jobs and OpenClaw's internal background sessions. Hook keys come from
# operator templates (``hooks.mappings[].sessionKey``), so their tail is hashed.
_PLAIN = frozenset({"main", "subagent", "cron", "heartbeat", "internal-session-effects"})
_WHOLE = frozenset({"global", "unknown"})
# Where a conversation key names its peer or group: everything after the marker is theirs.
_KINDS = frozenset({"direct", "dm", "group", "channel"})
_KEY_WORD = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # used with fullmatch
MAX_KEY_INPUT = 4096  # a longer session key is not looked at (it is not OpenClaw's)


PEER_KEY_NAME = "peer.key"  # next to the spool: the secret of the session key pseudonyms
PEER_KEY_BYTES = 32
NO_KEY = "nokey"  # stands in for every pseudonym when the secret cannot be had


def peer_key_path(spool: Path | None = None) -> Path:
    return (spool or spool_path()).with_name(PEER_KEY_NAME)


def read_peer_key(path: Path) -> bytes | None:
    """The secret, if a usable one is there (a regular file, not a symlink, of the right
    size); ``None`` otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | _NO_WAIT)
    except OSError:
        return None
    try:
        raw = os.read(fd, 2 * PEER_KEY_BYTES + 2)
    finally:
        os.close(fd)
    try:
        secret = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return secret if len(secret) == PEER_KEY_BYTES else None


def peer_key(spool: Path | None = None) -> bytes | None:
    """The per-install secret, created on first use (mode 0600, never replaced)."""
    path = peer_key_path(spool)
    secret = read_peer_key(path)
    if secret is not None:
        return secret
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_WAIT, 0o600)
    except FileExistsError:
        return read_peer_key(path)  # another hook created it first
    except OSError:
        return None
    try:
        os.write(fd, os.urandom(PEER_KEY_BYTES).hex().encode("ascii") + b"\n")
    finally:
        os.close(fd)
    return read_peer_key(path)


def safe_session_key(key: Any, secret: bytes | None) -> str | None:
    """A session key without the identity of the person or group it is with.

    OpenClaw keys a per-peer conversation as ``agent:<id>:<channel>:direct:<peerId>`` (or
    ``agent:<id>:direct:<peerId>``, with ``:thread:<id>`` and the like appended), and a group
    as ``...:group:<groupId>``; the peer id is a phone number, a chat user id or a linked name.
    Kept as they are: ``global``, ``unknown``, ``cron:<job>`` and ``agent:<id>:<kind>:...``
    with ``<kind>`` one of ``main``, ``subagent``, ``cron``, ``heartbeat``,
    ``internal-session-effects``. In any other key everything after the first ``direct``,
    ``dm``, ``group`` or ``channel`` within the channel part (else everything after
    ``agent:<id>``, or the whole key) becomes ``~`` and 12 hex digits of its SHA-256, e.g.
    ``agent:main:whatsapp:direct:~3f2a9c01d4e5``. The digits are an HMAC-SHA256 with
    ``secret``, the install's own key (:func:`peer_key`), so the same peer gives the same key
    and a phone number cannot be found by hashing every number; whoever holds the key file
    can still do that. The whole key is hashed, never a cut-off part of it. Without a secret
    every reduced part is ``~nokey``. Kelpie applies the same function, with the same key
    file, to Workboard's keys.
    """
    if isinstance(key, bool) or not isinstance(key, str):
        return None
    text = key.strip()
    if not text or len(text) > MAX_KEY_INPUT:
        return None
    parts = text.split(":")
    if text in _WHOLE or (parts[0] == "cron" and len(text) <= MAX_ID):
        return text
    if parts[0] == "agent" and len(parts) >= 3 and _KEY_WORD.fullmatch(parts[1]):
        head = parts[:2]
        if parts[2] in _PLAIN and len(text) <= MAX_ID:
            return text
        marker = next(
            (i for i in range(2, min(len(parts) - 1, 5)) if parts[i] in _KINDS),
            None,
        )
        if marker is not None and all(_KEY_WORD.fullmatch(p) for p in parts[2:marker]):
            head = parts[: marker + 1]
        return ":".join([*head, "~" + _digest(":".join(parts[len(head) :]), secret)])
    if parts[0] == "hook" and len(parts) >= 2:
        return "hook:~" + _digest(":".join(parts[1:]), secret)
    return "~" + _digest(text, secret)


def _digest(text: str, secret: bytes | None) -> str:
    if secret is None:
        return NO_KEY
    mac = hmac.new(secret, text.encode("utf-8", "replace"), hashlib.sha256)
    return mac.hexdigest()[:12]


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


def _count(value: Any) -> int | None:
    """A non-negative whole number (tokens, milliseconds, characters), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):
        return None


def _at(value: Any, now: float) -> float:
    """When the hook fired, as the plugin saw it; ``now`` if missing or implausible."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return now
    return float(value) if 0 < value <= now + FUTURE_SKEW else now


def _field(data: dict[str, Any], key: str) -> Any:
    """``key`` from the event, else from the hook context."""
    for part in ("event", "ctx"):
        section = data.get(part)
        if isinstance(section, dict) and section.get(key) is not None:
            return section.get(key)
    return None


def _session_key(key: Any) -> str | None:
    """A key that names no one needs no secret: the key file is only read (or created) for
    the others."""
    plain = safe_session_key(key, None)
    if plain is None or NO_KEY not in plain:
        return plain
    try:
        secret = peer_key()
    except Exception:  # the line matters more than its pseudonym
        secret = None
    return safe_session_key(key, secret)


def record(data: dict[str, Any], now: float | None = None) -> dict[str, Any] | None:
    """The spool line for one forwarded hook; ``None`` for hooks Kelpie does not record."""
    kind = EVENTS.get(str(data.get("hook")))
    session = _id(_field(data, "sessionId"))
    if kind is None or session is None:
        return None
    clock = time.time() if now is None else now
    event = data.get("event") if isinstance(data.get("event"), dict) else {}
    rec: dict[str, Any] = {
        "type": kind,
        "at": _at(data.get("at"), clock),
        "session_id": session,
        "v": HOOK_VERSION,
        # The version of the plugin as the Gateway loaded it (index.js): what enforces stops.
        "plugin": _count(data.get("plugin")),
        # The plugin's before_agent_run has run: it has conversation access.
        "gate": data.get("gate") is True,
        "session_key": _session_key(_field(data, "sessionKey")),
        "agent": _id(_field(data, "agentId")),
        "run_id": _id(_field(data, "runId")),
    }
    if kind == "session_start":
        rec["resumed_from"] = _id(event.get("resumedFrom"))
    elif kind == "tool":
        args = event.get("params")
        failed = event.get("error") not in (None, "")
        digest = event.get("paramsDigest")  # arguments too large to pass: hashed by the plugin
        rec.update(
            tool=_id(event.get("toolName")),
            tool_call_id=_id(_field(data, "toolCallId")),
            args_hash=_id(digest) if isinstance(digest, str) else args_hash(args),
            command=_command(args),
            outcome="error" if failed else "ok",
            duration_ms=_count(event.get("durationMs")),
            output_chars=None if failed else _count(event.get("outputChars")),
        )
    elif kind == "llm":
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        rec.update(
            provider=_id(event.get("provider")),
            model=_id(event.get("model")),
            trigger=_id(_field(data, "trigger")),
            **{key: _count(usage.get(key)) for key in USAGE_KEYS},
        )
    elif kind == "run_end":
        success = event.get("success")
        rec.update(
            success=success if isinstance(success, bool) else None,
            duration_ms=_count(event.get("durationMs")),
            trigger=_id(_field(data, "trigger")),
        )
    elif kind == "session_end":
        rec.update(reason=_id(event.get("reason")), next_session_id=_id(event.get("nextSessionId")))
    elif kind in ("stopped", "resumed"):
        rec["event"] = _id(event.get("event"))
    return rec


# Never follow a symlink, never block on a FIFO (with no reader, opening one fails: ENXIO).
_NO_WAIT = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


def _rotate(path: Path, limit: int) -> None:
    """Move a full spool to ``<spool>.1``. Under a lock, and only if the spool is still full
    once the lock is held, so two hooks never rotate twice (losing the older ``.1``)."""
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
    # One O_APPEND write per line: concurrent hooks do not interleave short lines.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | _NO_WAIT
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def main(stdin: Any = None) -> int:
    try:
        raw = (stdin or sys.stdin).read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return 0
        data = json.loads(raw)
        if isinstance(data, dict) and (rec := record(data)) is not None:
            append(rec)
    except Exception:  # a hook must never break the agent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
