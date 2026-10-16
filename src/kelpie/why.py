"""``kelpie why <task>``: why is this task not moving?

Read-only, like ``kelpie status`` and ``kelpie doctor``: one snapshot is built the way the
control loop builds it (platform adapters, the connector steps that attach conditions, and
the span store opened read-only), every loaded rule is evaluated against it (rules are pure,
so this is observe mode whatever their configured mode), and the explanation is printed.
Offline by default: only the connector steps that read nothing but the board run (task
status and human task conditions); ``--refresh`` adds the network ones (GitHub PR, CI and
block conditions), as one tick of ``kelpie serve`` would, and a note says when they were
skipped.

- the task's status and how long it has been there, owner, labels (held ones flagged),
  block and sessions;
- its linked conditions and their state;
- what it waits for, link by link up to the first dead end (``kelpie.rules._chains``, the
  same graph rule 3 uses) and what waits behind it;
- the rule findings about it: actions on the task, or naming it in their params (a chain,
  a list of dependents), with each rule's configured mode;
- its open alert states and its latest ledger entries, read from the database without
  writing to it.

Nothing is written: no ledger entry, no alert state, no span attribution. Exit codes: 0; 1
when the task is not on the board; 2 for a config error; 3 when a platform adapter failed to
read its board (the task may be there: nothing is explained). ``--json`` always prints one
object: the explanation (``schema_version`` 1; at most ``JSON_SESSIONS`` sessions, newest
first, with ``sessions_total``), or ``{"error": "not_found" | "adapter_failed", ...}``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.app import (
    LoadedRules,
    Settings,
    build_adapters,
    connector_steps,
    load_rules,
    merged_snapshot,
    utc_now,
)
from kelpie.core.actions import Action
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry
from kelpie.core.model import Condition, Session, Snapshot, Task
from kelpie.core.spans import SpanStore
from kelpie.core.states import OpenState
from kelpie.core.storage import (
    SchemaOutdated,
    SqliteSpanStore,
    ledger_entry,
    open_readonly,
    open_state,
)
from kelpie.metrics import Metrics
from kelpie.rules._chains import BlockerGraph, Chain
from kelpie.rules._tasks import DEFAULT_STALE_AFTER, is_live, last_seen, linked_conditions
from kelpie.traces.store import SpanWindow

log = logging.getLogger(__name__)

DEFAULT_ENTRIES = 10
SCHEMA_VERSION = 1  # of the --json object
TEXT_SESSIONS = 3  # sessions shown in the text view, newest first
JSON_SESSIONS = 10  # ...and in --json


@dataclass(frozen=True, slots=True)
class SnapshotRead:
    """One read-only snapshot, and what went wrong or was left out while building it."""

    snapshot: Snapshot
    adapter_failures: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Failure:
    """Why there is no explanation: ``error`` is ``not_found`` or ``adapter_failed``."""

    error: str
    message: str
    problems: list[str]
    code: int


@dataclass(frozen=True, slots=True)
class Finding:
    """A rule's action about the task, as it would be emitted on this snapshot."""

    action: Action
    mode: Mode


@dataclass(slots=True)
class Explanation:
    task: Task
    snapshot: Snapshot
    stale_after: timedelta
    conditions: list[Condition]
    missing_condition: str | None
    chains: list[Chain]
    behind: dict[str, tuple[str, ...]]
    findings: list[Finding]
    states: list[OpenState]
    entries: list[LedgerEntry]
    dry_run: bool
    problems: list[str] = field(default_factory=list)
    hold_labels: frozenset[str] = frozenset()


