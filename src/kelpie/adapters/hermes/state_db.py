"""Hermes' own session store (``state.db``, one per profile), read-only: waits in sessions.

Each Hermes profile keeps its sessions and their messages in a SQLite database of its own:
``<home>/state.db`` for the ``default`` profile and ``<home>/profiles/<name>/state.db`` for
the others. Kelpie opens each strictly read-only (``mode=ro``, ``query_only``), one
short-lived connection per read, and never writes. A database that is missing is skipped;
one it cannot read, or whose rows it cannot make sense of, is logged once and skipped, so
the board is read all the same.

**Which sessions.** Those whose ``source`` is one of ``sources`` (default: Kanban workers,
their subagents and cron jobs, not a person's own chats), active since ``since``: Hermes'
``last_activity_at``, else their start, on its index for recent sessions; at most
``MAX_SESSIONS`` per profile, the most recently active. Their assistant messages with tool
calls since then are read on Hermes' ``(session_id, timestamp)`` index. A rewound message
(``active = 0``, not compacted) is left out, and a tool call is counted once however often
compaction copied it (by its call id and time: a copy keeps both, and an id alone repeats).
A session is a worker's by its ``source`` or, where Hermes records it, ``created_source``.

**Waits** (issue #197, rule 2). A foreground ``terminal`` call whose command waits becomes
a :class:`Wait` (:func:`wait_shape`): ``sleep``, ``gh run watch``, ``gh pr checks --watch``,
also behind ``timeout``, ``env``, ``nohup``, ``bash -c`` and the like, or a shell loop
(``while``, ``until``, ``for``) over one of them (``"sleep loop"``). A command, group or loop
sent to the background with ``&`` does not wait (what follows it still counts), a heredoc's
body is not a command, and a loop of sleeps under ``SHORT_SLEEP`` that runs no ``gh``, inside a
larger script, waits for a server, not for CI. How
long it waited, kept as its bucket (:func:`kelpie.core.model.wait_bucket`): when the whole
command only waits, the time until its own result message (at most the ``sleep`` arguments
when it only sleeps); otherwise the ``sleep`` arguments alone, or unknown. Only that shape
and bucket are kept, never the command's other words or values, so the hooks spool's
redaction still holds.
"""

from __future__ import annotations

import json
import logging
import math
import re
import shlex
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.hermes.git_state import warn_once
from kelpie.core.model import wait_bucket

log = logging.getLogger(__name__)

DEFAULT_PROFILE = "default"  # the profile whose home is Hermes' home itself
# Hermes session sources that are agents at work, not a person's chat.
DEFAULT_SOURCES = ("kanban", "subagent", "cron")
BUSY_TIMEOUT_MS = 2000
TERMINAL_TOOL = "terminal"
MAX_COMMAND = 4096  # characters of a command parsed for a wait; a longer one is not read
MAX_TOOL_CALLS = 65536  # characters of a message's tool calls; more is not read
MAX_SESSIONS = 2000  # most recently active sessions read per profile
MAX_SLEEP = 7 * 86400  # seconds: a longer sleep argument counts as this long
# A loop of shorter sleeps inside a script waits for a server to start, not for CI.
SHORT_SLEEP = timedelta(seconds=10)
_EARLIEST, _LATEST = 946684800.0, 7258118400.0  # 2000-01-01 .. 2200-01-01: a sane epoch
_CHUNK = 500  # ids per IN (...) query
_PUNCTUATION = "();<>|&\n"  # shell operators; a newline separates commands too
_LOOPS = frozenset({"while", "until", "for"})
_CONDITION_LOOPS = frozenset({"while", "until"})  # their condition is a command of its own
# Words that start a command inside a compound one: the command follows them.
_LEADERS = frozenset({"do", "then", "else", "elif", "if", "!", "{", "("})
_ENDS = frozenset({"done", "fi", "esac", "}", ")"})  # keywords that end a compound command
# The keywords that open what each closing keyword ends.
_OPENERS = {
    "done": frozenset({"while", "until", "for", "select"}),
    "fi": frozenset({"if"}),
    "esac": frozenset({"case"}),
    "}": frozenset({"{"}),
}
_SETUP = frozenset({"cd", "pushd", "popd", "export", "set", "source", ".", "unset", "true", ":"})
# Commands that only run the command after them: their options that take a value.
_WRAPPERS = {
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "time": frozenset({"-o", "-f", "--output", "--format"}),
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_GH_VALUED = frozenset({"-R", "--repo", "--hostname"})  # gh's global flags that take a value
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_GH_IN = re.compile(r"(?:^|[\s\"'`(;|&])gh(?:\s|$)")  # gh run inside a word: "$(gh …)", `gh …`
_FD = re.compile(r"(?<![^\s;&|(])\d(?=[<>])")  # a file descriptor glued to a redirection
_SLEEP_ARG = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")
_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True, slots=True)
class Wait:
    """One wait in a Hermes session, as ``state.db`` recorded it (shape and bucket only)."""

    profile: str
    session: str  # Hermes' session id in that profile's state.db
    cwd: str | None  # the session's working directory: the task's workspace for a worker
    at: datetime
    shape: str
    waited: timedelta | None


