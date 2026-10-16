"""Hermes platform adapter: read side.

Describes a Hermes farm to Kelpie from two sources:

- the Kanban SQLite database (``~/.hermes/kanban.db``), opened strictly read-only, one
  short-lived connection per snapshot (Hermes writes to it concurrently, WAL mode);
- the hooks spool written by the Hermes plugin (:mod:`kelpie.adapters.hermes.plugin`),
  turned into task, session and tool spans by
  :func:`kelpie.traces.hooks.spans_from_hook_events` (a Hermes run is a session,
  ``hermes-run-<id>``). When Hermes also exports OTLP spans for the same calls, the app
  keeps the OTLP ones, completed from the hooks (:func:`kelpie.traces.hooks.merge_hook_spans`).

Actions, all through Hermes' own CLI (Kelpie never writes ``kanban.db``): ``stop``
(``SupportsStop``, see :mod:`kelpie.adapters.hermes.stop`); ``wait`` (``SupportsWait``: park
a ready or running task in Hermes ``scheduled`` with its conditions), ``requeue`` (wake a task
Kelpie parked) and ``comment``, see :mod:`kelpie.adapters.hermes.park`. Unblock and WIP commit
are not implemented yet, and requeue of a task Kelpie did not park is not supported.

Status mapping (task ``status`` -> :class:`~kelpie.core.model.Status`)::

    triage, todo, scheduled -> BACKLOG   (``scheduled`` is a timed queue; see below)
    ready                   -> READY
    running                 -> IN_PROGRESS
    blocked                 -> BLOCKED
    review                  -> REVIEW
    scheduled, parked by Kelpie (marker in its reason, see park.py) -> WAITING
    done                    -> DONE
    archived                -> (skipped)

``body`` is ``tasks.body`` (the description; none for finished tasks), at most ``MAX_BODY``
characters; PR references in it link nothing (incident 17). ``assignee`` is the assignee
profile and ``role`` its role: on Hermes an assignee only routes a task until it is
dispatched. ``owner_id`` is the profile of the live run that claims the task (its
``current_run_id`` names a run that has not ended), none while nothing runs, and the
assignee itself for a human owner (``human_owners``). Hermes has no roles, so ``roles``
(``[adapters.hermes.roles]``) maps profiles to roles, and any other profile's role is its name
without a trailing ``-<number>`` (``dev-1``, ``dev-2`` -> ``dev``; ``role_from_name = false``
turns that off), else the profile itself. Unclaimed tasks are therefore queued work of their
role for rule 6 (rule 6's ``ready_is_queued`` is not needed for Hermes).
``status_since`` is the time of the latest status-changing event of the task (any
``task_events`` kind except the non-status ones in ``NON_STATUS_EVENTS``), else
``started_at``/``completed_at``/``created_at``. Done tasks older than ``lookback`` are left
out.

Blocks. A ``blocked`` task gets a :class:`~kelpie.core.model.Block` only from evidence that is
current, that is a ``blocked`` event at or after ``status_since``:

- typed block (``needs_input``, ``capability``, ``transient``; Hermes parks these for an
  operator): ``waiting_human: <kind>: <reason>``, which the exit contract allows;
- untyped legacy block: the raw reason text;
- no such event (the circuit breaker parked the task, ``tasks.block_kind`` is left over from an
  earlier block): from the latest status event; ``gave_up`` gives
  ``guard: hermes circuit breaker (<failures>/<limit>): <error>``, anything else an empty reason.

Sessions: one per ``task_runs`` row (``hermes-run-<id>``), for runs that are open or ended
within ``lookback``::

    run outcome                                    final_status
    completed                                      DONE
    review_requested                               REVIEW
    blocked                                        BLOCKED
    crashed, timed_out, stale, rate_limited,
    spawn_failed, gave_up, reclaimed,
    changes_requested, scheduled,
    NULL (still open or ended without outcome)     None  (the session declared nothing)

``exit_reason`` is the outcome, followed by the run's error text when there is one.

Progress note and git state. ``Session.summary`` is ``task_runs.summary``. The git fields
(``last_commit``, ``last_commit_at``, ``branch``, ``changed_files``, ``dirty_worktree``) and
the task's ``files`` (what it changed since its merge-bases with ``base_refs``, plus
what is uncommitted; rule 12 compares them across live tasks) come from the workspace as it
is now, so they go only on the latest run of a task that is not
finished and whose ``workspace_kind`` is ``worktree`` with an existing absolute
``workspace_path``. A resolved path used by more than one such run is read for none of them.
Workspaces are agent-controlled: see :mod:`kelpie.adapters.hermes.git_state` for how git is run
there; any failure leaves a field ``None``. ``branch`` is ``tasks.branch_name``, else git's.
Each path is read at most once per snapshot, by up to ``GIT_WORKERS`` threads, all within
``GIT_BUDGET_FACTOR * git_timeout``; a path whose git call timed out is skipped for
``GIT_BACKOFF`` (by snapshot time).

Liveness. ``Session.last_heartbeat`` is the latest of the run's ``last_heartbeat_at``, the
task's ``last_heartbeat_at`` (when the run is the task's ``current_run_id``) and the newest
spool tool call of the run. A valid Hermes claim counts as a heartbeat: an open run that is the
task's ``current_run_id`` with ``claim_expires`` in the future has ``last_heartbeat = now``
(Hermes extends claims for workers that are alive in a long LLM call). Hermes tolerates stale
heartbeats for longer than Kelpie's 30 minute default, so set ``session_stale_after = "60m"``
for the rules that take it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from kelpie.adapters.hermes.git_state import (
    DEFAULT_TIMEOUT,
    GitState,
    Problem,
    probe_git,
    read_git_state,
    valid_base_ref,
    warn_once,
)
from kelpie.adapters.hermes.park import (
    DEFAULT_WAIT_KEY,
    comment_args,
    decode_wait,
    encode_wait,
    has_marker,
    load_key,
    run_kanban,
    schedule_args,
    unblock_args,
)
from kelpie.adapters.hermes.stop import (
    CLI_TIMEOUT,
    DEFAULT_CLI,
    STOP_DONE,
    HermesCliError,
    block_task,
    run_id,
    stop_comment,
)
from kelpie.core.actions import ActionKind, ActionResult
from kelpie.core.model import (
    Artifact,
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Return,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.core.ports import PlatformEvent, UnsupportedAction
from kelpie.traces.hooks import SESSION_END, TOOL, HookEvent, spans_from_hook_events
from kelpie.traces.semconv import OUTPUT_CHARS_ATTRIBUTE

log = logging.getLogger(__name__)

DEFAULT_DB = Path("~/.hermes/kanban.db")
DEFAULT_SPOOL = Path("~/.hermes/kelpie/spool.jsonl")
DEFAULT_LOOKBACK = timedelta(hours=24)
DEFAULT_GIT_TIMEOUT = timedelta(seconds=DEFAULT_TIMEOUT)
# Branches task work starts from: a task's files are what it changed since its newest
# merge-base with any of them, plus what is uncommitted.
DEFAULT_BASE_REFS = ("origin/main", "main")
BUSY_TIMEOUT_MS = 5000
MAX_BODY = 8000  # characters of a task's description kept in the snapshot
# A trailing instance number of a profile name: dev-1, dev-2 -> role "dev".
_INSTANCE = re.compile(r"^(.+?)-\d+$")
# The ``tasks.workspace_kind`` whose directory is the task's own: ``dir`` may point to a shared
# or human checkout, ``scratch`` is not a repository.
WORKSPACE_KINDS = ("worktree",)
GIT_WORKERS = 4  # workspaces read in parallel
GIT_BUDGET_FACTOR = 2  # all git calls of one snapshot end within this many git_timeouts
GIT_BACKOFF = timedelta(minutes=5)  # a workspace whose git timed out is skipped this long

NOT_IMPLEMENTED = (
    "this Hermes action is not implemented yet (only stop is); "
    "run the rule in observe or suggest mode"
)

STATUS_MAP: Mapping[str, Status] = {
    "triage": Status.BACKLOG,
    "todo": Status.BACKLOG,
    "scheduled": Status.BACKLOG,
    "ready": Status.READY,
    "running": Status.IN_PROGRESS,
    "blocked": Status.BLOCKED,
    "review": Status.REVIEW,
    "done": Status.DONE,
}

OUTCOME_STATUS: Mapping[str, Status] = {
    "completed": Status.DONE,
    "review_requested": Status.REVIEW,
    "blocked": Status.BLOCKED,
}

# ``task_events`` kinds that do not change a task's status; every other kind does.
NON_STATUS_EVENTS = (
    "heartbeat",
    "claim_extended",
    "commented",
    "edited",
    "assigned",
    "specified",
    "attached",
    "attachment_removed",
    "reprioritized",
    "spawned",
    "worker_registered",
    "pid_alive",
    "reclaim_deferred",
    "respawn_guarded",
    "skipped_nonspawnable",
    "claim_rejected",
    "linked",
    "unlinked",
    "archive_worker_termination",
    "suspected_hallucinated_references",
    "completion_blocked_hallucination",
    "completion_blocked_empty_result",
    # written by the dispatcher or an operator without changing the status
    "terminal_worker_reaped",
    # bookkeeping of a worker's turns and PR receipts, also after a task completed
    "worker_bound",
    "worker_result",
    "owner_admitted",
    "pr_acceptance",
    "model_override_set",
    "reasoning_effort_set",
    "tip_scratch_workspace",
    "workspace_cleanup_deferred_shared",
)
BLOCK_EVENTS = ("blocked", "auto_blocked")
# Statuses before any work starts that only a person (or the decomposer) moves a card out of:
# a card there holding up others is rule 3's "stuck before work" (incident 17).
DEFAULT_PREWORK = ("triage", "scheduled")
PREWORK_ALLOWED = frozenset({"triage", "scheduled"})  # todo: waiting for parents is queued
MAX_RETURN_REASON = 2000  # characters of a review's reason kept on a return
MAX_ARTIFACT_PATH = 500  # characters of an artifact path kept
AGENT_UPLOADER = "agent"  # task_attachments.uploaded_by of Hermes' kanban_attach tool
# Hermes block kinds that wait for an operator (``dependency`` waits in ``todo``).
TYPED_BLOCKS = ("needs_input", "capability", "transient")


class HermesError(RuntimeError):
    """The Hermes board cannot be read, or a stop failed. ``public_reason`` (when set) says
    why without anything a CLI printed, for the human told about it."""

    def __init__(self, message: str, public_reason: str | None = None) -> None:
        super().__init__(message)
        self.public_reason = public_reason


def _utc(epoch: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(epoch), UTC) if epoch is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _in_clause(values: Sequence[str]) -> str:
    return ",".join("?" * len(values))


def _subtasks(event: sqlite3.Row | None) -> tuple[str, ...]:
    """The children a ``decomposed`` event split the task into."""
    if event is None:
        return ()
    ids = _payload(event["payload"]).get("child_ids")
    if not isinstance(ids, list):
        return ()
    return tuple(dict.fromkeys(str(i) for i in ids if isinstance(i, str) and i.strip()))


def _reopens(conn: _Connection, ids: set[str]) -> dict[str, tuple[datetime, ...]]:
    """When a done task was moved back out of done: a ``status`` move to anything but done
    or archived after it was done (a ``completed`` event, or a later ``status`` move to
    done). Only tasks that ever completed are read. A done task Hermes moves back because an
    ancestor was reopened (``reason: ancestor_reopened``) is not counted: the ancestor's own
    reopen is, once."""
    rows = conn.execute(
        "SELECT task_id, kind, payload, created_at FROM task_events"
        " WHERE kind IN ('completed', 'status') AND task_id IN"
        " (SELECT DISTINCT task_id FROM task_events WHERE kind = 'completed')"
        " ORDER BY task_id, created_at, id"
    ).fetchall()
    found: dict[str, list[datetime]] = {}
    done: dict[str, bool] = {}
    for r in rows:
        tid = r["task_id"]
        if tid not in ids:
            continue
        payload = _payload(r["payload"])
        to = payload.get("status") if r["kind"] == "status" else "done"
        if payload.get("reason") == "ancestor_reopened":
            done[tid] = False
            continue
        if to in ("done", "archived"):
            done[tid] = True
        elif done.get(tid) and isinstance(to, str):
            done[tid] = False
            if (at := _utc(r["created_at"])) is not None:
                found.setdefault(tid, []).append(at)
    return {k: tuple(v) for k, v in found.items()}


def _evidence(
    conn: _Connection, ids: set[str], cutoff: datetime
) -> list[tuple[str, int | str, str, datetime]]:
    """What a run left as evidence: ``(task, run id or uploader, path, when)`` for the
    artifacts a ``completed`` or ``review_requested`` event lists (with its run) and for files
    attached to the task (``task_attachments``, by who uploaded them; none on older
    schemas)."""
    found: list[tuple[str, int | str, str, datetime]] = []
    rows = conn.execute(
        "SELECT task_id, run_id, payload, created_at FROM task_events"
        " WHERE kind IN ('completed', 'review_requested') AND run_id IS NOT NULL"
        " AND created_at >= ?",
        (int(cutoff.timestamp()),),
    ).fetchall()
    for r in rows:
        at = _utc(r["created_at"])
        paths = _payload(r["payload"]).get("artifacts")
        if r["task_id"] not in ids or at is None or not isinstance(paths, list):
            continue
        found.extend(
            (r["task_id"], int(r["run_id"]), str(p)[:MAX_ARTIFACT_PATH], at)
            for p in paths
            if isinstance(p, str) and p.strip()
        )
    try:
        rows = conn.execute(
            "SELECT task_id, filename, uploaded_by, created_at FROM task_attachments"
            " WHERE created_at >= ?",
            (int(cutoff.timestamp()),),
        ).fetchall()
    except sqlite3.OperationalError:
        return found
    for r in rows:
        at = _utc(r["created_at"])
        who = (r["uploaded_by"] or "").strip() if isinstance(r["uploaded_by"], str) else ""
        if r["task_id"] in ids and at is not None and who and isinstance(r["filename"], str):
            found.append((r["task_id"], who, r["filename"][:MAX_ARTIFACT_PATH], at))
    return found


def _with_evidence(
    sessions: tuple[Session, ...], evidence: Sequence[tuple[str, int | str, str, datetime]]
) -> tuple[Session, ...]:
    """Evidence as artifacts of the run that left it: the run a completion event names, or
    for an attachment, the latest run on the task that started before the upload, of the
    uploading profile (``hermes kanban attach`` records it) or of any profile when the
    uploader is ``agent`` (Hermes' attach tool). Files a person or the dashboard uploaded,
    or uploaded before any run, are no run's evidence."""
    if not evidence:
        return sessions
    by_id = {s.id: s for s in sessions}
    runs: dict[tuple[str, str], list[Session]] = {}
    for s in sessions:
        runs.setdefault((s.task_id, s.owner_id), []).append(s)
    extra: dict[str, list[Artifact]] = {}
    for task_id, source, path, at in evidence:
        if isinstance(source, int):
            run_id = f"hermes-run-{source}"
            if run_id in by_id and by_id[run_id].task_id == task_id:
                extra.setdefault(run_id, []).append(Artifact(path, at))
            continue
        if source == AGENT_UPLOADER:
            candidates = [s for (t, _), ss in runs.items() if t == task_id for s in ss]
        else:
            candidates = list(runs.get((task_id, source), ()))
        mine = [s for s in candidates if s.started_at <= at]
        if mine:
            run = max(mine, key=lambda s: (s.started_at, s.id))
            extra.setdefault(run.id, []).append(Artifact(path, at))
    return tuple(
        replace(s, artifacts=s.artifacts + tuple(extra[s.id])) if s.id in extra else s
        for s in sessions
    )


def _run_id(raw: Any) -> int | None:
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


def _payload(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _col(row: sqlite3.Row, name: str) -> Any:
    """A column that older Hermes schemas may not have."""
    return row[name] if name in row.keys() else None  # noqa: SIM118


def _workspace(row: sqlite3.Row) -> Path | None:
    """The task's own directory, if it is one that exists on this host."""
    if _col(row, "workspace_kind") not in WORKSPACE_KINDS:
        return None
    raw = str(_col(row, "workspace_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return path if path.is_dir() else None


def _body(row: sqlite3.Row) -> str | None:
    """The task's description (``tasks.body``), the first ``MAX_BODY`` characters. Bytes
    (a BLOB-typed body) are decoded as Hermes does, with replacement characters."""
    raw = _col(row, "body")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = raw.strip() if isinstance(raw, str) else ""
    return text[:MAX_BODY] or None


def _with_files(task: Task, state: GitState | None) -> Task:
    """The task with its worktree's files and repository, when git could say, and its
    branch from git when ``tasks.branch_name`` is empty."""
    if state is None:
        return task
    if task.branch is None and state.branch:
        task = replace(task, branch=state.branch)
    if state.paths is None:
        return task
    return replace(task, files=state.paths, repo=state.repo)


def _park_conditions(
    task_id: str, scheduled: sqlite3.Row | None, key: bytes | None, window: timedelta
) -> list[tuple[str, ConditionKind, dict[str, Any]]] | None:
    """The conditions of Kelpie's park of a task, from its latest ``scheduled`` event, when
    that carries a marker signed for this task and recorded when Kelpie issued it (within
    ``window``); else ``None``."""
    if scheduled is None:
        return None
    reason = _payload(scheduled["payload"]).get("reason")
    if not isinstance(reason, str):
        return None
    return decode_wait(reason, task_id, key, _utc(scheduled["created_at"]), window)


def _park_reason(scheduled: sqlite3.Row | None) -> object:
    return None if scheduled is None else _payload(scheduled["payload"]).get("reason")


def _block_text(row: sqlite3.Row, status_event: sqlite3.Row | None, block_event: Any) -> str:
    if block_event is not None:  # a block event at/after the block's start: current evidence
        payload = _payload(block_event["payload"])
        kind = str(payload.get("kind") or _col(row, "block_kind") or "")
        reason = str(payload.get("reason") or "").strip()
        if kind in TYPED_BLOCKS:  # Hermes parks these for an operator
            return f"waiting_human: {kind}" + (f": {reason}" if reason else "")
        return ": ".join(x for x in (kind, reason) if x)
    # No current block event: ``tasks.block_kind`` may be left over from an earlier block.
    if status_event is not None and status_event["kind"] == "gave_up":
        p = _payload(status_event["payload"])
        failures = p.get("failures", _col(row, "consecutive_failures"))
        limit = p.get("effective_limit")
        error = str(p.get("error") or _col(row, "last_failure_error") or "").strip()
        count = f" ({failures}/{limit})" if failures and limit else ""
        return f"guard: hermes circuit breaker{count}" + (f": {error}" if error else "")
    return ""


def format_age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@dataclass(frozen=True, slots=True)
class _ParkState:
    status: str  # Hermes status
    run: int | None  # current run id
    ours: bool  # the latest ``scheduled`` event is Kelpie's park (verified)
    woken: bool  # it has left ``scheduled`` since (unblock, or a human in the dashboard)


@dataclass(slots=True)
class HermesAdapter:
    db_path: Path = DEFAULT_DB
    spool: Path | None = DEFAULT_SPOOL  # None: no hook data, no spans
    lookback: timedelta = DEFAULT_LOOKBACK
    git_state: bool = True  # read the git state of the workspaces of live tasks
    git_timeout: timedelta = DEFAULT_GIT_TIMEOUT  # per git call
    base_refs: tuple[str, ...] = DEFAULT_BASE_REFS  # see DEFAULT_BASE_REFS
    cli: str = DEFAULT_CLI  # the Hermes executable that carries out ``stop``
    cli_timeout: float = CLI_TIMEOUT  # seconds; a stop runs inside the control loop tick
    # Assignees that are people, not agents (Hermes lowercases profile names).
    human_owners: Collection[str] = ()
    prework_statuses: Collection[str] = DEFAULT_PREWORK
    # Profile -> role (Hermes lowercases profiles). Others: the profile without a trailing
    # "-<number>" (dev-1 -> dev), else the profile itself.
    roles: Mapping[str, str] = field(default_factory=dict)
    role_from_name: bool = True  # dev-1, dev-2 -> dev; false: a profile is its own role
    name: str = "hermes"
    # The action kinds it carries out (``kelpie agent`` offers only these): stop, park, wake
    # of a task Kelpie parked (requeue), comment. unblock and wip_commit are not implemented.
    actions: ClassVar[frozenset[ActionKind]] = frozenset(
        {ActionKind.STOP, ActionKind.WAIT, ActionKind.REQUEUE, ActionKind.WAKE, ActionKind.COMMENT}
    )
    # workspace -> snapshot time until which it is not read (its git timed out)
    _git_backoff: dict[Path, datetime] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    wait_key: Path = DEFAULT_WAIT_KEY  # signs park markers (see park.py); created on first park
    # Hermes status and run of each task in the latest snapshot: a park needs both unchanged
    _seen: dict[str, tuple[str, int | None]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path).expanduser()
        self.human_owners = frozenset(h.strip().casefold() for h in self.human_owners)
        self.base_refs = tuple(self.base_refs)
        if bad := [r for r in self.base_refs if not valid_base_ref(r)]:
            raise ValueError(f"invalid base refs: {bad}")
        self.roles = {k.strip().casefold(): v.strip().casefold() for k, v in self.roles.items()}
        if self.spool is not None:
            self.spool = Path(self.spool).expanduser()
        self.wait_key = Path(self.wait_key).expanduser()

    def role_of(self, profile: str) -> str:
        """The role of a Hermes profile: ``roles``, else the name without a trailing
        ``-<number>``, else the profile itself."""
        key = profile.strip().casefold()
        if key in self.roles:
            return self.roles[key]
        m = _INSTANCE.match(key) if self.role_from_name else None
        return m[1] if m else key

    # --- reading -------------------------------------------------------------------------

    def check(self) -> str | None:
        """``None`` if the board can be read, else why not (for ``kelpie status``)."""
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchall()
        except (HermesError, sqlite3.Error) as exc:
            return str(exc)
        return None

    def board_counts(self) -> tuple[int, int]:
        """(all tasks, tasks with status ``running``); raises HermesError/sqlite3.Error."""
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            running = conn.execute("SELECT COUNT(*) FROM tasks WHERE status = 'running'")
            return int(total), int(running.fetchone()[0])

    def live_workspaces(self) -> list[Path]:
        """Existing worktree directories of tasks that are not finished, sorted, unique."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status NOT IN ('done', 'archived')"
            ).fetchall()
        return sorted({p for row in rows if (p := _workspace(row)) is not None})

    def status_detail(self, now: datetime) -> list[str]:
        """Extra lines for ``kelpie status``: how fresh the hooks spool is."""
        if self.spool is None:
            return ["spool: disabled"]
        if not self.spool.exists():
            return [f"spool: missing ({self.spool})"]
        last = self.last_spool_at()
        if last is None:
            return [f"spool: no readable lines ({self.spool})"]
        return [f"spool: last line {format_age(now - last)} ago ({self.spool})"]

    def last_spool_at(self) -> datetime | None:
        if self.spool is None:
            return None
        try:
            with self.spool.open("rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                tail = f.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return None
        for line in reversed(tail):
            try:
                at = _utc(json.loads(line)["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if at is not None:
                return at
        return None

    def _connect(self) -> _Connection:
        if not self.db_path.is_file():
            raise HermesError(f"Hermes kanban database not found: {self.db_path}")
        try:
            conn = sqlite3.connect(
                self.db_path.resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,  # transactions are explicit (see _Connection)
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA query_only=1")
        except sqlite3.Error as exc:
            raise HermesError(f"cannot open Hermes kanban database {self.db_path}: {exc}") from exc
        return _Connection(conn)

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = now - self.lookback
        spans = self._spans(cutoff)
        beats: dict[str, datetime] = {}
        for span in spans:
            if span.kind is SpanKind.TOOL and span.session_id and span.end is not None:
                beats[span.session_id] = max(span.end, beats.get(span.session_id, span.end))
        try:
            with self._connect() as conn:  # one read transaction: a consistent view
                tasks, owner_ids, rows, waits = self._tasks(conn, cutoff)
                ids = {t.id for t in tasks}
                dependencies = self._dependencies(conn, ids)
                sessions, profiles = self._sessions(conn, ids, rows, cutoff, now, beats)
                sessions = _with_evidence(sessions, _evidence(conn, ids, cutoff))
        except sqlite3.Error as exc:
            raise HermesError(f"cannot read Hermes kanban database {self.db_path}: {exc}") from exc
        if self.git_state:  # outside the read transaction: git may be slow
            sessions, states = self._with_git_state(sessions, tasks, rows, now)
            tasks = tuple(_with_files(t, states.get(t.id)) for t in tasks)
        owners = tuple(
            Owner(
                id=p,
                role=self.role_of(p),
                kind=OwnerKind.HUMAN if p.casefold() in self.human_owners else OwnerKind.AGENT,
            )
            for p in sorted(owner_ids | profiles)
        )
        return Snapshot(
            now=now,
            tasks=tasks,
            owners=owners,
            dependencies=dependencies,
            conditions=waits,
            sessions=tuple(replace(s, source=self.name) for s in sessions),
            spans=spans,
        )

    @property
    def _park_window(self) -> timedelta:
        """How long after issuing a park marker Hermes may record it: one CLI call."""
        return timedelta(seconds=self.cli_timeout)

    def _key(self, create: bool = False) -> bytes | None:
        try:
            return load_key(self.wait_key, create=create)
        except (OSError, ValueError) as exc:
            warn_once((str(self.wait_key), "key"), "hermes: wait key unusable: %s", exc)
            if create:
                raise HermesError(f"wait key {self.wait_key} unusable: {exc}") from exc
            return None

    def _tasks(
        self, conn: _Connection, cutoff: datetime
    ) -> tuple[tuple[Task, ...], set[str], dict[str, sqlite3.Row], tuple[Condition, ...]]:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived'"
            " AND (status != 'done' OR completed_at IS NULL OR completed_at >= ?) ORDER BY id",
            (int(cutoff.timestamp()),),
        ).fetchall()
        latest = self._latest_events(conn, NON_STATUS_EVENTS, exclude=True)
        blocks = self._latest_events(conn, BLOCK_EVENTS)
        parks = self._latest_events(conn, ("scheduled",))
        splits = self._latest_events(conn, ("decomposed",))
        key = self._key() if parks else None
        claimed = self._claimed(conn, rows)
        returns = self._returns(conn, {r["id"] for r in rows})
        reopens = _reopens(conn, {r["id"] for r in rows})
        tasks: list[Task] = []
        owners: set[str] = set()
        known: dict[str, sqlite3.Row] = {}
        waits: list[Condition] = []
        self._seen = {}
        for row in rows:
            status = STATUS_MAP.get(row["status"])
            if status is None:
                log.warning(
                    "hermes: task %s has unknown status %r; skipped", row["id"], row["status"]
                )
                continue
            created = _utc(row["created_at"]) or datetime.fromtimestamp(0, UTC)
            fallback = {
                Status.IN_PROGRESS: _utc(row["started_at"]),
                Status.DONE: _utc(row["completed_at"]),
            }.get(status)
            event = latest.get(row["id"])
            since = _utc(event["created_at"]) if event else None
            since = since or fallback or created
            block = None
            if status is Status.BLOCKED:
                found = blocks.get(row["id"])
                at = _utc(found["created_at"]) if found else None
                current = found if at is not None and at >= since else None
                block = Block(
                    reason=_block_text(row, event, current),
                    since=at if current is not None and at is not None else since,
                )
            self._seen[row["id"]] = (row["status"], _run_id(_col(row, "current_run_id")))
            park = parks.get(row["id"])
            wait = _park_conditions(row["id"], park, key, self._park_window)
            if wait is None and row["status"] == "scheduled" and has_marker(_park_reason(park)):
                warn_once(
                    (row["id"], f"park:{park['created_at'] if park is not None else ''}"),
                    "hermes: task %s is scheduled with a Kelpie park marker that does not "
                    "verify (written by someone else, copied from an earlier park, or the wait "
                    "key %s is missing or changed); Kelpie leaves it alone",
                    row["id"],
                    self.wait_key,
                )
            if wait is not None and park is not None:
                parked_at = _utc(park["created_at"]) or since
                # Any way out of scheduled is a wake: the dashboard moves a task without an
                # ``unblocked`` event.
                woken = row["status"] != "scheduled"
                if row["status"] == "scheduled":  # parked by Kelpie: waiting on its conditions
                    status, since = Status.WAITING, parked_at
                if row["status"] == "scheduled" or (woken and not status.is_terminal):
                    # After a wake the conditions stay, registered before the new status, so
                    # rules ignore them and the GitHub step does not attach them again.
                    waits.extend(
                        Condition(
                            cid, kind, task_id=row["id"], params=params, registered_at=parked_at
                        )
                        for cid, kind, params in wait
                    )
            assignee = (row["assignee"] or "").strip() or None
            if assignee:
                owners.add(assignee)
            # The assignee only routes a task (its role, ``assignee``); the profile of a live
            # run that claims it owns it. A person assigned a task owns it outright (human
            # tasks have no runs).
            human = assignee is not None and assignee.casefold() in self.human_owners
            owner = assignee if human else claimed.get(row["id"])
            known[row["id"]] = row
            tasks.append(
                Task(
                    id=row["id"],
                    title=row["title"],
                    status=status,
                    status_since=since,
                    role=self.role_of(assignee) if assignee else None,
                    owner_id=owner,
                    assignee=assignee,
                    source=self.name,
                    block=block,
                    body=None if status.is_terminal else _body(row),
                    branch=str(_col(row, "branch_name") or "").strip() or None,
                    subtasks=_subtasks(splits.get(row["id"])),
                    prework=status is Status.BACKLOG and row["status"] in self.prework_statuses,
                    platform_status=row["status"],
                    returns=returns.get(row["id"], ()),
                    reopened_at=reopens.get(row["id"], ()),
                )
            )
        return tuple(tasks), owners, known, tuple(waits)

    @staticmethod
    def _returns(conn: _Connection, ids: set[str]) -> dict[str, tuple[Return, ...]]:
        """Every time a task on the board was sent back, whatever its age (a review loop is
        the task's history, not the last ``lookback``): a reviewer's ``changes_requested``
        (with its reason) and a reopened review (``review_reopened``), both category
        ``review``."""
        rows = conn.execute(
            "SELECT task_id, kind, payload, created_at FROM task_events"
            " WHERE kind IN ('changes_requested', 'review_reopened')"
            " ORDER BY task_id, created_at, id"
        ).fetchall()
        found: dict[str, list[Return]] = {}
        for r in rows:
            if r["task_id"] not in ids:
                continue
            at = _utc(r["created_at"])
            if at is None:
                continue
            reason = _payload(r["payload"]).get("reason")
            text = reason.strip() if isinstance(reason, str) and reason.strip() else None
            fallback = r["kind"].replace("_", " ")  # "changes requested", "review reopened"
            found.setdefault(r["task_id"], []).append(
                Return(at, (text or fallback)[:MAX_RETURN_REASON], "review")
            )
        return {k: tuple(v) for k, v in found.items()}

    @staticmethod
    def _claimed(conn: _Connection, rows: Sequence[sqlite3.Row]) -> dict[str, str]:
        """Tasks a live run claims (the task's ``current_run_id`` names a run of the task
        that has not ended), with the run's profile, else the assignee. An expired claim
        still counts until Hermes reclaims the task and ends the run: whether its worker is
        alive is the sessions' business (heartbeats), not ownership. Older schemas without
        ``current_run_id``: running tasks, by their assignee."""
        if not rows or "current_run_id" not in rows[0].keys():  # noqa: SIM118
            return {
                r["id"]: a
                for r in rows
                if r["status"] == "running" and (a := (r["assignee"] or "").strip())
            }
        found = conn.execute(
            "SELECT t.id, r.profile, t.assignee FROM tasks t"
            " JOIN task_runs r ON r.id = t.current_run_id AND r.task_id = t.id"
            " WHERE r.ended_at IS NULL"
        ).fetchall()
        claimed: dict[str, str] = {}
        for r in found:
            who = (r["profile"] or "").strip() or (r["assignee"] or "").strip()
            if who:
                claimed[r["id"]] = who
        return claimed

    @staticmethod
    def _latest_events(
        conn: _Connection, kinds: Sequence[str], exclude: bool = False
    ) -> dict[str, sqlite3.Row]:
        op = "NOT IN" if exclude else "IN"
        rows = conn.execute(
            "SELECT task_id, kind, payload, created_at FROM ("
            " SELECT *, ROW_NUMBER() OVER ("
            "  PARTITION BY task_id ORDER BY created_at DESC, id DESC) AS rn"
            f" FROM task_events WHERE kind {op} ({_in_clause(kinds)})) WHERE rn = 1",
            tuple(kinds),
        ).fetchall()
        return {r["task_id"]: r for r in rows}

    @staticmethod
    def _dependencies(conn: _Connection, ids: set[str]) -> tuple[Dependency, ...]:
        rows = conn.execute(
            "SELECT parent_id, child_id FROM task_links ORDER BY child_id, parent_id"
        ).fetchall()
        # Archived parents count as satisfied in Hermes, so links to them are dropped.
        return tuple(
            Dependency(task_id=r["child_id"], depends_on=r["parent_id"])
            for r in rows
            if r["child_id"] in ids and r["parent_id"] in ids
        )

    @staticmethod
    def _sessions(
        conn: _Connection,
        ids: set[str],
        tasks: Mapping[str, sqlite3.Row],
        cutoff: datetime,
        now: datetime,
        spool_beats: Mapping[str, datetime],
    ) -> tuple[tuple[Session, ...], set[str]]:
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE ended_at IS NULL OR ended_at >= ? ORDER BY id",
            (int(cutoff.timestamp()),),
        ).fetchall()
        sessions: list[Session] = []
        profiles: set[str] = set()
        for r in rows:
            started = _utc(r["started_at"])
            if r["task_id"] not in ids or started is None:
                continue
            if r["outcome"] in ("scheduled", "completed") and r["ended_at"] == r["started_at"]:
                # A zero-length run Hermes synthesizes when a task with no active run is
                # scheduled (a park) or completed (a person approving a review): nobody
                # worked, its profile must not look active, nor the closure an agent's.
                continue
            task = tasks[r["task_id"]]
            profile = (r["profile"] or "").strip()
            if profile:
                profiles.add(profile)
            outcome = r["outcome"]
            reason = " ".join(str(x).strip() for x in (outcome, r["error"]) if x and str(x).strip())
            session_id = f"hermes-run-{r['id']}"
            beats = [_utc(r["last_heartbeat_at"]), spool_beats.get(session_id)]
            if _col(task, "current_run_id") == r["id"]:
                beats.append(_utc(_col(task, "last_heartbeat_at")))
                expires = _col(task, "claim_expires")
                if r["ended_at"] is None and expires is not None and expires > now.timestamp():
                    beats.append(now)  # a valid claim counts as a heartbeat
            sessions.append(
                Session(
                    id=session_id,
                    task_id=r["task_id"],
                    owner_id=profile or (task["assignee"] or "").strip() or "unknown",
                    started_at=started,
                    ended_at=_utc(r["ended_at"]),
                    final_status=OUTCOME_STATUS.get(outcome) if outcome else None,
                    exit_reason=reason[:300] or None,
                    last_heartbeat=max((b for b in beats if b is not None), default=None),
                    summary=str(_col(r, "summary") or "").strip() or None,
                )
            )
        return tuple(sessions), profiles

    def _with_git_state(
        self,
        sessions: tuple[Session, ...],
        tasks: Sequence[Task],
        rows: Mapping[str, sqlite3.Row],
        now: datetime,
    ) -> tuple[tuple[Session, ...], dict[str, GitState]]:
        """Read the workspace of every unfinished task, attach its git state to the task's
        latest run (if one is in the snapshot), and return the state per task id (for
        ``Task.files`` and ``Task.repo``).

        Git describes the worktree as it is now, so older runs of a task get nothing, and
        neither do finished tasks: their worktree says nothing about a session any more. A
        task in review with no run inside ``lookback`` still gets its files: open work is
        what new work conflicts with.
        """
        latest: dict[str, Session] = {}
        for s in sessions:  # run ids only grow, and sessions come in run id order
            latest[s.task_id] = s
        users: dict[Path, list[tuple[str, str | None]]] = {}
        for t in tasks:
            if t.status.is_terminal:
                continue
            row = rows[t.id]
            if (path := _workspace(row)) is not None:
                branch = str(_col(row, "branch_name") or "").strip() or None
                users.setdefault(path, []).append((t.id, branch))
        self._git_backoff = {p: t for p, t in self._git_backoff.items() if t > now}
        states: dict[str, GitState] = {}
        jobs: dict[Path, tuple[str, str | None]] = {}
        for path, found in users.items():
            if len(found) > 1:
                warn_once(
                    (str(path), "shared"),
                    "hermes: workspace %s is used by tasks %s; no git state for them",
                    path,
                    ", ".join(sorted(tid for tid, _ in found)),
                )
                states.update((tid, GitState(branch=b)) for tid, b in found)
            elif path in self._git_backoff:
                log.debug(
                    "hermes: %s skipped until %s (git timed out)", path, self._git_backoff[path]
                )
                states.update((tid, GitState(branch=b)) for tid, b in found)
            else:
                jobs[path] = found[0]
        if jobs:
            states.update(self._read_git(jobs, now))
        attached: dict[str, Session] = {}
        for task_id, s in latest.items():
            if (state := states.get(task_id)) is not None:
                attached[s.id] = replace(
                    s,
                    last_commit=state.last_commit,
                    last_commit_at=state.last_commit_at,
                    branch=state.branch,
                    changed_files=state.changed_files,
                    dirty_worktree=state.dirty,
                )
        return tuple(attached.get(s.id, s) for s in sessions), states

    def _read_git(
        self, jobs: Mapping[Path, tuple[str, str | None]], now: datetime
    ) -> dict[str, GitState]:
        """Git state per task id; each path read once, all within the snapshot's budget."""
        timeout = self.git_timeout.total_seconds()
        deadline = time.monotonic() + GIT_BUDGET_FACTOR * timeout
        problem = probe_git(timeout)
        if problem is not None:
            self._report(None, problem)
            return {tid: GitState(branch=b, problem=problem) for tid, b in jobs.values()}
        with ThreadPoolExecutor(GIT_WORKERS, thread_name_prefix="kelpie-git") as pool:
            futures = {
                path: pool.submit(
                    read_git_state,
                    path,
                    timeout,
                    branch=b,
                    deadline=deadline,
                    base_refs=self.base_refs,
                )
                for path, (_, b) in jobs.items()
            }
        states: dict[str, GitState] = {}
        for path, future in futures.items():
            task_id, branch = jobs[path]
            try:
                state = future.result()
            except Exception:  # a bug in parsing must not fail the snapshot
                log.exception("hermes: reading git state of %s failed", path)
                state = GitState(branch=branch, problem=Problem.FAILED)
            if state.problem is Problem.TIMEOUT:
                self._git_backoff[path] = now + GIT_BACKOFF
            if state.paths_truncated:
                warn_once(
                    (str(path), "truncated"),
                    "hermes: %s changes more files than Kelpie reads; only the first are compared",
                    path,
                )
            self._report(path, state.problem)
            states[task_id] = state
        return states

    def _report(self, path: Path | None, problem: Problem | None) -> None:
        timeout = self.git_timeout.total_seconds()
        if problem is Problem.NO_GIT:
            warn_once(("", problem), "hermes: git not found on PATH; no git state is read")
        elif problem is Problem.TIMEOUT and path is None:
            warn_once(("", problem), "hermes: `git version` timed out after %ss", timeout)
        elif problem is Problem.TIMEOUT:
            warn_once(
                (str(path), problem),
                "hermes: git timed out after %ss in %s; skipping it for %s",
                timeout,
                path,
                GIT_BACKOFF,
            )
        elif problem is Problem.UNSAFE:
            warn_once(
                (str(path), problem),
                "hermes: %s has a git filter driver whose name cannot be overridden; not read",
                path,
            )
        elif problem is Problem.LAYOUT:
            warn_once(
                (str(path), problem),
                "hermes: %s has a .git layout Kelpie does not read (not an ordinary checkout or"
                " a linked worktree pointing back at it); no git state",
                path,
            )
        elif problem is Problem.BUDGET:
            warn_once(
                ("", problem),
                "hermes: git state of %s not read: the snapshot's git budget (%ss) is used up",
                path,
                GIT_BUDGET_FACTOR * timeout,
            )
        elif problem is Problem.FAILED:  # git's stderr is logged at debug level
            log.debug("hermes: no git state for %s (git failed)", path)

    # --- hooks spool ---------------------------------------------------------------------

    def _spool_lines(self, cutoff: datetime) -> Iterator[str]:
        if self.spool is None:
            return
        rotated = self.spool.with_name(self.spool.name + ".1")
        for path in (rotated, self.spool):
            try:
                if path is rotated and path.stat().st_mtime < cutoff.timestamp():
                    continue  # nothing in it can be inside the lookback
                with path.open(encoding="utf-8", errors="replace") as f:
                    yield from f
            except OSError:
                continue

    def _spans(self, cutoff: datetime) -> tuple[Span, ...]:
        events = (_event_from_line(line, cutoff) for line in self._spool_lines(cutoff))
        return spans_from_hook_events((e for e in events if e is not None), prefix="hermes")

    # --- PlatformAdapter -----------------------------------------------------------------

    def list_tasks(self) -> Sequence[Task]:
        return self.snapshot(datetime.now(UTC)).tasks

    def get_task(self, task_id: str) -> Task | None:
        return next((t for t in self.list_tasks() if t.id == task_id), None)

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return ()

    def stop(self, session_id: str, reason: str) -> str | ActionResult:
        """Stop a run: the task is blocked (``needs_input``) until a human unblocks it.

        Idempotent: a run that has already ended, or whose task is no longer running it, is
        left alone. Raises :class:`HermesError` for an unknown or foreign session id and when
        Hermes refuses or the task is still running afterwards. Returns what the stop means on
        Hermes, for the human told about it, or ``NOTHING_TO_DO`` when the run was over.
        """
        rid = run_id(session_id)
        if rid is None:
            raise HermesError(f"not a Hermes run session: {session_id!r}", "not a Hermes run")
        state = self._run_state(rid)
        if state is None:
            raise HermesError(f"unknown Hermes run: {session_id}", "the run is not on the board")
        task_id, active = state
        if not active:
            log.info("run %s of task %s is not active; nothing to stop", rid, task_id)
            return ActionResult.NOTHING_TO_DO
        try:
            block_task(
                self.cli,
                self.db_path,
                task_id,
                rid,
                stop_comment(task_id, reason),
                timeout=self.cli_timeout,
            )
        except HermesCliError as exc:
            # The run may have ended on its own between our read and the command.
            if self._run_state(rid) == (task_id, False):
                return ActionResult.NOTHING_TO_DO
            raise HermesError(str(exc), exc.public_reason) from exc
        if self._run_state(rid) != (task_id, False):
            message = f"task {task_id} is still running after its block"
            raise HermesError(message, message)
        return STOP_DONE.format(task=task_id)

    def _run_state(self, rid: int) -> tuple[str, bool] | None:
        """``(task id, run is the task's active run)`` for a run, ``None`` if it is unknown."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT r.task_id, r.ended_at, t.status, t.current_run_id FROM task_runs r"
                " LEFT JOIN tasks t ON t.id = r.task_id WHERE r.id = ?",
                (rid,),
            ).fetchone()
        if row is None:
            return None
        active = (
            row["ended_at"] is None and row["status"] == "running" and row["current_run_id"] == rid
        )
        return str(row["task_id"]), active

    # --- park, wake, comment (see kelpie.adapters.hermes.park) ----------------------------

    def _task_state(self, task_id: str) -> _ParkState | None:
        """Hermes' status and run of a task and whether its latest park is Kelpie's, or
        ``None`` for an unknown task."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            park = conn.execute(
                "SELECT payload, created_at FROM task_events WHERE task_id = ?"
                " AND kind = 'scheduled' ORDER BY created_at DESC, id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        ours = _park_conditions(task_id, park, self._key(), self._park_window) is not None
        status = str(row["status"])
        return _ParkState(status, _run_id(row["current_run_id"]), ours, status != "scheduled")

    def wait(self, task_id: str, conditions: Sequence[Condition], reason: str) -> None:
        """Park a ready or running task in Hermes ``scheduled`` with the conditions in the
        reason, if Hermes still shows the status and run the snapshot showed. A running task
        is pinned to that run. Raises ``UnsupportedAction`` for any other status and
        :class:`HermesError` when the task moved or Hermes refuses; a park Kelpie cannot
        read back is undone."""
        state = self._task_state(task_id)
        if state is None:
            raise HermesError(f"unknown Hermes task: {task_id}")
        seen = self._seen.get(task_id)
        if seen is None:  # not in the snapshot the rule saw: nothing to check against
            raise HermesError(f"task {task_id} was not in the last snapshot; not parked")
        if seen != (state.status, state.run):
            raise HermesError(
                f"task {task_id} moved since the snapshot ({seen[0]} run {seen[1]} -> "
                f"{state.status} run {state.run}); not parked"
            )
        if state.status == "running" and state.run is not None:
            pin: int | None = state.run
        elif state.status == "ready":
            pin = None  # no run to pin: Hermes has no "still ready" guard (see park.py)
        else:
            raise UnsupportedAction(f"cannot park Hermes task {task_id} in status {state.status!r}")
        try:
            text = encode_wait(reason, conditions, task_id, self._wait_key(), datetime.now(UTC))
            run_kanban(
                self.cli,
                self.db_path,
                schedule_args(task_id, text),
                timeout=self.cli_timeout,
                task_id=task_id if pin is not None else None,
                rid=pin,
            )
        except HermesCliError as exc:
            raise HermesError(str(exc)) from exc
        after = self._task_state(task_id)
        if after is not None and after.status == "scheduled" and after.ours:
            return
        if after is not None and after.status == "scheduled":
            self._undo_park(task_id)
        raise HermesError(f"task {task_id} is not parked after hermes kanban schedule")

    def _undo_park(self, task_id: str) -> None:
        """Wake a task Kelpie just scheduled but cannot read back as its park: left
        scheduled, nothing would ever wake it."""
        log.warning("hermes: park of %s could not be read back; waking it again", task_id)
        try:
            run_kanban(
                self.cli,
                self.db_path,
                unblock_args(task_id, "Kelpie could not record its park; woken again"),
                timeout=self.cli_timeout,
            )
        except HermesCliError as exc:
            raise HermesError(
                f"task {task_id} is scheduled but Kelpie cannot read back its park, and "
                f"waking it failed: {exc}; unblock it by hand"
            ) from exc

    def _wait_key(self) -> bytes:
        key = self._key(create=True)
        if key is None:  # pragma: no cover - load_key creates it or raises
            raise HermesError(f"wait key {self.wait_key} could not be created")
        return key

    def requeue(self, task_id: str, reason: str) -> None:
        """Wake a task Kelpie parked (``hermes kanban unblock``). Hermes keeps its assignee
        and lands it in ready, or todo while a parent is open. A Kelpie park already woken
        is left alone; anything else is not supported on Hermes (``UnsupportedAction``)."""
        state = self._task_state(task_id)
        if state is None:
            raise HermesError(f"unknown Hermes task: {task_id}")
        if state.status != "scheduled":
            if state.ours and state.woken:
                log.info("hermes: task %s is already awake (%s)", task_id, state.status)
                return
            raise UnsupportedAction(
                f"Hermes task {task_id} is {state.status}, not parked by Kelpie; requeue it by hand"
            )
        if not state.ours:
            raise UnsupportedAction(
                f"Hermes task {task_id} was not parked by Kelpie; requeue it by hand"
            )
        try:
            run_kanban(
                self.cli, self.db_path, unblock_args(task_id, reason), timeout=self.cli_timeout
            )
        except HermesCliError as exc:
            if (after := self._task_state(task_id)) is not None and after.status != "scheduled":
                return  # woken in between (by a human): nothing left to do
            raise HermesError(str(exc)) from exc

    def unblock(self, task_id: str, reason: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def comment(self, task_id: str, body: str) -> None:
        """``hermes kanban comment --author kelpie``; PR URLs are rewritten (park.py)."""
        try:
            run_kanban(
                self.cli, self.db_path, comment_args(task_id, body), timeout=self.cli_timeout
            )
        except HermesCliError as exc:
            raise HermesError(str(exc)) from exc

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)


def _event_from_line(line: str, cutoff: datetime) -> HookEvent | None:
    """One spool line to a hook event; malformed, foreign or old lines give ``None``."""
    try:
        rec = json.loads(line)
        if not isinstance(rec, dict) or rec.get("type") not in (TOOL, SESSION_END):
            return None
        at = _utc(rec["at"])
        if at is None or at < cutoff:
            return None
        duration = rec.get("duration_ms")
        millis = duration if isinstance(duration, int | float) and duration >= 0 else 0
        run_id = rec.get("run_id")
        task_id = rec.get("task_id")
        tool = rec.get("tool")
        call_id = rec.get("tool_call_id")
        attributes: dict[str, Any] = {}
        if isinstance(rec.get("command"), str):
            attributes["command"] = rec["command"]
        chars = rec.get("output_chars")
        if isinstance(chars, int) and not isinstance(chars, bool) and chars >= 0:
            attributes[OUTPUT_CHARS_ATTRIBUTE] = chars
        return HookEvent(
            kind=rec["type"],
            at=at,
            key=hashlib.sha256(line.strip().encode("utf-8", "replace")).hexdigest()[:16],
            session_id=f"hermes-run-{run_id}" if run_id else None,
            task_id=str(task_id) if task_id else None,
            tool=str(tool) if tool else None,
            args_hash=str(rec["args_hash"]) if rec.get("args_hash") else None,
            duration=timedelta(milliseconds=millis),
            outcome=str(rec["outcome"]) if rec.get("outcome") else None,
            tool_call_id=str(call_id) if call_id else None,
            attributes=attributes,
        )
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


class _Connection:
    """A read transaction on a read-only connection, closed by ``with``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> _Connection:
        try:
            self._conn.execute("BEGIN")
        except sqlite3.Error:
            self._conn.close()
            raise
        return self

    def __exit__(self, exc_type: object, *exc: object) -> None:
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
        finally:
            self._conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)
