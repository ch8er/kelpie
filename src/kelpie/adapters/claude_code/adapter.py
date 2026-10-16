"""Claude Code platform adapter: read side.

Describes a farm of Claude Code sessions to Kelpie from two local sources on the agent host:

- **task lists**: ``<tasks_dir>/<list>/<id>.json`` (``tasks_dir`` is ``~/.claude/tasks``),
  the files behind Claude Code's ``TaskCreate``/``TaskUpdate`` tools. Sessions share a list
  when they run with the same ``CLAUDE_CODE_TASK_LIST_ID`` (an agent team shares its team's
  list); only the lists named in ``task_lists`` are read, never written;
- **the hooks spool** written by :mod:`kelpie.adapters.claude_code.hook`: one line per session
  start, prompt, tool call, end of turn and session end, which gives sessions, their liveness
  and hook tool spans (:func:`kelpie.traces.hooks.spans_from_hook_events`). Claude Code's own
  OpenTelemetry spans (``claude_code.*``) arrive through OTLP ingest and link to the same
  sessions by ``session.id``; a tool call reported both ways is kept once (by
  ``tool_use_id``).

Task ids are ``<list>/<id>`` (``farm/3``): ids are only unique within a list.

Status mapping (task file ``status`` -> :class:`~kelpie.core.model.Status`)::

    pending, every blockedBy task completed (or gone)  -> READY
    pending, waiting on an open blockedBy task          -> BACKLOG
    in_progress                                         -> IN_PROGRESS
    completed                                           -> DONE
    anything else                                       -> (skipped, logged)

``owner`` is the owner and its role (agents claim tasks by name). ``blockedBy`` gives
:class:`~kelpie.core.model.Dependency` entries within the list. Task files carry no
timestamps, so ``status_since`` is the time of the latest ``TaskUpdate`` that set the
task's status as seen in the spool, else the file's modification time (which any edit moves).
Completed tasks whose file is older than ``lookback`` are left out. Claude Code has no
blocked or review status, so tasks never carry a :class:`~kelpie.core.model.Block`.

Sessions: one per Claude Code ``session_id`` seen in the spool within ``lookback``
(subagent calls count for their parent session)::

    started_at      first event (``SessionStart`` when present)
    ended_at        ``SessionEnd``, unless events of the same session follow it (resumed);
                    else the last event once the session's process is gone
    last_heartbeat  latest event of any kind; now while its process runs
    exit_reason     ``SessionEnd`` reason or "process gone", or the error kind of a failed
                    last turn
    task_id         ``KELPIE_TASK_ID`` of the session, else ``<list>/<id>`` of the task it
                    last set ``in_progress``, else ``session:<session_id>`` (no such task)
    final_status    DONE when the session itself set its task ``completed`` and ended;
                    else None (the session declared nothing)
    owner_id        ``KELPIE_AGENT``, else the owner set on the task the session claimed
                    (in the claiming update or a later one), else the ``--agent`` type,
                    else ``claude-code``

A ``TaskUpdate`` counts only on a watched list: the hook records the session's list
(``CLAUDE_CODE_TASK_LIST_ID``, else its own session id), so private tasks stay private.

Liveness: an interactive session waiting at its prompt fires no hooks, so its last line ages
like a crashed session's. For a session without ``SessionEnd`` the Claude Code process the
hook recorded is checked (:mod:`kelpie.adapters.claude_code.process`: Linux only, and only
when host, user, PID namespace and boot match Kelpie's own): running means alive
(``last_heartbeat`` = now), gone means ended at its last line (``exit_reason = "process
gone"``); when it cannot be told, the spool alone decides, as before. Headless sessions
(``claude -p``) end with ``SessionEnd``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from kelpie.adapters.claude_code import hook
from kelpie.adapters.claude_code.process import process_state
from kelpie.core.actions import ActionKind, ActionResult
from kelpie.core.model import (
    Dependency,
    Owner,
    Session,
    Snapshot,
    Span,
    Status,
    Task,
)
from kelpie.core.ports import PlatformEvent, UnsupportedAction
from kelpie.traces.hooks import SESSION_END, SESSION_START, TOOL, HookEvent, spans_from_hook_events
from kelpie.traces.semconv import OUTPUT_CHARS_ATTRIBUTE

log = logging.getLogger(__name__)

NAME = "claude-code"
DEFAULT_TASKS_DIR = Path("~/.claude/tasks")
DEFAULT_SPOOL = Path("~/.claude/kelpie/spool.jsonl")
DEFAULT_STOP_DIR = Path(hook.DEFAULT_STOP_DIR)
DEFAULT_LOOKBACK = timedelta(hours=24)
DEFAULT_OWNER = "claude-code"
NO_TASK_PREFIX = "session:"
MAX_TASK_FILE = 1024 * 1024  # bytes; a larger task file is skipped
MAX_TASKS_PER_LIST = 5000
MAX_BLOCKED_BY = 100  # blockedBy entries kept per task
MAX_FIELD = 128  # characters kept of an owner name
MAX_TITLE = 200
FUTURE_SKEW = timedelta(minutes=5)  # spool lines dated further ahead are dropped
# A "process gone" verdict is not trusted for a session that wrote a line this recently.
PROCESS_GRACE = timedelta(minutes=5)
WATCH_ENDED = timedelta(hours=1)  # a watched session found gone is reported this long
WATCH_UNKNOWN = 3  # snapshots in a row a watched session's process cannot be told, then dropped
# A stop not enforced: the session still records tool calls this long after its marker.
STOP_GRACE = timedelta(minutes=2)
MAX_MARKER = 4096
SPOOL_TAIL = 65536  # bytes read from the end of the spool to find its newest line
MAX_SPOOL_READ = 64 * 1024 * 1024  # bytes read per spool file and snapshot
MAX_LINE = 16 * 1024  # a longer spool line is skipped (the hook writes well under 2 KB)

UNSUPPORTED = (
    "Claude Code has no operator command for this action; "
    "Kelpie alerts a human instead (run the rule in observe or suggest mode)"
)

STATUS_MAP: Mapping[str, Status] = {
    "in_progress": Status.IN_PROGRESS,
    "completed": Status.DONE,
}

# Claude Code turns a list id into a directory name this way (``[^a-zA-Z0-9_-]`` -> ``-``).
_UNSAFE_LIST_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_TASK_FILE = re.compile(r"^[A-Za-z0-9_-]{1,128}\.json$")
_EVENT_TYPES = frozenset({"session_start", "prompt", "tool", "turn_end", "session_end", "stopped"})


class ClaudeCodeError(RuntimeError):
    """The Claude Code task lists cannot be read, or a stop failed. ``public_reason`` (when
    set) is the part a chat message may show."""

    def __init__(self, message: str, public_reason: str | None = None) -> None:
        super().__init__(message)
        self.public_reason = public_reason


def list_dir_name(task_list: str) -> str:
    """The directory Claude Code keeps a task list in, under ``tasks_dir``."""
    return _UNSAFE_LIST_CHARS.sub("-", task_list)


def _utc(epoch: Any) -> datetime | None:
    if isinstance(epoch, bool) or not isinstance(epoch, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


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
    agent_id: str | None = None
    task_list: str | None = None
    kelpie_task: str | None = None
    agent: str | None = None
    agent_type: str | None = None
    reason: str | None = None
    error: str | None = None
    tool: str | None = None
    tool_call_id: str | None = None
    args_hash: str | None = None
    command: str | None = None
    outcome: str | None = None
    duration_ms: int = 0
    output_chars: int | None = None
    task: str | None = None
    task_status: str | None = None
    task_owner: str | None = None
    version: int = 0  # the hook's spool format version (``v``); 0 before stop support
    pid: int | None = None  # the Claude Code process the hook ran under
    pid_start: str | None = None  # its start time (Linux clock ticks since boot)
    host: str | None = None  # the host the hook ran on
    uid: int | None = None  # the user it ran as
    pidns: str | None = None  # its PID namespace (/proc/self/ns/pid)
    boot: str | None = None  # the kernel's boot id

    @property
    def recorded(self) -> dict[str, Any]:
        """What the liveness check compares (:func:`process_state`)."""
        return {
            "pid_start": self.pid_start,
            "host": self.host,
            "uid": self.uid,
            "pidns": self.pidns,
            "boot": self.boot,
        }


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
    duration = rec.get("duration_ms")
    chars = rec.get("output_chars")
    outcome = rec.get("outcome")
    return SpoolEvent(
        type=rec["type"],
        at=at,
        key=hashlib.sha256(line.strip().encode("utf-8", "replace")).hexdigest()[:16],
        session_id=session,
        agent_id=_text(rec.get("agent_id")),
        task_list=_text(rec.get("task_list")),
        kelpie_task=_text(rec.get("kelpie_task")),
        agent=_text(rec.get("agent")),
        agent_type=_text(rec.get("agent_type")),
        reason=_text(rec.get("reason")),
        error=_text(rec.get("error")),
        tool=_text(rec.get("tool")),
        tool_call_id=_text(rec.get("tool_call_id")),
        args_hash=_text(rec.get("args_hash")),
        command=_text(rec.get("command")),
        outcome=outcome if outcome in ("ok", "error") else None,
        duration_ms=duration
        if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0
        else 0,
        output_chars=chars
        if isinstance(chars, int) and not isinstance(chars, bool) and chars >= 0
        else None,
        task=_text(rec.get("task")),
        task_status=_text(rec.get("task_status")),
        task_owner=_text(rec.get("task_owner")),
        version=rec["v"] if isinstance(rec.get("v"), int) and not isinstance(rec["v"], bool) else 0,
        pid=rec["pid"]
        if isinstance(rec.get("pid"), int) and not isinstance(rec["pid"], bool) and rec["pid"] > 1
        else None,
        pid_start=_text(rec.get("pid_start")),
        host=_text(rec.get("host")),
        uid=rec["uid"]
        if isinstance(rec.get("uid"), int) and not isinstance(rec["uid"], bool)
        else None,
        pidns=_text(rec.get("pidns")),
        boot=_text(rec.get("boot")),
    )


@dataclass(frozen=True, slots=True)
class _TaskFile:
    task_list: str
    id: str
    subject: str
    status: str
    owner: str | None
    blocked_by: tuple[str, ...]
    mtime: datetime

    @property
    def ref(self) -> str:
        return f"{self.task_list}/{self.id}"


@dataclass(slots=True)
class ClaudeCodeAdapter:
    tasks_dir: Path = DEFAULT_TASKS_DIR
    task_lists: Sequence[str] = ()  # list ids to read; empty: no board tasks
    spool: Path | None = DEFAULT_SPOOL  # None: no hook data, no sessions
    lookback: timedelta = DEFAULT_LOOKBACK
    stop_dir: Path | None = DEFAULT_STOP_DIR  # None: stop is refused (alerts instead)
    # (pid, what the hook recorded) -> process running, None: cannot tell. None: the real
    # check against this host's /proc (process_state); replaceable in tests.
    process_check: Callable[[int, Mapping[str, Any]], bool | None] | None = field(
        default=None, repr=False, compare=False
    )
    name: str = NAME
    # The action kinds it carries out (``kelpie agent`` offers only these): stop via the hook's
    # marker. Task lists are only read.
    actions: ClassVar[frozenset[ActionKind]] = frozenset({ActionKind.STOP})
    _warned: set[str] = field(default_factory=set, init=False, repr=False, compare=False)
    # Sessions seen running, kept watched after their lines leave ``lookback``.
    _watched: dict[str, _Watched] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.tasks_dir = Path(self.tasks_dir).expanduser()
        self.task_lists = tuple(self.task_lists)
        if self.spool is not None:
            self.spool = Path(self.spool).expanduser()
        if self.stop_dir is not None:
            self.stop_dir = Path(self.stop_dir).expanduser()

    # --- status --------------------------------------------------------------------------

    def check(self) -> str | None:
        """``None`` if every configured task list can be read, else why not."""
        for task_list in self.task_lists:
            path = self.tasks_dir / list_dir_name(task_list)
            if not path.is_dir():
                return f"task list {task_list!r} not found: {path}"
            try:
                next(path.iterdir(), None)
            except OSError as exc:
                return f"task list {task_list!r} not readable: {exc}"
        return None

    def list_counts(self) -> dict[str, tuple[int, int]]:
        """Per configured list: (tasks, tasks in progress). Raises ClaudeCodeError."""
        counts: dict[str, tuple[int, int]] = {}
        for task_list in self.task_lists:
            files = self._read_list(task_list, strict=True)
            running = sum(1 for f in files if f.status == "in_progress")
            counts[task_list] = (len(files), running)
        return counts

    def status_detail(self, now: datetime) -> list[str]:
        """Extra lines for ``kelpie status``: task lists and how fresh the spool is."""
        lines = [f"task lists: {', '.join(self.task_lists) or 'none'} ({self.tasks_dir})"]
        if self.spool is None:
            return [*lines, "spool: disabled"]
        if not self.spool.exists():
            return [*lines, f"spool: missing ({self.spool})"]
        last = self.last_spool_at()
        if last is None:
            return [*lines, f"spool: no readable lines ({self.spool})"]
        return [*lines, f"spool: last line {format_age(now - last)} ago ({self.spool})"]

    def last_spool_at(self) -> datetime | None:
        event = self.last_spool_event()
        return None if event is None else event.at

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

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now - self.lookback
        events = sorted(self._events(cutoff, now + FUTURE_SKEW), key=lambda e: e.at)
        files = [f for tl in self.task_lists for f in self._read_list(tl)]
        updates = self._status_updates(events)
        tasks, dependencies = self._tasks(files, updates, cutoff)
        check = self._checker()
        sessions = self._sessions(events, self._markers(events), now, check)
        sessions += self._still_watched(sessions, now, check)
        owners = {t.owner_id for t in tasks if t.owner_id} | {s.owner_id for s in sessions}
        return Snapshot(
            now=now,
            tasks=tasks,
            owners=tuple(Owner(id=o, role=o) for o in sorted(owners)),
            dependencies=dependencies,
            sessions=sessions,
            spans=self._spans(events, sessions),
        )

    def _read_list(self, task_list: str, strict: bool = False) -> list[_TaskFile]:
        path = self.tasks_dir / list_dir_name(task_list)
        try:
            names = sorted(p.name for p in path.iterdir())
        except OSError as exc:
            if strict:
                raise ClaudeCodeError(f"cannot read task list {task_list!r}: {exc}") from exc
            self._warn(
                f"list:{task_list}", "claude-code: task list %r not readable: %s", task_list, exc
            )
            return []
        files: list[_TaskFile] = []
        for name in names:
            if not _TASK_FILE.match(name):
                continue  # .lock and other bookkeeping
            if len(files) >= MAX_TASKS_PER_LIST:
                self._warn(
                    f"cap:{task_list}",
                    "claude-code: task list %r has more than %d tasks; the rest are left out",
                    task_list,
                    MAX_TASKS_PER_LIST,
                )
                break
            if (task := self._read_task(task_list, path / name)) is not None:
                files.append(task)
        return files

    def _read_task(self, task_list: str, path: Path) -> _TaskFile | None:
        """One task file. Agents write these directories, so the file is opened without
        following symlinks and without blocking (a FIFO), checked to be a regular file on
        the open descriptor, and read up to ``MAX_TASK_FILE`` bytes only."""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return None  # removed, a symlink, or unreadable
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TASK_FILE:
                return None
            raw = os.read(fd, MAX_TASK_FILE + 1)
        except OSError:
            return None
        finally:
            os.close(fd)
        if len(raw) > MAX_TASK_FILE:
            return None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None  # being rewritten or not JSON: next snapshot
        if not isinstance(data, dict):
            return None
        task_id = _text(data.get("id")) if isinstance(data.get("id"), str) else None
        status = data.get("status")
        if task_id is None or task_id != path.stem or not isinstance(status, str):
            return None
        blocked = data.get("blockedBy")
        owner = data.get("owner")
        return _TaskFile(
            task_list=task_list,
            id=task_id,
            subject=str(data.get("subject") or "")[:MAX_TITLE]
            if isinstance(data.get("subject"), str)
            else "",
            status=status,
            owner=((_text(owner) or "")[:MAX_FIELD] or None) if isinstance(owner, str) else None,
            blocked_by=tuple(b[:MAX_FIELD] for b in blocked[:MAX_BLOCKED_BY] if isinstance(b, str))
            if isinstance(blocked, list)
            else (),
            mtime=datetime.fromtimestamp(info.st_mtime, UTC),
        )

    def _tasks(
        self,
        files: Sequence[_TaskFile],
        updates: Mapping[str, datetime],
        cutoff: datetime,
    ) -> tuple[tuple[Task, ...], tuple[Dependency, ...]]:
        by_ref = {f.ref: f for f in files}
        tasks: list[Task] = []
        dependencies: list[Dependency] = []
        for f in files:
            if f.status == "pending":
                waiting = any(
                    (dep := by_ref.get(f"{f.task_list}/{b}")) is not None
                    and dep.status != "completed"
                    for b in f.blocked_by
                )
                status = Status.BACKLOG if waiting else Status.READY
            elif (mapped := STATUS_MAP.get(f.status)) is not None:
                status = mapped
            else:
                self._warn(
                    f"status:{f.task_list}",  # agents control the text: one warning per list
                    "claude-code: task %s has unknown status %r; skipped (warned once per list)",
                    f.ref,
                    f.status[:32],
                )
                continue
            if status is Status.DONE and f.mtime < cutoff:
                continue
            tasks.append(
                Task(
                    id=f.ref,
                    title=f.subject,
                    status=status,
                    status_since=updates.get(f.ref, f.mtime),
                    role=f.owner,
                    owner_id=f.owner,
                    source=self.name,
                    platform_status=f.status,
                )
            )
        kept = {t.id for t in tasks}
        for f in files:
            for b in f.blocked_by:
                parent = f"{f.task_list}/{b}"
                if f.ref in kept and parent in kept:
                    dependencies.append(Dependency(task_id=f.ref, depends_on=parent))
        return tuple(tasks), tuple(dependencies)

    def _task_ref(self, event: SpoolEvent) -> str | None:
        """``<list>/<id>`` of the task a ``TaskUpdate`` event names, if it is on a watched list.

        The hook records the session's list (``CLAUDE_CODE_TASK_LIST_ID``, else the session id,
        Claude Code's own default), so a session's private tasks never pass for farm tasks."""
        if event.task is None or event.task_list not in self.task_lists:
            return None
        return f"{event.task_list}/{event.task}"

    def _status_updates(self, events: Iterable[SpoolEvent]) -> dict[str, datetime]:
        """Latest time each task's status was set through ``TaskUpdate``."""
        latest: dict[str, datetime] = {}
        for e in events:
            if e.task_status is not None and (ref := self._task_ref(e)) is not None:
                latest[ref] = e.at
        return latest

    def _markers(self, events: Sequence[SpoolEvent]) -> dict[str, datetime]:
        """Stop marker times of sessions in the spool (``{}`` without a stop directory)."""
        if self.stop_dir is None:
            return {}
        try:
            names = set(os.listdir(self.stop_dir))
        except OSError:
            return {}
        found: dict[str, datetime] = {}
        for sid in {e.session_id for e in events}:
            if f"{sid}.json" in names and (at := read_marker(self.stop_dir, sid)) is not None:
                found[sid] = at
        return found

    def _sessions(
        self,
        events: Sequence[SpoolEvent],
        markers: Mapping[str, datetime] | None,
        now: datetime,
        check: Callable[[int, Mapping[str, Any]], bool | None],
    ) -> tuple[Session, ...]:
        grouped: dict[str, list[SpoolEvent]] = {}
        for e in events:
            grouped.setdefault(e.session_id, []).append(e)
        sessions: list[Session] = []
        for sid, evs in grouped.items():
            last = evs[-1]
            ended = last.type == "session_end"
            ended_at = last.at if ended else None
            heartbeat = last.at
            alive = None if ended else _process_alive(evs, now, check)
            if alive:  # an idle interactive session: its process is there, waiting
                heartbeat = max(heartbeat, now)
            elif alive is False:  # the process is gone without a SessionEnd: it ended
                ended_at = last.at
            claimed: str | None = None
            declared: str | None = None
            owner_set: str | None = None
            for e in evs:
                # Owners set on other tasks (a lead assigning work) say nothing about this one.
                ref = self._task_ref(e)
                if ref is None:
                    continue
                if e.task_status == "in_progress":
                    claimed, declared, owner_set = ref, None, e.task_owner or owner_set
                elif ref == claimed:
                    declared = e.task_status or declared
                    owner_set = e.task_owner or owner_set
            kelpie_task = next((e.kelpie_task for e in reversed(evs) if e.kelpie_task), None)
            agent = next((e.agent for e in reversed(evs) if e.agent), None)
            agent_type = next(
                (e.agent_type for e in reversed(evs) if e.agent_type and e.agent_id is None), None
            )
            failure = next((e.error for e in reversed(evs) if e.type == "turn_end"), None)
            reason = last.reason if ended else "process gone" if alive is False else None
            stopped = next((e for e in reversed(evs) if e.type in ("stopped", "prompt")), None)
            if stopped is not None and stopped.type == "stopped":
                failure = " ".join(x for x in ("stopped by kelpie", failure) if x)
            marked = (markers or {}).get(sid)
            if marked is not None and not ended and _ignored_stop(evs, marked):
                self._warn(
                    f"unenforced:{sid}",
                    "claude-code: session %s still calls tools %s after its stop; is the hook "
                    "registered for PreToolUse, current, run as the same user, and pointed at "
                    "the same stop_dir?",
                    sid,
                    STOP_GRACE,
                )
                failure = " ".join(x for x in ("stop not enforced", failure) if x)
            sessions.append(
                Session(
                    id=sid,
                    task_id=kelpie_task or claimed or f"{NO_TASK_PREFIX}{sid}",
                    owner_id=agent or owner_set or agent_type or DEFAULT_OWNER,
                    started_at=evs[0].at,
                    ended_at=ended_at,
                    final_status=Status.DONE
                    if ended_at is not None and kelpie_task is None and declared == "completed"
                    else None,
                    exit_reason=" ".join(x for x in (reason, failure) if x)[:300] or None,
                    last_heartbeat=heartbeat,
                    source=self.name,
                )
            )
            if alive:
                self._watched[sid] = _Watched(sessions[-1], _newest(evs))
            elif alive is False or ended:
                self._watched.pop(sid, None)  # (None: watched on, see _still_watched)
        return tuple(sessions)

    def _checker(self) -> Callable[[int, Mapping[str, Any]], bool | None]:
        if self.process_check is not None:
            return self.process_check
        local = hook.identity()  # once per snapshot
        return lambda pid, recorded: process_state(pid, recorded, local)

    def _still_watched(
        self,
        current: Sequence[Session],
        now: datetime,
        check: Callable[[int, Mapping[str, Any]], bool | None],
    ) -> tuple[Session, ...]:
        """Sessions seen running whose spool lines have left ``lookback``: still running,
        they stay alive; gone, they end (and are reported for ``WATCH_ENDED`` more); when it
        cannot be told ``WATCH_UNKNOWN`` times in a row, they are dropped as before."""
        seen = {s.id for s in current}
        kept: list[Session] = []
        for sid, w in list(self._watched.items()):
            if sid in seen:
                continue
            if w.ended_seen is not None:
                if now - w.ended_seen > WATCH_ENDED:
                    del self._watched[sid]
                else:
                    kept.append(w.session)
                continue
            alive = check(w.newest.pid, w.newest.recorded) if w.newest.pid else None
            if alive:
                self._watched[sid] = _Watched(replace(w.session, last_heartbeat=now), w.newest)
                kept.append(self._watched[sid].session)
            elif alive is None and w.unknown + 1 < WATCH_UNKNOWN:
                w.unknown += 1  # e.g. /proc briefly unreadable: keep it as last seen
                kept.append(w.session)
            elif alive is False:
                gone = replace(
                    w.session,
                    ended_at=w.newest.at,
                    last_heartbeat=w.newest.at,
                    exit_reason="process gone",
                )
                self._watched[sid] = _Watched(gone, w.newest, ended_seen=now)
                kept.append(gone)
            else:
                del self._watched[sid]
        return tuple(kept)

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
        return spans_from_hook_events(hook_events, prefix=self.name)

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

    def stop(self, session_id: str, reason: str) -> str | ActionResult:
        """Stop a session at its next tool call (or machine-submitted prompt).

        Writes the stop marker the hook reads (see :mod:`kelpie.adapters.claude_code.hook`):
        cooperative, so a session in the middle of a model call stops once that call asks for
        a tool, and the stop holds until a human types a prompt in the session, removes the
        marker, or ``KELPIE_CLAUDE_STOP_TTL`` passes. Idempotent: an ended session is left
        alone (``NOTHING_TO_DO``); otherwise returns what the stop means here, for the human
        told about it. Raises :class:`UnsupportedAction` when stop is turned off or the
        session is not in the spool (no Kelpie hook runs there), :class:`ClaudeCodeError` for
        an id that is not a Claude Code session id or a marker that cannot be written.
        """
        if hook.marker_path(session_id) is None:
            raise ClaudeCodeError(f"not a Claude Code session id: {session_id!r}")
        if self.stop_dir is None:
            raise UnsupportedAction("stop is turned off ([adapters.claude_code] stop_dir = false)")
        now = datetime.now(UTC)
        own = sorted(
            (e for e in self._events(now - self.lookback) if e.session_id == session_id),
            key=lambda e: e.at,
        )
        watched = self._watched.get(session_id)
        if not own and watched is not None:
            if watched.ended_seen is not None:
                log.info("claude-code: session %s has ended; nothing to stop", session_id)
                return ActionResult.NOTHING_TO_DO
            own = [watched.newest]  # running for longer than lookback, idle
        if not own:
            raise UnsupportedAction(
                f"session {session_id} is not in the hooks spool, so no Kelpie hook can stop it"
            )
        last = own[-1]
        if last.type == "session_end" or _process_alive(own, now, self._checker()) is False:
            log.info("claude-code: session %s has ended; nothing to stop", session_id)
            return ActionResult.NOTHING_TO_DO
        if last.version < 1:
            raise UnsupportedAction(
                f"session {session_id} runs a Kelpie hook without stop support; copy the "
                "current hook.py and register it for PreToolUse"
            )
        try:
            write_marker(self.stop_dir, session_id, reason, now.timestamp())
        except OSError as exc:
            raise ClaudeCodeError(
                f"cannot write the stop marker for {session_id}: {exc}",
                "the stop marker could not be written",
            ) from exc
        log.info("claude-code: stop requested for session %s", session_id)
        return (
            "the session stops at its next tool call, until a human types a prompt in it or "
            "the stop expires"
        )

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


