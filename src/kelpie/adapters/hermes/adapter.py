"""Hermes platform adapter: read side.

Describes a Hermes farm to Kelpie from two sources:

- the Kanban SQLite database (``~/.hermes/kanban.db``), opened strictly read-only, one
  short-lived connection per snapshot (Hermes writes to it concurrently, WAL mode);
- the hooks spool written by the Hermes plugin (:mod:`kelpie.adapters.hermes.plugin`),
  turned into task, session and tool spans by
  :func:`kelpie.traces.hooks.spans_from_hook_events` (a Hermes run is a session,
  ``hermes-run-<id>``). When Hermes also exports OTLP spans for the same calls, the app
  keeps the OTLP ones, completed from the hooks (:func:`kelpie.traces.hooks.merge_hook_spans`).

Actions. Only ``stop`` (``SupportsStop``) is implemented, by asking Hermes to block the task
through its own CLI; see :mod:`kelpie.adapters.hermes.stop` for the mechanism. Requeue,
unblock, comment and WIP commit are not implemented yet: rules that need them run in
``observe`` or ``suggest`` mode.

Status mapping (task ``status`` -> :class:`~kelpie.core.model.Status`)::

    triage, todo, scheduled -> BACKLOG   (``scheduled`` is a timed queue, not a Kelpie wait)
    ready                   -> READY
    running                 -> IN_PROGRESS
    blocked                 -> BLOCKED
    review                  -> REVIEW
    done                    -> DONE
    archived                -> (skipped)

``body`` is ``tasks.body`` (the description; none for finished tasks), at most ``MAX_BODY``
characters; the dispatch gate's patterns read it with the title. ``role`` is the assignee
profile: Hermes routes work by profile, so a profile is both the owner and its role.
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
(``last_commit``, ``last_commit_at``, ``branch``, ``changed_files``, ``dirty_worktree``) come
from the workspace as it is now, so they go only on the latest run of a task that is not
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
import sqlite3
import time
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.hermes.git_state import (
    DEFAULT_TIMEOUT,
    GitState,
    Problem,
    probe_git,
    read_git_state,
    warn_once,
)
from kelpie.adapters.hermes.stop import (
    CLI_TIMEOUT,
    DEFAULT_CLI,
    HermesCliError,
    block_task,
    run_id,
    stop_comment,
)
from kelpie.core.model import (
    Block,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.core.ports import PlatformEvent
from kelpie.traces.hooks import SESSION_END, TOOL, HookEvent, spans_from_hook_events
from kelpie.traces.semconv import OUTPUT_CHARS_ATTRIBUTE

log = logging.getLogger(__name__)

DEFAULT_DB = Path("~/.hermes/kanban.db")
DEFAULT_SPOOL = Path("~/.hermes/kelpie/spool.jsonl")
DEFAULT_LOOKBACK = timedelta(hours=24)
DEFAULT_GIT_TIMEOUT = timedelta(seconds=DEFAULT_TIMEOUT)
BUSY_TIMEOUT_MS = 5000
MAX_BODY = 8000  # characters of a task's description kept in the snapshot
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
)
BLOCK_EVENTS = ("blocked", "auto_blocked")
# Hermes block kinds that wait for an operator (``dependency`` waits in ``todo``).
TYPED_BLOCKS = ("needs_input", "capability", "transient")


class HermesError(RuntimeError):
    """The Hermes board cannot be read."""


def _utc(epoch: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(epoch), UTC) if epoch is not None else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _in_clause(values: Sequence[str]) -> str:
    return ",".join("?" * len(values))


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


@dataclass(slots=True)
class HermesAdapter:
    db_path: Path = DEFAULT_DB
    spool: Path | None = DEFAULT_SPOOL  # None: no hook data, no spans
    lookback: timedelta = DEFAULT_LOOKBACK
    git_state: bool = True  # read the git state of the workspaces of live tasks
    git_timeout: timedelta = DEFAULT_GIT_TIMEOUT  # per git call
    cli: str = DEFAULT_CLI  # the Hermes executable that carries out ``stop``
    cli_timeout: float = CLI_TIMEOUT  # seconds; a stop runs inside the control loop tick
    # Assignees that are people, not agents (Hermes lowercases profile names).
    human_owners: Collection[str] = ()
    name: str = "hermes"
    # workspace -> snapshot time until which it is not read (its git timed out)
    _git_backoff: dict[Path, datetime] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path).expanduser()
        self.human_owners = frozenset(h.strip().casefold() for h in self.human_owners)
        if self.spool is not None:
            self.spool = Path(self.spool).expanduser()

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
                tasks, owner_ids, rows = self._tasks(conn, cutoff)
                ids = {t.id for t in tasks}
                dependencies = self._dependencies(conn, ids)
                sessions, profiles = self._sessions(conn, ids, rows, cutoff, now, beats)
        except sqlite3.Error as exc:
            raise HermesError(f"cannot read Hermes kanban database {self.db_path}: {exc}") from exc
        if self.git_state:  # outside the read transaction: git may be slow
            sessions = self._with_git_state(sessions, tasks, rows, now)
        owners = tuple(
            Owner(
                id=p,
                role=p,
                kind=OwnerKind.HUMAN if p.casefold() in self.human_owners else OwnerKind.AGENT,
            )
            for p in sorted(owner_ids | profiles)
        )
        return Snapshot(
            now=now,
            tasks=tasks,
            owners=owners,
            dependencies=dependencies,
            sessions=sessions,
            spans=spans,
        )

    def _tasks(
        self, conn: _Connection, cutoff: datetime
    ) -> tuple[tuple[Task, ...], set[str], dict[str, sqlite3.Row]]:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived'"
            " AND (status != 'done' OR completed_at IS NULL OR completed_at >= ?) ORDER BY id",
            (int(cutoff.timestamp()),),
        ).fetchall()
        latest = self._latest_events(conn, NON_STATUS_EVENTS, exclude=True)
        blocks = self._latest_events(conn, BLOCK_EVENTS)
        tasks: list[Task] = []
        owners: set[str] = set()
        known: dict[str, sqlite3.Row] = {}
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
            assignee = (row["assignee"] or "").strip() or None
            if assignee:
                owners.add(assignee)
            known[row["id"]] = row
            tasks.append(
                Task(
                    id=row["id"],
                    title=row["title"],
                    status=status,
                    status_since=since,
                    role=assignee,
                    owner_id=assignee,
                    source=self.name,
                    block=block,
                    body=None if status.is_terminal else _body(row),
                )
            )
        return tuple(tasks), owners, known

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
    ) -> tuple[Session, ...]:
        """Attach the workspace's git state to the latest run of each live task.

        Git describes the worktree as it is now, so older runs of a task get nothing, and
        neither do finished tasks: their worktree says nothing about a session any more.
        """
        live = {t.id for t in tasks if not t.status.is_terminal}
        latest: dict[str, Session] = {}
        for s in sessions:  # run ids only grow, and sessions come in run id order
            if s.task_id in live:
                latest[s.task_id] = s
        users: dict[Path, list[tuple[Session, str | None]]] = {}
        for task_id, s in latest.items():
            row = rows[task_id]
            if (path := _workspace(row)) is not None:
                branch = str(_col(row, "branch_name") or "").strip() or None
                users.setdefault(path, []).append((s, branch))
        self._git_backoff = {p: t for p, t in self._git_backoff.items() if t > now}
        states: dict[str, GitState] = {}
        jobs: dict[Path, tuple[Session, str | None]] = {}
        for path, found in users.items():
            if len(found) > 1:
                warn_once(
                    (str(path), "shared"),
                    "hermes: workspace %s is used by tasks %s; no git state for them",
                    path,
                    ", ".join(sorted(s.task_id for s, _ in found)),
                )
                states.update((s.id, GitState(branch=b)) for s, b in found)
            elif path in self._git_backoff:
                log.debug(
                    "hermes: %s skipped until %s (git timed out)", path, self._git_backoff[path]
                )
                states.update((s.id, GitState(branch=b)) for s, b in found)
            else:
                jobs[path] = found[0]
        if jobs:
            states.update(self._read_git(jobs, now))
        attached: dict[str, Session] = {}
        for s in sessions:
            if (state := states.get(s.id)) is not None:
                attached[s.id] = replace(
                    s,
                    last_commit=state.last_commit,
                    last_commit_at=state.last_commit_at,
                    branch=state.branch,
                    changed_files=state.changed_files,
                    dirty_worktree=state.dirty,
                )
        return tuple(attached.get(s.id, s) for s in sessions)

    def _read_git(
        self, jobs: Mapping[Path, tuple[Session, str | None]], now: datetime
    ) -> dict[str, GitState]:
        """Git state per session id; each path read once, all within the snapshot's budget."""
        timeout = self.git_timeout.total_seconds()
        deadline = time.monotonic() + GIT_BUDGET_FACTOR * timeout
        problem = probe_git(timeout)
        if problem is not None:
            self._report(None, problem)
            return {s.id: GitState(branch=b, problem=problem) for s, b in jobs.values()}
        with ThreadPoolExecutor(GIT_WORKERS, thread_name_prefix="kelpie-git") as pool:
            futures = {
                path: pool.submit(read_git_state, path, timeout, branch=b, deadline=deadline)
                for path, (_, b) in jobs.items()
            }
        states: dict[str, GitState] = {}
        for path, future in futures.items():
            session, branch = jobs[path]
            try:
                state = future.result()
            except Exception:  # a bug in parsing must not fail the snapshot
                log.exception("hermes: reading git state of %s failed", path)
                state = GitState(branch=branch, problem=Problem.FAILED)
            if state.problem is Problem.TIMEOUT:
                self._git_backoff[path] = now + GIT_BACKOFF
            self._report(path, state.problem)
            states[session.id] = state
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

    def stop(self, session_id: str, reason: str) -> None:
        """Stop a run: the task is blocked (``needs_input``) until a human unblocks it.

        Idempotent: a run that has already ended, or whose task is no longer running it, is
        left alone. Raises :class:`HermesError` for an unknown or foreign session id and when
        Hermes refuses or the task is still running afterwards.
        """
        rid = run_id(session_id)
        if rid is None:
            raise HermesError(f"not a Hermes run session: {session_id!r}")
        state = self._run_state(rid)
        if state is None:
            raise HermesError(f"unknown Hermes run: {session_id}")
        task_id, active = state
        if not active:
            log.info("run %s of task %s is not active; nothing to stop", rid, task_id)
            return
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
                return
            raise HermesError(str(exc)) from exc
        if self._run_state(rid) != (task_id, False):
            raise HermesError(f"task {task_id} is still running after its block")

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

    def requeue(self, task_id: str, reason: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def unblock(self, task_id: str, reason: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

    def comment(self, task_id: str, body: str) -> None:
        raise NotImplementedError(NOT_IMPLEMENTED)

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
