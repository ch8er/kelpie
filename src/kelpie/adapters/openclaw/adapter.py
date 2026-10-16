"""OpenClaw platform adapter: read side.

Describes an OpenClaw Gateway's agents to Kelpie from two local sources on the Gateway host:

- **the hooks spool** written by the Kelpie OpenClaw plugin (``plugin/index.js``, which hands
  each hook to ``plugin/kelpie_hook.py``): session starts and ends, tool calls, and, when the
  plugin has conversation access, each run's token usage and end. It gives sessions and spans
  (:func:`kelpie.traces.hooks.spans_from_hook_events`). OpenClaw's own OpenTelemetry export
  (the ``diagnostics-otel`` plugin) is optional; its spans carry no session keys, so tool
  spans it sends are matched to the hook spans by tool call id when they arrive through OTLP
  ingest;
- **the Workboard** (OpenClaw's bundled Kanban plugin), read from its SQLite database, see
  :mod:`kelpie.adapters.openclaw.workboard` for the card and status mapping. Without the
  Workboard there are sessions but no board tasks.

Sessions: one per OpenClaw ``sessionId`` seen in the spool within ``lookback``. An OpenClaw
session is a conversation (``sessionKey`` such as ``agent:main:main``) between resets: every
run in it, heartbeats and cron wakes included, belongs to the same Kelpie session, so a tool
call repeated across heartbeat runs counts as one streak for ``loop_breaker`` (OpenClaw's own
loop detection, off by default, counts per run)::

    started_at      first line of the session
    ended_at        ``session_end`` (reset, new, idle, daily, deleted, shutdown...), unless
                    lines of the same session follow it
    last_heartbeat  latest line of any kind
    exit_reason     the ``session_end`` reason; a failed last run adds "run failed"
    task_id         the Workboard card linked to the session's ``sessionKey``, else
                    ``session:<sessionKey>`` (not a board task)
    owner_id        the card's owner, else the OpenClaw agent id, else ``openclaw``

A session waiting for its next message or heartbeat writes nothing, so a quiet session counts
as ended once its last line is older than a rule's ``session_stale_after``; the Gateway's
default heartbeat runs every 30 minutes.

Spans: a TOOL span per ``tool`` line, SESSION and TASK spans around them, and, from ``llm``
lines (plugin with conversation access), one LLM span per run with the run's tokens
(OpenClaw's ``input`` excludes cache reads and writes; Kelpie's ``input_tokens`` includes
them, as the GenAI conventions do).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.openclaw import workboard
from kelpie.adapters.openclaw.workboard import Card, WorkboardError
from kelpie.core.model import Owner, Session, Snapshot, Span, SpanKind, Task
from kelpie.core.ports import PlatformEvent, UnsupportedAction
from kelpie.traces.hooks import (
    HOOKS,
    SESSION_END,
    SESSION_START,
    SOURCE,
    TOOL,
    HookEvent,
    spans_from_hook_events,
)
from kelpie.traces.semconv import OUTPUT_CHARS_ATTRIBUTE

log = logging.getLogger(__name__)

NAME = "openclaw"
DEFAULT_STATE_DIR = Path("~/.openclaw")
WORKBOARD_DB = Path("plugins/workboard/workboard.sqlite")  # under the state directory
DEFAULT_SPOOL = Path("~/.openclaw/kelpie/spool.jsonl")
DEFAULT_LOOKBACK = timedelta(hours=24)
DEFAULT_OWNER = "openclaw"
NO_TASK_PREFIX = "session:"
FUTURE_SKEW = timedelta(minutes=5)  # spool lines dated further ahead are dropped
SPOOL_TAIL = 65536  # bytes read from the end of the spool to find its newest line
MAX_SPOOL_READ = 64 * 1024 * 1024  # bytes read per spool file and snapshot
MAX_LINE = 16 * 1024  # a longer spool line is skipped (the hook writes well under 2 KB)
MAX_FIELD = 128
MAX_COMMAND = 200  # the hook's redacted command prefix is at most this long

UNSUPPORTED = (
    "the OpenClaw adapter does not carry out this action; "
    "Kelpie alerts a human instead (run the rule in observe or suggest mode)"
)

_EVENT_TYPES = frozenset({"session_start", "tool", "llm", "run_end", "session_end", "stopped"})


class OpenClawError(RuntimeError):
    """The OpenClaw sources cannot be read, or an action failed. ``public_reason`` (when set)
    is the part a chat message may show."""

    def __init__(self, message: str, public_reason: str | None = None) -> None:
        super().__init__(message)
        self.public_reason = public_reason


def _utc(epoch: Any) -> datetime | None:
    if isinstance(epoch, bool) or not isinstance(epoch, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _text(value: Any) -> str | None:
    return (value.strip()[:MAX_FIELD] or None) if isinstance(value, str) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def format_age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@dataclass(frozen=True, slots=True)
class SpoolEvent:
    """One spool line, validated."""

    type: str
    at: datetime
    key: str
    session_id: str
    session_key: str | None = None
    agent: str | None = None
    run_id: str | None = None
    tool: str | None = None
    tool_call_id: str | None = None
    args_hash: str | None = None
    command: str | None = None
    outcome: str | None = None
    duration_ms: int = 0
    output_chars: int | None = None
    provider: str | None = None
    model: str | None = None
    trigger: str | None = None
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    success: bool | None = None
    reason: str | None = None
    version: int = 0  # the hook's spool format version (``v``)


def parse_line(line: str, cutoff: datetime, until: datetime | None = None) -> SpoolEvent | None:
    """One spool line to an event; malformed, foreign, older than ``cutoff`` or later than
    ``until`` (a forged or mis-clocked line would keep a session alive) give ``None``."""
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    if not isinstance(rec, dict) or rec.get("type") not in _EVENT_TYPES:
        return None
    at = _utc(rec.get("at"))
    session = _text(rec.get("session_id"))
    if at is None or at < cutoff or session is None or (until is not None and at > until):
        return None
    outcome = rec.get("outcome")
    success = rec.get("success")
    version = rec.get("v")
    return SpoolEvent(
        type=rec["type"],
        at=at,
        key=hashlib.sha256(line.strip().encode("utf-8", "replace")).hexdigest()[:16],
        session_id=session,
        session_key=_text(rec.get("session_key")),
        agent=_text(rec.get("agent")),
        run_id=_text(rec.get("run_id")),
        tool=_text(rec.get("tool")),
        tool_call_id=_text(rec.get("tool_call_id")),
        args_hash=_text(rec.get("args_hash")),
        command=rec["command"].strip()[:MAX_COMMAND] or None
        if isinstance(rec.get("command"), str)
        else None,
        outcome=outcome if outcome in ("ok", "error") else None,
        duration_ms=_count(rec.get("duration_ms")) or 0,
        output_chars=_count(rec.get("output_chars")),
        provider=_text(rec.get("provider")),
        model=_text(rec.get("model")),
        trigger=_text(rec.get("trigger")),
        input=_count(rec.get("input")) or 0,
        output=_count(rec.get("output")) or 0,
        cache_read=_count(rec.get("cacheRead")) or 0,
        cache_write=_count(rec.get("cacheWrite")) or 0,
        success=success if isinstance(success, bool) else None,
        reason=_text(rec.get("reason")),
        version=version if isinstance(version, int) and not isinstance(version, bool) else 0,
    )


@dataclass(slots=True)
class OpenClawAdapter:
    state_dir: Path = DEFAULT_STATE_DIR
    # The Workboard database; None: no board tasks. Default: <state_dir>/plugins/workboard/...
    workboard_db: Path | None = None
    spool: Path | None = DEFAULT_SPOOL  # None: no hook data, no sessions
    lookback: timedelta = DEFAULT_LOOKBACK
    name: str = NAME
    _warned: set[str] = field(default_factory=set, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir).expanduser()
        if self.workboard_db is not None:
            self.workboard_db = Path(self.workboard_db).expanduser()
        if self.spool is not None:
            self.spool = Path(self.spool).expanduser()

    # --- status --------------------------------------------------------------------------

    def status_detail(self, now: datetime) -> list[str]:
        """Extra lines for ``kelpie status``: the Workboard and how fresh the spool is."""
        db = self.workboard_db
        lines = [f"workboard: {db if db is not None else 'not read'}"]
        if self.spool is None:
            return [*lines, "spool: disabled"]
        if not self.spool.exists():
            return [*lines, f"spool: missing ({self.spool})"]
        last = self.last_spool_event()
        if last is None:
            return [*lines, f"spool: no readable lines ({self.spool})"]
        return [*lines, f"spool: last line {format_age(now - last.at)} ago ({self.spool})"]

    def last_spool_event(self, until: datetime | None = None) -> SpoolEvent | None:
        """The newest readable spool line (read from the file's tail only), ignoring lines
        dated after ``until``."""
        if self.spool is None:
            return None
        try:
            fd = _open_regular(self.spool)
        except OSError:
            return None
        try:
            size = os.fstat(fd).st_size
            os.lseek(fd, max(0, size - SPOOL_TAIL), os.SEEK_SET)
            tail = os.read(fd, SPOOL_TAIL).decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        finally:
            os.close(fd)
        oldest = datetime.min.replace(tzinfo=UTC)
        for line in reversed(tail):
            if (event := parse_line(line, oldest, until)) is not None:
                return event
        return None

    # --- reading -------------------------------------------------------------------------

    def board(self, now: datetime) -> workboard.Board:
        """The Workboard as of ``now`` (empty when not configured or not there yet).
        Raises :class:`OpenClawError` when it is there but cannot be read."""
        db = self.workboard_db
        if db is None:
            return workboard.Board(cards=(), dependencies=())
        if not db.exists():
            self._warn(
                "workboard:missing",
                "openclaw: Workboard database %s not found (is the workboard plugin enabled?); "
                "sessions only, no board tasks",
                db,
            )
            return workboard.Board(cards=(), dependencies=())
        try:
            return workboard.read_board(db, now - self.lookback, now, self.name)
        except WorkboardError as exc:
            raise OpenClawError(str(exc), "the OpenClaw Workboard could not be read") from exc

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now - self.lookback
        events = sorted(self._events(cutoff, now + FUTURE_SKEW), key=lambda e: e.at)
        board = self.board(now)
        sessions = self._sessions(events, board.cards)
        tasks = tuple(c.task for c in board.cards)
        owners = {t.owner_id for t in tasks if t.owner_id} | {s.owner_id for s in sessions}
        return Snapshot(
            now=now,
            tasks=tasks,
            owners=tuple(Owner(id=o, role=o) for o in sorted(owners)),
            dependencies=board.dependencies,
            sessions=sessions,
            spans=self._spans(events, sessions),
        )

    def _sessions(self, events: Sequence[SpoolEvent], cards: Sequence[Card]) -> tuple[Session, ...]:
        by_key = {c.session_key: c.task for c in cards if c.session_key}
        grouped: dict[str, list[SpoolEvent]] = {}
        for e in events:
            grouped.setdefault(e.session_id, []).append(e)
        sessions: list[Session] = []
        for sid, evs in grouped.items():
            last = evs[-1]
            ended = last.type == "session_end"
            key = next((e.session_key for e in reversed(evs) if e.session_key), None)
            card: Task | None = by_key.get(key) if key else None
            agent = next((e.agent for e in reversed(evs) if e.agent), None)
            owner = card.owner_id if card is not None else None
            run_end = next((e for e in reversed(evs) if e.type == "run_end"), None)
            failure = "run failed" if run_end is not None and run_end.success is False else None
            reason = last.reason if ended else None
            sessions.append(
                Session(
                    id=sid,
                    task_id=card.id if card is not None else f"{NO_TASK_PREFIX}{key or sid}",
                    owner_id=owner or agent or DEFAULT_OWNER,
                    started_at=evs[0].at,
                    ended_at=last.at if ended else None,
                    exit_reason=" ".join(x for x in (reason, failure) if x)[:300] or None,
                    last_heartbeat=last.at,
                    source=self.name,
                )
            )
        return tuple(sessions)

    def _spans(self, events: Sequence[SpoolEvent], sessions: Sequence[Session]) -> tuple[Span, ...]:
        task_of = {s.id: s.task_id for s in sessions if not s.task_id.startswith(NO_TASK_PREFIX)}
        hook_events: list[HookEvent] = []
        for e in events:
            kind = {"tool": TOOL, "session_start": SESSION_START, "session_end": SESSION_END}.get(
                e.type
            )
            if kind is None:
                continue
            attributes: dict[str, Any] = {}
            if e.command is not None:
                attributes["command"] = e.command
            if e.output_chars is not None:
                attributes[OUTPUT_CHARS_ATTRIBUTE] = e.output_chars
            hook_events.append(
                HookEvent(
                    kind=kind,
                    at=e.at,
                    key=e.key,
                    session_id=e.session_id,
                    task_id=task_of.get(e.session_id),
                    tool=e.tool,
                    args_hash=e.args_hash,
                    duration=timedelta(milliseconds=e.duration_ms),
                    outcome=e.outcome,
                    tool_call_id=e.tool_call_id,
                    attributes=attributes,
                )
            )
        spans = spans_from_hook_events(hook_events, prefix=self.name)
        return spans + self._llm_spans(events, spans)

    def _llm_spans(self, events: Sequence[SpoolEvent], spans: Sequence[Span]) -> tuple[Span, ...]:
        """One LLM span per ``llm`` line (a run's token usage), under its session's span."""
        parents = {s.session_id: s for s in spans if s.kind is SpanKind.SESSION and s.session_id}
        out: list[Span] = []
        for e in events:
            if e.type != "llm":
                continue
            parent = parents.get(e.session_id)
            attributes: dict[str, Any] = {"gen_ai.operation.name": "chat", SOURCE: HOOKS}
            if e.provider:
                attributes["gen_ai.provider.name"] = e.provider
            if e.trigger:
                attributes["openclaw.trigger"] = e.trigger
            out.append(
                Span(
                    trace_id=parent.trace_id if parent is not None else e.session_id,
                    span_id=f"{self.name}-llm-{e.key}",
                    parent_id=parent.span_id if parent is not None else None,
                    name=f"chat {e.model}" if e.model else "chat",
                    kind=SpanKind.LLM,
                    start=e.at,
                    end=e.at,
                    task_id=parent.task_id if parent is not None else None,
                    session_id=e.session_id,
                    model=e.model,
                    input_tokens=e.input + e.cache_read + e.cache_write,
                    output_tokens=e.output,
                    cached_tokens=e.cache_read,
                    outcome="ok",
                    attributes=attributes,
                )
            )
        return tuple(out)

    def _spool_lines(self, cutoff: datetime) -> Iterator[str]:
        if self.spool is None:
            return
        rotated = self.spool.with_name(self.spool.name + ".1")
        for path in (rotated, self.spool):
            try:
                if path is rotated and path.stat().st_mtime < cutoff.timestamp():
                    continue  # nothing in it can be inside the lookback
                yield from _read_lines(path, MAX_SPOOL_READ)
            except OSError:
                continue

    def _events(self, cutoff: datetime, until: datetime | None = None) -> Iterator[SpoolEvent]:
        for line in self._spool_lines(cutoff):
            if (event := parse_line(line, cutoff, until)) is not None:
                yield event

    def _warn(self, key: str, message: str, *args: object) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    # --- PlatformAdapter -----------------------------------------------------------------

    def list_tasks(self) -> Sequence[Task]:
        return self.snapshot(datetime.now(UTC)).tasks

    def get_task(self, task_id: str) -> Task | None:
        return next((t for t in self.list_tasks() if t.id == task_id), None)

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return ()

    def requeue(self, task_id: str, reason: str) -> None:
        raise UnsupportedAction(UNSUPPORTED)

    def unblock(self, task_id: str, reason: str) -> None:
        raise UnsupportedAction(UNSUPPORTED)

    def comment(self, task_id: str, body: str) -> None:
        raise UnsupportedAction(UNSUPPORTED)

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        raise UnsupportedAction(UNSUPPORTED)


def workboard_path(state_dir: Path) -> Path:
    """Where the Workboard plugin keeps its database under an OpenClaw state directory."""
    return Path(state_dir) / WORKBOARD_DB


def _open_regular(path: Path) -> int:
    """A descriptor for a regular file at ``path``, opened without following a symlink or
    blocking on a FIFO (agents can write next to the spool). Raises OSError otherwise."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
    except OSError:
        os.close(fd)
        raise
    return fd


def _read_lines(path: Path, limit: int) -> Iterator[str]:
    """Lines of a regular file, reading at most ``limit`` bytes; overlong lines skipped."""
    fd = _open_regular(path)
    with os.fdopen(fd, "rb") as f:
        left = limit
        while left > 0:
            line = f.readline(min(MAX_LINE + 1, left))
            if not line:
                return
            left -= len(line)
            if len(line) > MAX_LINE:
                while line and not line.endswith(b"\n") and left > 0:  # skip its rest
                    line = f.readline(min(MAX_LINE + 1, left))
                    left -= len(line)
                continue
            yield line.decode("utf-8", "replace")