def write_marker(directory: Path, session_id: str, reason: str, at: float) -> Path:
    """Write ``<directory>/<session_id>.json`` atomically (mode 0600), replacing an older one."""
    if not hook._SESSION_ID.fullmatch(session_id):
        raise ValueError(f"not a Claude Code session id: {session_id!r}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError(f"{directory} is not a plain directory")
    body = json.dumps({"at": at, "reason": hook.clean_reason(reason)}).encode()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".stop-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        target = directory / f"{session_id}.json"
        os.replace(tmp, target)  # replaces a symlink itself, never its target
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def read_marker(directory: Path, session_id: str) -> datetime | None:
    """When the session's stop marker was written; None if there is no usable one."""
    try:
        fd = _open_regular(directory / f"{session_id}.json")
    except OSError:
        return None
    try:
        raw = os.read(fd, MAX_MARKER + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8", "replace")) if len(raw) <= MAX_MARKER else None
    except ValueError:
        return None
    return _utc(data.get("at")) if isinstance(data, dict) else None


def _ignored_stop(events: Sequence[SpoolEvent], marked: datetime) -> bool:
    """The session went on calling tools well after its stop marker, and the hook never
    refused anything: the stop is not enforced."""
    after = [e for e in events if e.at >= marked]
    if any(e.type == "stopped" for e in after):
        return False
    # A call counts by its start (a long call begun before the stop is not a miss), and only
    # while the marker holds for the hook (it ignores markers older than its TTL).
    expires = marked + timedelta(seconds=hook.DEFAULT_STOP_TTL)
    return any(
        e.type == "tool"
        and marked + STOP_GRACE < e.at - timedelta(milliseconds=e.duration_ms) < expires
        for e in after
    )


@dataclass(slots=True)
class _Watched:
    session: Session  # as last built from the spool
    newest: SpoolEvent  # the line whose process is checked
    ended_seen: datetime | None = None  # when it was found gone
    unknown: int = 0  # snapshots in a row its process could not be told


def _newest(events: Sequence[SpoolEvent]) -> SpoolEvent:
    """The newest line that describes the session's process (``stopped`` lines carry none)."""
    return next((e for e in reversed(events) if e.type != "stopped"), events[-1])


def _process_alive(
    events: Sequence[SpoolEvent],
    now: datetime,
    check: Callable[[int, Mapping[str, Any]], bool | None],
) -> bool | None:
    """Whether the session's Claude Code process still runs, from its newest line: True or
    False only when it can be told for sure, else None. A newest line without a pid (a
    resumed session under an older hook) cannot be told; a "gone" for a session that wrote
    a line within ``PROCESS_GRACE`` is not trusted."""
    newest = _newest(events)
    if newest.pid is None:
        return None
    alive = check(newest.pid, newest.recorded)
    if alive is False and now - events[-1].at < PROCESS_GRACE:
        return None
    return alive