@dataclass(frozen=True, slots=True)
class Shape:
    """What :func:`wait_shape` finds in a command."""

    shape: str
    slept: timedelta | None  # the sum of its ``sleep`` arguments, if known
    whole: bool  # the command does nothing but wait (and set up for it: ``cd``, ``export``)


def state_dbs(home: Path) -> list[tuple[str, Path]]:
    """``(profile, path)`` of every profile's ``state.db`` under Hermes' ``home``."""
    found = [(DEFAULT_PROFILE, home / "state.db")]
    try:
        entries = sorted((home / "profiles").iterdir())
    except OSError:
        entries = []
    found += [(p.name.casefold(), p / "state.db") for p in entries if not p.name.startswith(".")]
    return [(name, path) for name, path in found if path.is_file()]


def read_waits(
    home: Path, since: datetime, sources: Collection[str] = DEFAULT_SOURCES
) -> list[Wait]:
    """The waits in sessions of every profile active since ``since``."""
    dbs = state_dbs(home)
    if not dbs:
        warn_once((str(home), "no-state"), "hermes: no state.db under %s (state_home)", home)
    waits: list[Wait] = []
    for profile, path in dbs:
        try:
            waits += _read_db(profile, path, since, sources)
        except Exception as exc:  # whatever a database holds must not stop the board's read
            warn_once((str(path), "state"), "hermes: cannot read %s: %s", path, exc)
    return waits


def _read_db(profile: str, path: Path, since: datetime, sources: Collection[str]) -> list[Wait]:
    conn = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
    )
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only=1")
        conn.execute("BEGIN")  # one read transaction: a consistent view
        start = since.timestamp()
        sessions = _sessions(conn, start, sources)
        calls = _wait_calls(conn, sessions, start)
        ends = {
            (sid, at, call_id): end
            for sid, at, call_id, shape in calls
            if shape.whole and call_id and (end := _result_at(conn, sid, at, call_id)) is not None
        }
    finally:
        conn.close()
    waits = []
    for sid, at, call_id, shape in calls:
        waited = shape.slept
        if (end := ends.get((sid, at, call_id))) is not None:
            elapsed = timedelta(seconds=max(0.0, end - at))
            # Nothing but sleeps: at most their arguments (a result may come late).
            capped = shape.shape == "sleep" and shape.slept is not None
            waited = min(elapsed, shape.slept) if capped else elapsed  # type: ignore[type-var]
        waits.append(
            Wait(
                profile,
                sid,
                sessions[sid],
                datetime.fromtimestamp(at, UTC),
                shape.shape,
                None if waited is None else wait_bucket(waited),
            )
        )
    return waits


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _sessions(
    conn: sqlite3.Connection, start: float, sources: Collection[str]
) -> dict[str, str | None]:
    """Session id -> working directory, of the sessions active since ``start``."""
    columns = _columns(conn, "sessions")
    active = (
        "COALESCE(last_activity_at, started_at)" if "last_activity_at" in columns else "started_at"
    )
    where, args = f"{active} >= ?", [start]
    if sources and "source" in columns:
        marks = ",".join("?" * len(sources))
        if "created_source" in columns:  # source is routing state; created_source the origin
            where += f" AND (source IN ({marks}) OR created_source IN ({marks}))"
            args += sorted(sources) * 2
        else:
            where += f" AND source IN ({marks})"
            args += sorted(sources)
    rows = conn.execute(
        f"SELECT id, cwd FROM sessions WHERE {where} ORDER BY {active} DESC LIMIT ?",
        (*args, MAX_SESSIONS),
    )
    return {str(sid): (cwd if isinstance(cwd, str) and cwd else None) for sid, cwd in rows}


