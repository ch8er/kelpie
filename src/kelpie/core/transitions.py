"""Task status transitions Kelpie observes itself (#157).

Boards keep no status history (Hermes and the Claude Code task lists report only a task's
current status and since when), so the daily report used to rebuild a task's day from what
the board says *now*. On every tick the control loop compares each task's status with the
last one recorded for it and stores a :class:`StatusChange` only when it differs: the
normalized ``status`` or the platform's own ``platform_status``. A task seen for the first
time gets a row too (``previous`` None), with the board's ``status_since`` as the best known
start. Kelpie never writes to the board.

This lives next to the ledger and the state store, never in rules: rules stay pure and
see only the :class:`~kelpie.core.model.Snapshot`.

**Restarts and gaps.** The last record per task is read back from the store when the
recorder starts, so a restart compares with what was recorded before it and invents no
transition. A change is known only to within a tick, and to within the whole gap while
``kelpie serve`` was down: each row carries ``after``, the last time Kelpie saw the old
status (the previous tick in this process, else the previous record), and ``at``, the tick
that saw the new one. :func:`changed_at` places the change at the board's own
``status_since`` when that falls inside ``(after, at]``, else at ``at``.

A task missing from a snapshot (a board not read this tick, a task archived) records
nothing: absence is not a status.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from kelpie.core.model import Snapshot, Status, Task
from kelpie.core.ports import TransitionStore

log = logging.getLogger(__name__)

# Stored characters of the platform's own status, the board and the source: names, not text.
MAX_NAME = 128


@dataclass(frozen=True, slots=True)
class StatusChange:
    """A task's status as Kelpie observed it change (or first saw it).

    ``at`` is the snapshot time of the tick that saw it; ``since`` the board's own
    ``status_since`` then; ``after`` the last time the previous status was seen (None for
    the first sighting). ``board`` is the task's board when one adapter watches several
    (task ids then carry its prefix), ``source`` the adapter it came from."""

    at: datetime
    task_id: str
    status: Status
    since: datetime
    platform_status: str | None = None
    previous: Status | None = None
    previous_platform: str | None = None
    after: datetime | None = None
    board: str | None = None
    source: str | None = None

    @property
    def first(self) -> bool:
        """True for the first sighting of the task: no transition, only a known start."""
        return self.previous is None


def changed_at(change: StatusChange) -> datetime:
    """When the change most likely happened: the board's ``status_since`` when it lies
    within ``(after, at]`` (the board's own record agrees with what Kelpie saw), else
    ``at`` (the tick that saw it). For a first sighting, ``status_since`` when it is not
    after ``at``."""
    lo = change.after
    if change.since <= change.at and (lo is None or change.since > lo):
        return change.since
    return change.at


def _name(value: str | None) -> str | None:
    return None if value is None else value[:MAX_NAME]


def observe(
    last: Mapping[str, StatusChange],
    tasks: Iterable[Task],
    now: datetime,
    seen: Mapping[str, datetime] | None = None,
) -> list[StatusChange]:
    """The changes of ``tasks`` against ``last`` (the newest record per task id), at
    ``now``; ``seen`` is when each task was last seen with its recorded status (default:
    its record's ``at``). Pure: no I/O, no clock. A task listed twice counts once."""
    seen = seen or {}
    out: list[StatusChange] = []
    done: set[str] = set()
    for task in tasks:
        if task.id in done:
            continue
        done.add(task.id)
        platform = _name(task.platform_status)
        prev = last.get(task.id)
        if prev is not None and prev.status is task.status and prev.platform_status == platform:
            continue
        out.append(
            StatusChange(
                at=now,
                task_id=task.id,
                status=task.status,
                since=task.status_since,
                platform_status=platform,
                previous=None if prev is None else prev.status,
                previous_platform=None if prev is None else prev.platform_status,
                after=None if prev is None else max(seen.get(task.id, prev.at), prev.at),
                board=_name(task.board),
                source=_name(task.source),
            )
        )
    return out


@dataclass
class MemoryTransitionStore:
    """Transitions in memory (tests, and ``kelpie serve`` without a database)."""

    rows: list[StatusChange] = field(default_factory=list)

    def last_statuses(self) -> Mapping[str, StatusChange]:
        out: dict[str, StatusChange] = {}
        for row in self.rows:
            out[row.task_id] = row
        return out

    def record_transitions(self, changes: Sequence[StatusChange]) -> None:
        self.rows.extend(changes)

    def transitions(self, start: datetime, end: datetime) -> list[StatusChange]:
        """Rows in ``[start, end)``, plus each task's newest row before ``start``."""
        before: dict[str, StatusChange] = {}
        inside = []
        for row in self.rows:
            if row.at < start:
                before[row.task_id] = row
            elif row.at < end:
                inside.append(row)
        return [*before.values(), *inside]

    def prune(self, before: datetime) -> int:
        """Drop rows older than ``before``, but never a task's newest row."""
        newest = {id(r) for r in self.last_statuses().values()}
        kept = [r for r in self.rows if r.at >= before or id(r) in newest]
        gone = len(self.rows) - len(kept)
        self.rows = kept
        return gone


@dataclass
class TransitionRecorder:
    """Records the status changes of each snapshot (:func:`observe`) in ``store``.

    The newest record per task is loaded from the store on the first tick, so a restart
    records no transition for a task whose status did not change. A failing store is
    logged and retried on the next tick: recording never stops the control loop, and the
    changes not stored are compared again (and so stored) once the store is back."""

    store: TransitionStore
    _last: dict[str, StatusChange] | None = field(default=None, init=False)
    _seen: dict[str, datetime] = field(default_factory=dict, init=False)
    failures: int = field(default=0, init=False)

    def record(self, snapshot: Snapshot) -> list[StatusChange]:
        """Store the snapshot's changes; returns them ([] when nothing changed or the
        store failed)."""
        try:
            if self._last is None:
                self._last = dict(self.store.last_statuses())
            changes = observe(self._last, snapshot.tasks, snapshot.now, self._seen)
            if changes:
                self.store.record_transitions(changes)
        except Exception:
            self.failures += 1
            log.exception("recording task status transitions failed")
            return []
        for c in changes:
            self._last[c.task_id] = c
        for t in snapshot.tasks:
            self._seen[t.id] = snapshot.now
        return changes


def history(changes: Iterable[StatusChange]) -> dict[str, tuple[StatusChange, ...]]:
    """Recorded changes per task id, in time order (by when they were seen)."""
    out: dict[str, list[StatusChange]] = {}
    for c in changes:
        out.setdefault(c.task_id, []).append(c)
    return {k: tuple(sorted(v, key=lambda c: c.at)) for k, v in out.items()}
