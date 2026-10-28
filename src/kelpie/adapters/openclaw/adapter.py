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
    exit_reason     the ``session_end`` reason; a failed last run adds "run failed"; "stopped
                    by kelpie" once the plugin refused something for a stop, until the stop
                    is lifted; "stop not enforced" (see :meth:`OpenClawAdapter.stop`)
    task_id         the Workboard card linked to the session's ``sessionKey`` (several cards
                    can name one key, e.g. the main chat session: a running card wins, then
                    any open card, then a finished one, the most recently updated first),
                    else ``session:<sessionKey>`` (not a board task)
    owner_id        the card's owner, else the OpenClaw agent id, else ``openclaw``

A session waiting for its next message or heartbeat writes nothing, so a quiet session counts
as ended once its last line is older than a rule's ``session_stale_after``; the Gateway's
default heartbeat runs every 30 minutes.

Spans: a TOOL span per ``tool`` line, SESSION and TASK spans around them, and, from ``llm``
lines (plugin with conversation access), one LLM span per run with the run's tokens
(OpenClaw's ``input`` excludes cache reads and writes; Kelpie's ``input_tokens`` includes
them, as the GenAI conventions do). ``token_spans = False`` leaves the LLM spans out: OpenClaw's
own OTLP export (``diagnostics-otel``) reports the same tokens without a session, and the
daily report would count a run twice if both reached it.

Actions: ``stop`` (``SupportsStop``, see :mod:`kelpie.adapters.openclaw.stop`). Task actions
raise ``UnsupportedAction`` (the engine alerts a human).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters import _spool
from kelpie.adapters.openclaw import workboard
from kelpie.adapters.openclaw.plugin.kelpie_hook import (
    peer_key_path,
    read_peer_key,
    safe_session_key,
)
from kelpie.adapters.openclaw.stop import (
    CLI_TIMEOUT,
    DEFAULT_CLI,
    DEFAULT_STOP_DIR,
    DEFAULT_STOP_TTL,
    SESSION_ID,
    OpenClawCliError,
    abort_session,
    read_marker,
    write_marker,
)
from kelpie.adapters.openclaw.workboard import Card, WorkboardError
from kelpie.core.actions import ActionResult
from kelpie.core.model import Owner, Session, Snapshot, Span, SpanKind, Status, Task
from kelpie.core.ports import PlatformEvent, UnsupportedAction
from kelpie.traces.command import redact_command
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
MAX_KEY = 256  # a session key as the hook reduced it (kept keys are at most 128)
BOARD_GRACE = timedelta(minutes=5)  # the last board read stands in this long when a read fails
MAX_COMMAND = 200  # the hook's redacted command prefix is at most this long
MAX_DURATION_MS = 7 * 24 * 3600 * 1000  # a tool call's duration is cut to a week

UNSUPPORTED = (
    "the OpenClaw adapter does not carry out this action; "
    "Kelpie alerts a human instead (run the rule in observe or suggest mode)"
)

_EVENT_TYPES = frozenset(
    {"session_start", "tool", "llm", "run_end", "session_end", "stopped", "resumed"}
)
# A stop not enforced: the session still records tool calls this long after its marker.
STOP_GRACE = timedelta(minutes=2)
STOP_VERSION = 2  # the first plugin (index.js) version that enforces stop markers
STOP_LINES = ("stopped", "resumed")
_INTERNAL = re.compile(r"(^|:)internal-session-effects(:|$)")  # OpenClaw's own background lanes


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
    plugin: int = 0  # the version of the plugin (index.js) the Gateway had loaded
    gate: bool = False  # the plugin's before_agent_run had run (conversation access)


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
        session_key=(rec["session_key"].strip()[:MAX_KEY] or None)
        if isinstance(rec.get("session_key"), str)
        else None,
        agent=_text(rec.get("agent")),
        run_id=_text(rec.get("run_id")),
        tool=_text(rec.get("tool")),
        tool_call_id=_text(rec.get("tool_call_id")),
        args_hash=_text(rec.get("args_hash")),
        # Redacted again: agents can write spool lines, and this text reaches alerts.
        command=redact_command(rec["command"][:MAX_COMMAND])
        if isinstance(rec.get("command"), str)
        else None,
        outcome=outcome if outcome in ("ok", "error") else None,
        # Bounded: a forged huge duration must not overflow date arithmetic.
        duration_ms=min(_count(rec.get("duration_ms")) or 0, MAX_DURATION_MS),
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
        plugin=_count(rec.get("plugin")) or 0,
        gate=rec.get("gate") is True,
    )