def _wait_calls(
    conn: sqlite3.Connection, sessions: Collection[str], start: float
) -> list[tuple[str, float, str, Shape]]:
    """``(session, at, call key, shape)`` of the terminal calls that wait, oldest first,
    each once (compaction copies a call; a compressed session's child carries it too)."""
    live = ""
    if {"active", "compacted"} <= _columns(conn, "messages"):
        live = " AND NOT (active = 0 AND compacted = 0)"  # a rewound turn did not happen
    rows: list[tuple[str, float, Any]] = []
    for chunk in _chunks(sorted(sessions)):
        marks = ",".join("?" * len(chunk))
        for sid, at, raw in conn.execute(
            f"SELECT session_id, timestamp, tool_calls FROM messages "
            f"WHERE session_id IN ({marks}) AND role = 'assistant' "
            f"AND tool_calls IS NOT NULL AND timestamp >= ?{live}",
            (*chunk, start),
        ):
            if (when := _epoch(at)) is not None:
                rows.append((str(sid), when, raw))
    rows.sort(key=lambda r: (r[1], r[0]))
    seen: set[object] = set()
    out = []
    for sid, at, raw in rows:
        for call_id, cmd in _terminal_commands(raw):
            # A copy keeps the original's id and time; ids alone repeat (Hermes derives a
            # missing one from the call's name and arguments). A Codex call's composite id
            # ("call_x|fc_y") is kept whole only on the call.
            key: object = (call_id.split("|", 1)[0], at) if call_id else (sid, at, cmd)
            if key in seen:
                continue
            seen.add(key)
            if (shape := wait_shape(cmd)) is not None:
                out.append((sid, at, call_id, shape))
    return out


def _result_at(conn: sqlite3.Connection, sid: str, at: float, call_id: str) -> float | None:
    """When the call's result came: its own ``tool`` message (Hermes runs one message's calls
    one after another, so the session's next message may be another call's), found from
    ``at`` on along the ``(session_id, timestamp)`` index."""
    row = conn.execute(
        "SELECT timestamp FROM messages WHERE session_id = ? AND timestamp >= ? "
        "AND role = 'tool' AND tool_call_id = ? ORDER BY timestamp LIMIT 1",
        (sid, at, call_id.split("|", 1)[0]),  # Hermes keeps a Codex composite id's first part
    ).fetchone()
    return _epoch(row[0]) if row else None