class _Capture(logging.Handler):
    """Collects the errors merged_snapshot logs for a source it leaves out."""

    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def read_snapshot(
    settings: Settings, clock: Callable[[], datetime] | None = None, refresh: bool = False
) -> SnapshotRead:
    """One snapshot as the control loop assembles it, without writing anywhere, and the
    sources that failed (they are left out, as on a tick). Without ``refresh`` no connector
    that calls out over the network runs."""
    clock = clock or utc_now
    metrics = Metrics()
    adapters = build_adapters(settings)
    enrich, _ = connector_steps(settings, metrics, clock, network=refresh)
    problems: list[str] = []
    github = settings.github
    if not refresh and github is not None and github.enabled:
        problems.append(
            "GitHub conditions were not read (offline); conditions and blocks that depend on "
            "them may differ from what kelpie serve sees: pass --refresh"
        )
    store: SpanStore | None = None
    db = settings.db_path
    if settings.traces.enabled and db is not None and db.exists():
        try:
            store = SqliteSpanStore(db, readonly=True)
        except SchemaOutdated as exc:
            problems.append(f"spans left out: {exc}")
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            problems.append(f"spans left out: cannot open the span store {db}: {exc}")
    capture = _Capture()
    app_log = logging.getLogger("kelpie.app")
    app_log.addHandler(capture)
    try:
        traces = settings.traces
        window = (
            SpanWindow(store, traces.window, traces.totals_window, traces.max_snapshot_spans)
            if store is not None
            else None
        )
        budgets = settings.budgets if window is not None else None
        snapshot = merged_snapshot(
            adapters, metrics, clock, window, budgets, remember=False, enrich=enrich
        )()
    finally:
        app_log.removeHandler(capture)
        close = getattr(store, "close", None)
        if callable(close):
            close()
    failed = {n for n, c in metrics.adapter_errors.items() if c}
    failures = [m for m in capture.messages if m.startswith("adapter ")]
    failures += [
        f"adapter {n} failed to snapshot"
        for n in sorted(failed)
        if not any(m.startswith(f"adapter {n} ") for m in failures)
    ]
    problems += [f"connector {n} failed" for n, c in metrics.connector_errors.items() if c]
    if metrics.span_store_errors:
        problems.append("the span store could not be read")
    return SnapshotRead(snapshot, failures, problems)


def _mentions(value: Any, task_id: str) -> bool:
    if isinstance(value, str):
        return value == task_id
    if isinstance(value, list | tuple | set | frozenset):
        return any(_mentions(v, task_id) for v in value)
    if isinstance(value, dict):
        return any(_mentions(v, task_id) for v in value.values())
    return False


def evaluate_rules(
    loaded: LoadedRules, snapshot: Snapshot, task_id: str
) -> tuple[list[Finding], list[str]]:
    """The actions each rule emits about the task on this snapshot. Rules are pure; their
    output is only collected here, never dispatched."""
    modes = {s.name: s.mode for s in loaded.statuses}
    findings: list[Finding] = []
    problems: list[str] = []
    for rule in loaded.rules:
        try:
            actions = list(rule.evaluate(snapshot))
        except Exception as exc:  # the engine logs and skips a failing rule too
            problems.append(f"rule {rule.name} failed: {type(exc).__name__}: {exc}")
            continue
        for a in actions:
            if a.task_id == task_id or _mentions(a.params, task_id):
                findings.append(Finding(a, modes.get(rule.name, Mode.OBSERVE)))
    return findings, problems