@dataclass(slots=True)
class OpenClawAdapter:
    state_dir: Path = DEFAULT_STATE_DIR
    # The Workboard database; None: no board tasks (the config defaults it under state_dir).
    workboard_db: Path | None = None
    spool: Path | None = DEFAULT_SPOOL  # None: no hook data, no sessions
    lookback: timedelta = DEFAULT_LOOKBACK
    token_spans: bool = True  # LLM spans from ``llm`` lines (off when OTLP reports tokens)
    stop_dir: Path | None = DEFAULT_STOP_DIR  # None: stop is refused (alerts instead)
    stop_ttl: timedelta = timedelta(seconds=DEFAULT_STOP_TTL)  # must match the plugin's stopTtl
    cli: str | None = DEFAULT_CLI  # runs ``sessions.abort``; None: markers only
    cli_timeout: float = CLI_TIMEOUT
    name: str = NAME
    _warned: set[str] = field(default_factory=set, init=False, repr=False, compare=False)
    _board_error: str | None = field(default=None, init=False, repr=False, compare=False)
    _last_board: tuple[datetime, workboard.Board] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.state_dir = Path(self.state_dir).expanduser()
        if self.stop_dir is not None:
            self.stop_dir = Path(self.stop_dir).expanduser()
        if self.workboard_db is not None:
            self.workboard_db = Path(self.workboard_db).expanduser()
        if self.spool is not None:
            self.spool = Path(self.spool).expanduser()

    # --- status --------------------------------------------------------------------------

    def check(self) -> str | None:
        """``None`` if the Workboard can be read (or is not used), else why not: the last
        snapshot's failure, else a look at its schema. A Workboard that does not exist yet is
        not a problem: sessions only."""
        if self._board_error is not None:
            return self._board_error
        if self._last_board is None:  # no snapshot yet (kelpie status): read it now
            try:
                self.board(datetime.now(UTC))
            except OpenClawError as exc:
                return str(exc)
            return None
        db = self.workboard_db
        if db is None or not db.exists():
            return None
        try:
            conn = workboard.connect(db)
        except WorkboardError as exc:
            return str(exc)
        try:
            missing = workboard.missing_columns(conn)
        except sqlite3.Error as exc:
            return f"cannot read Workboard database {db}: {exc}"
        finally:
            conn.close()
        if missing:
            return f"Workboard database {db} lacks {', '.join(missing)}"
        return None

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
        tail = _spool.tail_lines(self.spool, SPOOL_TAIL)
        if tail is None:
            return None
        oldest = datetime.min.replace(tzinfo=UTC)
        for line in reversed(tail):
            if (event := parse_line(line, oldest, until)) is not None:
                return event
        return None

    # --- reading -------------------------------------------------------------------------

    def board(self, now: datetime) -> workboard.Board:
        """The Workboard as of ``now`` (empty when not configured or not there yet). Raises
        :class:`OpenClawError` when it is there but cannot be read (see :meth:`snapshot`)."""
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
            board = workboard.read_board(db, now - self.lookback, now, self.name, self._peer_key())
        except WorkboardError as exc:
            raise OpenClawError(str(exc), "the OpenClaw Workboard could not be read") from exc
        for status in board.unknown:  # agents control the text: one warning per status
            self._warn(
                f"status:{status}",
                "openclaw: Workboard cards with unknown status %r are left out (warned once)",
                status,
            )
        return board

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now - self.lookback
        events = sorted(self._events(cutoff, now + FUTURE_SKEW), key=lambda e: e.at)
        board = self._board_or_last(now)
        sessions = self._sessions(events, board.cards, now, self._markers(events))
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

    def _board_or_last(self, now: datetime) -> workboard.Board:
        """The board, or when it cannot be read: the last one read within ``BOARD_GRACE``
        (a failure for a tick or two does not make every card vanish), else none. Sessions
        matter more than the board (they are what a stop acts on), and agents can write the
        file: a broken board must not blind Kelpie to loops."""
        try:
            board = self.board(now)
        except OpenClawError as exc:
            self._board_error = str(exc)
            self._warn(f"board:{exc}", "openclaw: %s; sessions only until it can be read", exc)
            last = self._last_board
            if last is not None and now - last[0] <= BOARD_GRACE:
                return last[1]
            return workboard.Board(cards=(), dependencies=())
        self._board_error = None
        self._last_board = (now, board)
        return board

    def _markers(self, events: Sequence[SpoolEvent]) -> dict[str, datetime]:
        """Stop marker times of sessions in the spool (``{}`` without a stop directory). Each
        session's marker is opened by name: agents can fill the directory, so it is never
        listed."""
        if self.stop_dir is None:
            return {}
        found: dict[str, datetime] = {}
        for sid in {e.session_id for e in events}:
            if (at := read_marker(self.stop_dir, sid)) is not None:
                found[sid] = at
        return found

    def _sessions(
        self,
        events: Sequence[SpoolEvent],
        cards: Sequence[Card],
        now: datetime,
        markers: dict[str, datetime] | None = None,
    ) -> tuple[Session, ...]:
        secret = self._peer_key()
        by_key: dict[str, Task] = {}
        unassigned: list[tuple[str, Task]] = []  # worker keys the Gateway puts an agent in front of
        for c in cards:  # newest first: on a tie the most recently updated card wins
            worker = c.worker_key()
            if worker is not None and worker.startswith("subagent:"):
                unassigned.append((worker, c.task))
                worker = None
            for k in (c.session_key, safe_session_key(worker, secret)):
                if k and _rank(c.task) > _rank(by_key.get(k)):
                    by_key[k] = c.task
        grouped: dict[str, list[SpoolEvent]] = {}
        for e in events:
            grouped.setdefault(e.session_id, []).append(e)
        sessions: list[Session] = []
        for sid, evs in grouped.items():
            # What the plugin did about a stop says nothing about whether the session lives.
            real = [e for e in evs if e.type not in STOP_LINES] or evs
            last = real[-1]
            ended = last.type == "session_end"
            key = next((e.session_key for e in reversed(evs) if e.session_key), None)
            agent = next((e.agent for e in reversed(evs) if e.agent), None)
            card = _card_for(key, agent, by_key, unassigned, secret)
            owner = card.owner_id if card is not None else None
            run_end = next((e for e in reversed(evs) if e.type == "run_end"), None)
            failure = "run failed" if run_end is not None and run_end.success is False else None
            stop = next((e for e in reversed(evs) if e.type in STOP_LINES), None)
            marked = (markers or {}).get(sid)
            held = marked is not None and now - marked <= self.stop_ttl
            if stop is not None and stop.type == "stopped" and held:
                failure = " ".join(x for x in ("stopped by kelpie", failure) if x)
            if marked is not None and not ended and _ignored_stop(evs, marked, self.stop_ttl):
                self._warn(
                    f"unenforced:{sid}",
                    "openclaw: session %s still calls tools %s after its stop; is the Kelpie "
                    "plugin current and enabled, and does its stopDir match stop_dir?",
                    sid,
                    STOP_GRACE,
                )
                failure = " ".join(x for x in ("stop not enforced", failure) if x)
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
        if not self.token_spans:
            return spans
        return spans + self._llm_spans(events, spans, task_of)

    def _llm_spans(
        self, events: Sequence[SpoolEvent], spans: Sequence[Span], task_of: Mapping[str, str]
    ) -> tuple[Span, ...]:
        """One LLM span per ``llm`` line (a run's token usage), under its session's span when
        there is one (a session whose only lines in ``lookback`` are runs without tool calls
        has none: its LLM spans still carry the session and its task)."""
        parents = {s.session_id: s for s in spans if s.kind is SpanKind.SESSION and s.session_id}
        out: list[Span] = []
        for e in events:
            if e.type != "llm":
                continue
            parent = parents.get(e.session_id)
            task = task_of.get(e.session_id)
            attributes: dict[str, Any] = {"gen_ai.operation.name": "chat", SOURCE: HOOKS}
            if e.provider:
                attributes["gen_ai.provider.name"] = e.provider
            if e.trigger:
                attributes["openclaw.trigger"] = e.trigger
            out.append(
                Span(
                    trace_id=parent.trace_id
                    if parent is not None
                    else (f"{self.name}-task-{task}" if task else e.session_id),
                    span_id=f"{self.name}-llm-{e.key}",
                    parent_id=parent.span_id if parent is not None else None,
                    name=f"chat {e.model}" if e.model else "chat",
                    kind=SpanKind.LLM,
                    start=e.at,
                    end=e.at,
                    task_id=task,
                    session_id=e.session_id,
                    model=e.model,
                    input_tokens=e.input + e.cache_read + e.cache_write,
                    output_tokens=e.output,
                    cached_tokens=e.cache_read,
                    cache_write_tokens=e.cache_write,
                    outcome="ok",
                    attributes=attributes,
                )
            )
        return tuple(out)

    def _spool_lines(self, cutoff: datetime) -> Iterator[str]:
        return _spool.spool_lines(self.spool, cutoff, MAX_SPOOL_READ, MAX_LINE)

    def _events(self, cutoff: datetime, until: datetime | None = None) -> Iterator[SpoolEvent]:
        for line in self._spool_lines(cutoff):
            if (event := parse_line(line, cutoff, until)) is not None:
                yield event

    def _peer_key(self) -> bytes | None:
        """The hook's key for session key pseudonyms (next to the spool), if Kelpie can read
        it; never created here."""
        return None if self.spool is None else read_peer_key(peer_key_path(self.spool))

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

    def stop(self, session_id: str, reason: str) -> str | ActionResult:
        """Stop a session: a stop marker the plugin enforces, then an abort of its run in
        flight through the Gateway (see :mod:`kelpie.adapters.openclaw.stop`).

        Idempotent: an ended session is left alone (``NOTHING_TO_DO``); a second stop rewrites
        the marker. Returns what the stop means here, for the human told about it. Raises
        :class:`UnsupportedAction` when stop is turned off, the session is not in the spool or
        its plugin predates stop support; :class:`OpenClawError` for an id that is not an
        OpenClaw session id or a marker that cannot be written.
        """
        if not SESSION_ID.fullmatch(session_id):
            raise OpenClawError(f"not an OpenClaw session id: {session_id!r}")
        if self.stop_dir is None:
            raise UnsupportedAction("stop is turned off ([adapters.openclaw] stop_dir = false)")
        now = datetime.now(UTC)
        events = sorted(self._events(now - self.lookback, now + FUTURE_SKEW), key=lambda e: e.at)
        own = [e for e in events if e.session_id == session_id]
        if not own:
            raise UnsupportedAction(
                f"session {session_id} is not in the hooks spool, so the Kelpie plugin cannot "
                "stop it"
            )
        real = [e for e in own if e.type not in STOP_LINES] or own
        if real[-1].type == "session_end":
            log.info("openclaw: session %s has ended; nothing to stop", session_id)
            return ActionResult.NOTHING_TO_DO
        # The plugin is per Gateway: its newest line says what the Gateway runs now (a session
        # idle since an upgrade still has older lines of its own).
        if max(own[-1].plugin, events[-1].plugin) < STOP_VERSION:
            raise UnsupportedAction(
                f"session {session_id} runs a Kelpie plugin without stop support (or one copied "
                "but not yet loaded); copy the current plugin and restart the Gateway"
            )
        # With conversation access the plugin also refuses scheduled runs and lets the
        # operator's message lift the stop. It says so in its lines ("gate") once a run has
        # started; the usage and end of runs are only recorded with access too.
        access = any(e.type in ("llm", "run_end") or e.gate for e in events)
        if access:
            held = (
                "every tool call and every run but the operator's message is refused until the "
                "operator messages it, the session is reset or the stop expires"
            )
        else:
            held = (
                "every tool call is refused until the session is reset or the stop expires (the "
                "plugin has no conversation access: scheduled runs still start, and a message "
                "does not lift the stop)"
            )
        problem: OSError | None = None
        try:
            # The key the operator's message comes under: not one of OpenClaw's background
            # lanes, which can share the session id.
            keys = [e.session_key for e in reversed(own) if e.session_key]
            key = next((k for k in keys if not _INTERNAL.search(k)), keys[0] if keys else None)
            write_marker(self.stop_dir, session_id, reason, now.timestamp(), key)
            log.info("openclaw: stop requested for session %s", session_id)
        except OSError as exc:  # an agent can sabotage the marker: still end the run below
            problem = exc
        aborted = self._abort(session_id, own, events)
        if problem is not None:
            raise OpenClawError(
                f"cannot write the stop marker for {session_id}: {problem} ({aborted})",
                f"the stop marker could not be written ({aborted})",
            ) from problem
        if aborted == "its current run was aborted":
            return f"{aborted}; {held}"
        return f"{held}; {aborted}" if aborted else held

    def _abort(
        self, session_id: str, own: Sequence[SpoolEvent], events: Sequence[SpoolEvent]
    ) -> str:
        """Abort the session's run in flight; what happened, in a few words ("" when nothing
        was running). The session key is not used when a newer session has taken it (a
        stopped id after ``/new`` or a rollover): only the session's own run id is."""
        key = next((e.session_key for e in reversed(own) if e.session_key), None)
        run = next((e.run_id for e in reversed(own) if e.run_id), None)
        if key is not None and any(
            e.session_key == key and e.session_id != session_id and e.at > own[-1].at
            for e in events
        ):
            key = None
        if self.cli is None or (key is None and run is None):
            why = "cli = false" if self.cli is None else "no current session key or run"
            return f"its current run was not aborted ({why})"
        try:
            status = abort_session(self.cli, key, run, timeout=self.cli_timeout)
        except OpenClawCliError as exc:
            log.warning("openclaw: cannot abort the run of session %s: %s", session_id, exc)
            return f"its current run could not be aborted ({exc.public_reason})"
        log.info(
            "openclaw: sessions.abort for session %s (key %s, run %s): %s",
            session_id,
            key,
            run,
            status,
        )
        return "its current run was aborted" if status == "aborted" else ""

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