def _epoch(value: object) -> float | None:
    """A Unix time Hermes stored, if it is one (a number, finite, in a sane range)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return value if math.isfinite(value) and _EARLIEST <= value <= _LATEST else None


def _chunks(items: Sequence[str]) -> Iterator[Sequence[str]]:
    for i in range(0, len(items), _CHUNK):
        yield items[i : i + _CHUNK]


def _terminal_commands(raw: Any) -> list[tuple[str, str]]:
    """``(tool call id, command)`` of the foreground terminal calls in a message's
    ``tool_calls`` (OpenAI's shape, ``{"function": {"name", "arguments"}}``, or flat)."""
    if isinstance(raw, str | bytes) and len(raw) > MAX_TOOL_CALLS:
        return []
    try:
        calls = json.loads(raw) if isinstance(raw, str | bytes) else raw
    except (ValueError, TypeError, RecursionError):
        return []
    if not isinstance(calls, list):
        return []
    out = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else call
        if fn.get("name") != TERMINAL_TOOL:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            if len(args) > MAX_TOOL_CALLS:
                continue
            try:
                args = json.loads(args)
            except (ValueError, RecursionError):
                continue
        if not isinstance(args, dict) or args.get("background"):
            continue  # a background command does not hold the session
        cmd = args.get("command")
        if isinstance(cmd, str) and cmd.strip() and len(cmd) <= MAX_COMMAND:
            # Hermes pairs a result with call_id first, then id.
            out.append((str(call.get("call_id") or call.get("id") or ""), cmd))
    return out


# --- the shape of a command that waits ---------------------------------------------------


def wait_shape(command: str, nested: bool = False) -> Shape | None:
    """The shape of a command that waits, how long its ``sleep`` arguments wait, and whether
    it does nothing else; None if it does not wait. ``nested``: inside ``bash -c``."""
    tokens = _tokens(command)
    if tokens is None:
        return None
    kept = _drop_background_groups(tokens)  # ( … ) &, done &: nothing waits for them
    dropped, tokens = len(kept) < len(tokens), kept
    found: list[tuple[str, timedelta | None, bool]] = []  # shape, slept, inside a loop
    other = False  # something else runs too
    outside = dropped  # something else runs outside any loop (a group sent off is)
    gh_looped = _gh_in_loop(tokens)  # a CI poll, however short its sleeps
    depth = 0  # loops open
    for words, background in _commands(tokens):
        head = words[0]
        if head in _LOOPS:
            depth += 1
            continue
        if head in _ENDS:
            depth = max(0, depth - 1) if head == "done" else depth
            continue
        if background:
            outside = outside or not depth  # cmd &: it runs on, and it is other work
            continue  # the session does not wait for it
        inner = _unwrap(words)
        if inner is None:
            other = True
            outside = outside or not depth
            continue
        name = inner[0].rsplit("/", 1)[-1]
        gh_looped = gh_looped or (bool(depth) and name == "gh")
        if name in _SHELLS and not nested and (script := _script(inner)) is not None:
            sub = wait_shape(script, nested=True)
            if sub is None:
                other = True
                continue
            found.append((sub.shape, sub.slept, depth > 0))
            other = other or not sub.whole
            continue
        if (shape := _simple_shape(name, inner)) is None:
            other = other or name not in _SETUP
            outside = outside or (not depth and name not in _SETUP)
            continue
        found.append((*shape, depth > 0))
    if outside and not gh_looped:  # a loop of short sleeps in a script: a server, not CI
        found = [f for f in found if not (f[2] and f[0] == "sleep" and _short(f[1]))]
    if not found:
        return None
    looped = [s for s, _, loop in found if loop or s.endswith(" loop")]
    if looped:
        main = next((s for s in looped if not s.startswith("sleep")), looped[0])
        return Shape(main if main.endswith(" loop") else f"{main} loop", None, not other)
    main = next((s for s, _, _ in found if s != "sleep"), "sleep")
    sleeps = [t for s, t, _ in found if s == "sleep"]
    slept = None if not sleeps or None in sleeps else sum(sleeps, timedelta(0))  # type: ignore[arg-type]
    return Shape(main, slept, not other)


def _gh_in_loop(tokens: Sequence[str]) -> bool:
    """Whether a loop runs ``gh``: as a command, or inside a word (``[ "$(gh run view 1)" =
    done ]``, ``st=`gh run view 1```)."""
    depth = 0
    for k, tok in enumerate(tokens):
        if _command_start(tokens, k) and tok in _LOOPS:
            depth += 1
        elif _command_start(tokens, k) and tok == "done":
            depth = max(0, depth - 1)
        elif depth and (tok == "gh" or _GH_IN.search(tok)):
            return True
    return False


def _short(slept: timedelta | None) -> bool:
    return slept is not None and slept < SHORT_SLEEP


def _background(tok: str) -> bool:
    """Whether operator ``tok`` sends what is before it to the background (``&``, ``&)``),
    not ``&&``, ``|&`` nor a redirection (``&>``)."""
    t = tok.replace("\n", "")
    return t.startswith("&") and not t.startswith("&&") and "<" not in t and ">" not in t


def _operator(tok: str) -> bool:
    return bool(tok) and all(ch in _PUNCTUATION for ch in tok)


def _command_start(tokens: Sequence[str], k: int) -> bool:
    """Whether ``tokens[k]`` is in command position (a keyword there is one)."""
    if k == 0:
        return True
    before = tokens[k - 1]
    return (_operator(before) and not _redirect(before)) or before in _LEADERS


def _group_start(tokens: Sequence[str], j: int) -> int | None:
    """Where the group, loop or compound command that ``tokens[j]`` closes starts: the
    matching ``(`` (not a ``$(…)`` substitution nor an ``x=(…)`` array), or the keyword
    opening ``done``, ``fi``, ``esac``, ``}``; None if ``tokens[j]`` closes none."""
    closer = tokens[j]
    if _operator(closer) and ")" in closer:
        depth = 0
        for k in range(j, -1, -1):
            if _operator(tokens[k]):
                depth += tokens[k].count(")") - tokens[k].count("(")
                if depth <= 0:
                    before = tokens[k - 1] if k else ""
                    return None if before.endswith(("$", "=")) else k
        return None
    openers = _OPENERS.get(closer)
    if openers is None or not _command_start(tokens, j):
        return None
    depth = 0
    for k in range(j, -1, -1):
        if not _command_start(tokens, k):
            continue
        if tokens[k] == closer:
            depth += 1
        elif tokens[k] in openers:
            depth -= 1
            if depth == 0:
                return k
    return None


def _drop_background_groups(tokens: Sequence[str]) -> list[str]:
    """``tokens`` without the groups and loops sent to the background (``( … ) &``,
    ``done >log 2>&1 &``), each replaced by a separator: what follows still counts."""
    out = list(tokens)
    i = 0
    while i < len(out):
        tok = out[i]
        t = tok.replace("\n", "")
        closer = None
        if _operator(tok) and t.startswith(")") and _background(t.lstrip(")")):
            closer = i  # glued: ")&"
        elif i and _operator(tok) and _background(t):
            j = i - 1
            while j >= 1 and _redirect(out[j - 1]):  # past its redirections
                j -= 2
            closer = j if j >= 0 else None
        start = _group_start(out, closer) if closer is not None else None
        if start is None:
            i += 1
            continue
        out[start : i + 1] = [";\n" if "\n" in tok else ";"]
        i = start + 1
    return out


def _redirect(tok: str) -> bool:
    return bool(tok) and all(ch in _PUNCTUATION for ch in tok) and ("<" in tok or ">" in tok)


def _tokens(command: str) -> list[str] | None:
    command = _FD.sub("", command)  # 2>/dev/null: the 2 is a file descriptor, not a word
    try:
        # Not posix: quotes stay on their words, so a quoted "#" is no comment (_unquote).
        lexer = shlex.shlex(command, posix=False, punctuation_chars=_PUNCTUATION)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""  # shlex would swallow the newline after a comment
        return list(lexer)
    except ValueError:  # unbalanced quotes: no reliable words
        return None


def _commands(tokens: Sequence[str]) -> Iterator[tuple[list[str], bool]]:
    """The simple commands of a tokenized command line, and whether each was sent to the
    background (``&``): split at operators (``;``, ``&&``, ``|``, newlines...), past leading
    keywords and variable assignments, without redirections, comments and heredoc bodies.
    A ``while`` or ``until`` is a command of its own (``[while]``), before its condition."""
    words: list[str] = []
    comment = False
    heredoc: str | None = None  # the delimiter of the heredoc whose body comes next
    skip_body: str | None = None  # in a heredoc body, until this line
    skip = False  # the next token is a redirection's target
    line_start = True
    for tok in [*tokens, "\n"]:
        operator = bool(tok) and all(ch in _PUNCTUATION for ch in tok)
        if skip_body is not None:  # a heredoc body: its lines are text, not commands
            if line_start and tok == skip_body:
                skip_body = None
            line_start = operator and "\n" in tok
            continue
        if operator and ("<" in tok or ">" in tok) and not any(c in tok for c in "\n;|"):
            if tok.startswith("<<") and not tok.startswith("<<<"):  # <<< is a here-string
                heredoc = ""  # its delimiter is the next token
            skip = True
            continue
        if skip:
            skip = False
            if heredoc == "":
                heredoc = _unquote(tok.lstrip("-").lstrip("\\"))  # <<-EOF, <<'EOF'
            continue
        if operator:
            if words:
                yield words, _background(tok)
            words, comment = [], comment and "\n" not in tok
            if "\n" in tok and heredoc:
                skip_body, heredoc = heredoc, None
            line_start = "\n" in tok
            continue
        line_start = False
        if comment:
            continue
        if tok.startswith("#"):
            comment = True  # the rest of the line is a comment
            continue
        if not words and (tok in _LEADERS or _ASSIGNMENT.match(tok)):
            continue
        if not words and tok in _CONDITION_LOOPS:
            yield [tok], False  # the loop; its condition follows as a command
            continue
        words.append(_unquote(tok))


def _unquote(word: str) -> str:
    """A word without the quotes around it (``'sleep 300'`` -> ``sleep 300``)."""
    if len(word) >= 2 and word[0] == word[-1] and word[0] in "'\"":
        return word[1:-1]
    return word


def _unwrap(words: Sequence[str]) -> list[str] | None:
    """The command a wrapper runs (``timeout 600 gh run watch 1`` -> ``gh run watch 1``)."""
    rest = list(words)
    while rest:
        name = rest[0].rsplit("/", 1)[-1]
        if name not in _WRAPPERS:
            break
        valued = _WRAPPERS[name]
        rest.pop(0)
        while rest and (rest[0].startswith("-") or (name == "env" and _ASSIGNMENT.match(rest[0]))):
            if rest.pop(0) in valued and rest:
                rest.pop(0)
        if name == "timeout" and rest:
            rest.pop(0)  # its duration
    return rest or None


def _script(words: Sequence[str]) -> str | None:
    """The script of ``bash -c '<script>'`` (``-lc``, ``-ec``...), if that is the call."""
    for i, word in enumerate(words[1:], 1):
        if word.startswith("--") and word != "--":
            continue  # --login, --noprofile
        if not word.startswith("-") or word == "--":
            return None
        if "c" in word[1:]:
            return words[i + 1] if i + 1 < len(words) else None
    return None


def _simple_shape(name: str, words: Sequence[str]) -> tuple[str, timedelta | None] | None:
    if name == "sleep":
        total = 0.0
        for arg in words[1:]:
            m = _SLEEP_ARG.fullmatch(arg)
            if m is None:
                return "sleep", None
            total += float(m[1]) * _UNITS[m[2]]
        return "sleep", timedelta(seconds=min(total, MAX_SLEEP)) if len(words) > 1 else None
    if name != "gh":
        return None
    sub: list[str] = []
    flags: list[str] = []
    rest = iter(words[1:])
    for word in rest:
        if word in _GH_VALUED:
            next(rest, None)
        elif word.startswith("-"):
            flags.append(word)
        else:
            sub.append(word)
    if sub[:2] == ["run", "watch"]:
        return "gh run watch", None
    watch = any(f == "--watch" or f in ("--watch=true", "--watch=1") for f in flags)
    if sub[:2] == ["pr", "checks"] and watch:
        return "gh pr checks --watch", None
    return None