def read_history(
    db: Path | None, task_id: str, state_ids: Iterable[str], limit: int
) -> tuple[list[OpenState], list[LedgerEntry], list[str]]:
    """Open alert states and the latest ledger entries for the task, read-only."""
    if db is None:
        return [], [], ["ledger in memory: no history outside kelpie serve"]
    if not db.exists():
        return [], [], [f"no database at {db} yet"]
    try:
        conn = open_readonly(db, 5000)
    except (SchemaOutdated, RuntimeError, sqlite3.Error) as exc:
        return [], [], [f"history left out: {exc}"]
    try:
        ids = sorted(set(state_ids))
        marks = ",".join("?" * len(ids))
        sql = "SELECT * FROM states WHERE closed_at IS NULL AND (task_id = ?"
        sql += f" OR id IN ({marks}))" if ids else ")"
        rows = conn.execute(sql + " ORDER BY opened_at, rowid_", (task_id, *ids)).fetchall()
        states = [open_state(r) for r in rows]
        rows = conn.execute(
            "SELECT * FROM actions WHERE task_id = ? ORDER BY at DESC, id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        entries = [ledger_entry(r) for r in rows]
    except sqlite3.Error as exc:
        return [], [], [f"history left out: {exc}"]
    finally:
        conn.close()
    return states, entries, []


def explain_snapshot(
    loaded: LoadedRules, snapshot: Snapshot, task_id: str, dry_run: bool = True
) -> Explanation | None:
    """The explanation from one snapshot, without history; None when the task is not in it."""
    task = snapshot.task(task_id)
    if task is None:
        return None
    orphan = next((r for r in loaded.rules if r.name == "orphan_status"), None)
    stale_after: timedelta = getattr(orphan, "session_stale_after", DEFAULT_STALE_AFTER)
    conditions, missing = linked_conditions(task, snapshot)
    graph = BlockerGraph(snapshot, stale_after)
    findings, failed = evaluate_rules(loaded, snapshot, task_id)
    return Explanation(
        task=task,
        snapshot=snapshot,
        stale_after=stale_after,
        conditions=conditions,
        missing_condition=missing,
        chains=graph.chains(task_id),
        behind=graph.behind(task_id),
        findings=findings,
        states=[],
        entries=[],
        dry_run=dry_run,
        problems=failed,
        hold_labels=frozenset(getattr(orphan, "hold_labels", frozenset())),
    )


def explain(
    settings: Settings,
    task_id: str,
    entries: int = DEFAULT_ENTRIES,
    clock: Callable[[], datetime] | None = None,
    refresh: bool = False,
) -> Explanation | Failure:
    """Everything ``kelpie why`` shows, or why it cannot be shown."""
    loaded = load_rules(settings.engine)
    read = read_snapshot(settings, clock, refresh)
    if read.adapter_failures:
        return Failure(
            "adapter_failed",
            "a platform adapter could not read its board, so the task cannot be explained",
            [*read.adapter_failures, *read.problems],
            3,
        )
    ex = explain_snapshot(loaded, read.snapshot, task_id, settings.engine.dry_run)
    if ex is None:
        return Failure("not_found", f"task {task_id} is not on the board", read.problems, 1)
    state_ids = [f.action.state for f in ex.findings if f.action.state is not None]
    ex.states, ex.entries, unread = read_history(settings.db_path, task_id, state_ids, entries)
    ex.problems = [*read.problems, *ex.problems, *unread]
    return ex


# -- rendering -------------------------------------------------------------------------------


def _age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def _when(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M UTC") if at.utcoffset() == timedelta(0) else at.isoformat()


def _session(s: Session, now: datetime, stale_after: timedelta) -> str:
    if is_live(s, now, stale_after):
        return f"{s.id} by {s.owner_id}: live, last sign of life {_age(now - last_seen(s))} ago"
    if s.ended_at is None:
        return f"{s.id} by {s.owner_id}: silent for {_age(now - last_seen(s))} (treated as gone)"
    final = f"final status {s.final_status}" if s.final_status else "no final status"
    reason = f", {s.exit_reason}" if s.exit_reason else ""
    return f"{s.id} by {s.owner_id}: ended {_age(now - s.ended_at)} ago, {final}{reason}"


def _condition(c: Condition, now: datetime) -> str:
    state = {True: "met", False: "unmet", None: "unknown"}[c.satisfied]
    extra = []
    if c.params.get("terminal") is True:
        extra.append("can never be met")
    if error := c.params.get("error"):
        extra.append(f"error {error}")
    if (target := c.params.get("task")) is not None:
        extra.append(f"task {target}")
    checked = f"checked {_age(now - c.checked_at)} ago" if c.checked_at else "never checked"
    detail = f" ({', '.join(extra)})" if extra else ""
    return f"{c.id} [{c.kind}]: {state}{detail}, {checked}"


def _chain(chain: Chain, snapshot: Snapshot) -> str:
    path = " -> ".join(chain.path)
    end = chain.end
    if end.dead is not None:
        return f"DEAD END  {path} -> {end.dead.ref}: {end.dead.why} ({end.via})"
    if end.task is not None:  # the chain stops at a task that is not waiting
        target = snapshot.task(end.task)
        status = f"is {target.status}" if target is not None else "is not on the board"
        return f"waits     {path}: {end.task} {status}"
    if end.external is not None:
        return f"waits     {path}: {end.via}"
    return f"waits     {path}: {chain.path[-1]} {end.via}"


def _mode(f: Finding, dry_run: bool) -> str:
    return f"{f.mode}, dry-run" if dry_run and f.mode is not Mode.OBSERVE else str(f.mode)


def render(ex: Explanation) -> str:
    t, now = ex.task, ex.snapshot.now
    owner = ex.snapshot.owner(t.owner_id) if t.owner_id else None
    if t.owner_id is None:
        who = "none"
    elif owner is None:
        who = f"{t.owner_id} (not in the owner directory)"
    else:
        who = f"{owner.id} ({owner.kind}, role {owner.role})"
    held = sorted(ex.hold_labels.intersection(t.labels))
    lines = [
        f"Task {t.id}: {t.title}",
        f"  status   {t.status} for {_age(now - t.status_since)} (since {_when(t.status_since)})",
        f"  owner    {who}; role {t.role or 'none'}" + (f"; source {t.source}" if t.source else ""),
    ]
    if t.labels:
        mark = f" (held: {', '.join(held)}; stop patterns skip it)" if held else ""
        lines.append(f"  labels   {', '.join(t.labels)}{mark}")
    if t.block is not None:
        cond = f", condition {t.block.condition_id}" if t.block.condition_id else ""
        lines.append(f"  block    '{t.block.reason}' for {_age(now - t.block.since)}{cond}")
    sessions = sorted(ex.snapshot.sessions_for(t.id), key=lambda s: s.started_at, reverse=True)
    live = [s for s in sessions if is_live(s, now, ex.stale_after)]
    lines.append(f"  session  {'live' if live else 'none live'}")
    lines += [f"    {_session(s, now, ex.stale_after)}" for s in sessions[:TEXT_SESSIONS]]
    if len(sessions) > TEXT_SESSIONS:
        lines.append(f"    ... and {len(sessions) - TEXT_SESSIONS} older")

    lines.append("Conditions")
    if ex.missing_condition:
        lines.append(f"  {ex.missing_condition}: named by the block, Kelpie cannot see it")
    lines += [f"  {_condition(c, now)}" for c in ex.conditions]
    if not ex.conditions and not ex.missing_condition:
        lines.append("  none")

    lines.append("Waits for")
    lines += [f"  {_chain(c, ex.snapshot)}" for c in ex.chains] or ["  nothing Kelpie can see"]
    if ex.behind:
        ids = sorted(ex.behind)
        lines.append(f"  behind it: {len(ids)} task(s): {', '.join(ids)}")

    lines.append("Rules on this snapshot")
    for f in ex.findings:
        a = f.action
        lines.append(f"  {a.rule} [{_mode(f, ex.dry_run)}] {a.kind} {a.severity}: {a.reason}")
    if not ex.findings:
        lines.append("  no rule acts on this task")

    lines.append("Open alert states")
    for s in ex.states:
        lines.append(
            f"  {s.id} ({s.severity}, since {_when(s.since)}, level {s.level}): {s.summary}"
        )
    if not ex.states:
        lines.append("  none")

    lines.append("Ledger (latest first)")
    for e in ex.entries:
        lines.append(f"  {_when(e.at)} {e.rule} {e.kind} {e.mode} {e.outcome}: {e.reason}")
    if not ex.entries:
        lines.append("  none")
    lines += [f"note: {p}" for p in ex.problems]
    return "\n".join(lines) + "\n"


def as_json(ex: Explanation) -> dict[str, Any]:
    t, now = ex.task, ex.snapshot.now
    sessions = sorted(ex.snapshot.sessions_for(t.id), key=lambda s: s.started_at, reverse=True)
    return {
        "schema_version": SCHEMA_VERSION,
        "task": {
            "id": t.id,
            "title": t.title,
            "status": t.status.value,
            "status_since": t.status_since.isoformat(),
            "age_seconds": int((now - t.status_since).total_seconds()),
            "owner_id": t.owner_id,
            "role": t.role,
            "labels": list(t.labels),
            "held_by": sorted(ex.hold_labels.intersection(t.labels)),
            "block": None
            if t.block is None
            else {
                "reason": t.block.reason,
                "since": t.block.since.isoformat(),
                "condition_id": t.block.condition_id,
            },
        },
        "sessions": [
            {
                "id": s.id,
                "owner_id": s.owner_id,
                "live": is_live(s, now, ex.stale_after),
                "started_at": s.started_at.isoformat(),
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "final_status": s.final_status.value if s.final_status else None,
            }
            for s in sessions[:JSON_SESSIONS]
        ],
        "sessions_total": len(sessions),
        "conditions": [
            {
                "id": c.id,
                "kind": c.kind.value,
                "satisfied": c.satisfied,
                "checked_at": c.checked_at.isoformat() if c.checked_at else None,
                "params": c.params,
            }
            for c in ex.conditions
        ],
        "missing_condition": ex.missing_condition,
        "chains": [
            {
                "path": list(c.path),
                "via": c.end.via,
                "dead_end": None
                if c.end.dead is None
                else {"ref": c.end.dead.ref, "why": c.end.dead.why},
                "external": c.end.external,
            }
            for c in ex.chains
        ],
        "behind": sorted(ex.behind),
        "findings": [
            {
                "rule": f.action.rule,
                "mode": f.mode.value,
                "kind": f.action.kind.value,
                "severity": f.action.severity.value,
                "task_id": f.action.task_id,
                "reason": f.action.reason,
                "state": f.action.state,
                "params": f.action.params,
            }
            for f in ex.findings
        ],
        "states": [
            {
                "id": s.id,
                "rule": s.rule,
                "severity": s.severity.value,
                "since": s.since.isoformat(),
                "opened_at": s.opened_at.isoformat(),
                "level": s.level,
                "summary": s.summary,
            }
            for s in ex.states
        ],
        "ledger": [
            {
                "at": e.at.isoformat(),
                "rule": e.rule,
                "kind": e.kind.value,
                "mode": e.mode.value,
                "outcome": e.outcome.value,
                "reason": e.reason,
            }
            for e in ex.entries
        ],
        "dry_run": ex.dry_run,
        "problems": ex.problems,
    }


def run(
    settings: Settings,
    task_id: str,
    as_json_output: bool = False,
    entries: int = DEFAULT_ENTRIES,
    clock: Callable[[], datetime] | None = None,
    refresh: bool = False,
) -> tuple[str, str, int]:
    """Stdout, stderr and the exit code."""
    ex = explain(settings, task_id, entries, clock, refresh)
    if isinstance(ex, Failure):
        if as_json_output:
            body = {
                "schema_version": SCHEMA_VERSION,
                "error": ex.error,
                "task_id": task_id,
                "message": ex.message,
                "problems": ex.problems,
            }
            return json.dumps(body, indent=2) + "\n", "", ex.code
        lines = [f"kelpie: {ex.message}", *(f"  {p}" for p in ex.problems)]
        return "", "\n".join(lines) + "\n", ex.code
    if as_json_output:
        return json.dumps(as_json(ex), indent=2, default=str) + "\n", "", 0
    return render(ex), "", 0