def _card_for(
    key: str | None,
    agent: str | None,
    by_key: Mapping[str, Task],
    unassigned: Sequence[tuple[str, Task]],
    secret: bytes | None,
) -> Task | None:
    """The card a session key works on: the card that names the key or whose worker it is,
    else, as Workboard matches its workers, an unassigned card whose ``subagent:...`` key
    the session's key is with its agent in front (the Gateway puts its default agent there).
    Keys are compared as the hook reduced them."""
    if key is None:
        return None
    if (card := by_key.get(key)) is not None:
        return card
    best: Task | None = None
    for worker, task in unassigned:
        full = safe_session_key(f"agent:{agent}:{worker}", secret) if agent else None
        if (key == full or key.endswith(":" + worker)) and _rank(task) > _rank(best):
            best = task
    return best


def _rank(task: Task | None) -> int:
    """Which of the cards naming one session key the session works on: a running card, then
    an open one, then a finished one."""
    if task is None:
        return -1
    if task.status is Status.IN_PROGRESS:
        return 2
    return 0 if task.status.is_terminal else 1


def _ignored_stop(events: Sequence[SpoolEvent], marked: datetime, ttl: timedelta) -> bool:
    """The session went on calling tools well after its stop marker, and the plugin never
    refused anything: the stop is not enforced."""
    after = [e for e in events if e.at >= marked]
    if any(e.type in STOP_LINES for e in after):
        return False
    # A call counts by its start (a long call begun before the stop is not a miss), and only
    # while the marker holds for the plugin (it ignores markers older than its TTL).
    expires = marked + ttl
    return any(
        e.type == "tool"
        and marked + STOP_GRACE < e.at - timedelta(milliseconds=e.duration_ms) < expires
        for e in after
    )


def workboard_path(state_dir: Path) -> Path:
    """Where the Workboard plugin keeps its database under an OpenClaw state directory."""
    return Path(state_dir) / WORKBOARD_DB
