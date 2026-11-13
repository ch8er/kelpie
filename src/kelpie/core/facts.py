"""History of task owners, blocks and conditions that Kelpie observes itself.

The status history (#157, :mod:`kelpie.core.transitions`) records when a task's status
changed. This records the other facts a board reports only as they are now:

- **owner**, per task: who holds it (``Task.owner_id``), or nobody;
- **block**, per task: its block (reason, category, linked condition, since, Kelpie's
  mark), or none;
- **condition**, per condition id: its kind, whether it holds and the tasks waiting on it.

On every tick the control loop compares each fact with the last one recorded for it and
stores a :class:`FactChange` only when it differs. A fact that was never recorded counts
as none (no owner, no block): a task first seen without an owner or a block records
nothing, one first seen with them records a row (``previous`` None). Something missing from
a snapshot (a board not read this tick, a task archived, a condition no connector reports
any more) records nothing: absence is not a value. Kelpie never writes to the board.

Like the status history this lives next to the ledger and the state store, never in
rules: rules stay pure and see only the :class:`~kelpie.core.model.Snapshot`. No rule reads
this history yet, so the snapshot carries none of it; the daily report, ``kelpie why`` or a
rule that needs it reads the store (or gets a snapshot field) when it does.

**Restarts and gaps.** The last record per fact is read back from the store when the
recorder starts, so a restart records no change for a fact that did not change. Each row
carries ``after``, the last time Kelpie saw the previous value (the previous tick in this
process, else the previous record), and ``at``, the tick that saw the new one: the change
happened in ``(after, at]``.

**What is stored.** Values are canonical JSON. A block's reason is an agent's text: it is
stored cut to :data:`MAX_REASON` characters, like the ledger cuts agent text in params.
Retention follows the ledger's (``[storage] retention`` and ``max_rows``), keeping each
fact's value at the cut.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from kelpie.core.model import Block, Condition, Snapshot, Task
from kelpie.core.ports import FactStore

log = logging.getLogger(__name__)

MAX_REASON = 500  # characters of a block's reason that are stored (an agent's text)
MAX_NAME = 128  # characters of ids, the board and the source: names, not text


class FactKind(StrEnum):
    OWNER = "owner"  # subject: a task id
    BLOCK = "block"  # subject: a task id
    CONDITION = "condition"  # subject: a condition id


@dataclass(frozen=True, slots=True)
class FactChange:
    """A fact as Kelpie observed it change (or first saw it set).

    ``value`` and ``previous`` are canonical JSON, or None for none (no owner, no block).
    ``at`` is the snapshot time of the tick that saw it; ``after`` the last time the
    previous value was seen (None for a first record). ``board`` and ``source`` are the
    task's (for a condition, of its first waiting task the snapshot holds)."""

    at: datetime
    kind: FactKind
    subject: str
    value: str | None
    previous: str | None = None
    after: datetime | None = None
    board: str | None = None
    source: str | None = None

    @property
    def key(self) -> tuple[FactKind, str]:
        return self.kind, self.subject

    def decoded(self) -> Any:
        """The value as data (None for none)."""
        return None if self.value is None else json.loads(self.value)


def _json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _name(value: str | None) -> str | None:
    return None if value is None else value[:MAX_NAME]


def owner_value(task: Task) -> str | None:
    owner = _name(task.owner_id)
    return None if not owner else _json({"owner": owner})


def block_value(block: Block | None) -> str | None:
    if block is None:
        return None
    return _json(
        {
            "reason": block.reason[:MAX_REASON],
            "category": _name(block.category),
            "condition_id": _name(block.condition_id),
            "since": block.since.isoformat(),
            "kelpie": None if block.kelpie is None else str(block.kelpie),
        }
    )


def condition_value(condition: Condition) -> str:
    return _json(  # type: ignore[return-value]
        {
            "kind": str(condition.kind),
            "satisfied": condition.satisfied,
            "waiters": sorted(_name(t) or "" for t in condition.waiters),
        }
    )


def current(snapshot: Snapshot) -> list[tuple[FactKind, str, str | None, Task | None]]:
    """Every fact the snapshot shows: ``(kind, subject, value, its task)``; subjects are
    cut to :data:`MAX_NAME`. A task listed twice, or a condition, counts once (the first)."""
    out: list[tuple[FactKind, str, str | None, Task | None]] = []
    seen: set[str] = set()
    for task in snapshot.tasks:
        subject = task.id[:MAX_NAME]
        if subject in seen:
            continue
        seen.add(subject)
        out.append((FactKind.OWNER, subject, owner_value(task), task))
        out.append((FactKind.BLOCK, subject, block_value(task.block), task))
    tasks = {t.id: t for t in snapshot.tasks}
    done: set[str] = set()
    for c in snapshot.conditions:
        subject = c.id[:MAX_NAME]
        if subject in done:
            continue
        done.add(subject)
        waiter = next((tasks[t] for t in c.waiters if t in tasks), None)
        out.append((FactKind.CONDITION, subject, condition_value(c), waiter))
    return out


def observe(
    last: Mapping[tuple[FactKind, str], FactChange],
    snapshot: Snapshot,
    seen: Mapping[tuple[FactKind, str], datetime] | None = None,
) -> list[FactChange]:
    """The changes of the snapshot's facts against ``last`` (the newest record per fact),
    at ``snapshot.now``; ``seen`` is when each fact was last seen with its recorded value
    (default: its record's ``at``). Pure: no I/O, no clock."""
    seen = seen or {}
    out: list[FactChange] = []
    for kind, subject, value, task in current(snapshot):
        prev = last.get((kind, subject))
        before = None if prev is None else prev.value
        if value == before:
            continue
        out.append(
            FactChange(
                at=snapshot.now,
                kind=kind,
                subject=subject,
                value=value,
                previous=before,
                after=None if prev is None else max(seen.get(prev.key, prev.at), prev.at),
                board=None if task is None else _name(task.board),
                source=None if task is None else _name(task.source),
            )
        )
    return out


@dataclass
class MemoryFactStore:
    """Fact changes in memory (tests, and ``kelpie serve`` without a database)."""

    rows: list[FactChange] = field(default_factory=list)

    def last_facts(self) -> Mapping[tuple[FactKind, str], FactChange]:
        return {row.key: row for row in self.rows}

    def record_facts(self, changes: Sequence[FactChange]) -> None:
        self.rows.extend(changes)

    def facts(self, start: datetime, end: datetime) -> list[FactChange]:
        """Rows in ``[start, end)``, plus each fact's newest row before ``start``."""
        before: dict[tuple[FactKind, str], FactChange] = {}
        inside = []
        for row in self.rows:
            if row.at < start:
                before[row.key] = row
            elif row.at < end:
                inside.append(row)
        return [*before.values(), *inside]

    def prune(self, before: datetime, max_rows: int | None = None) -> int:
        """Drop rows older than ``before`` except each fact's newest one before it, then the
        oldest rows beyond ``max_rows`` (rows that are not a fact's newest first)."""
        newest_before: dict[tuple[FactKind, str], int] = {}
        for i, r in enumerate(self.rows):
            if r.at < before:
                newest_before[r.key] = i
        keep = set(newest_before.values())
        kept = [r for i, r in enumerate(self.rows) if r.at >= before or i in keep]
        if max_rows is not None and len(kept) > max_rows:
            newest = {id(r) for r in {r.key: r for r in kept}.values()}
            order = sorted(range(len(kept)), key=lambda i: (id(kept[i]) in newest, kept[i].at))
            drop = set(order[: len(kept) - max_rows])
            kept = [r for i, r in enumerate(kept) if i not in drop]
        gone = len(self.rows) - len(kept)
        self.rows = kept
        return gone


@dataclass
class FactRecorder:
    """Records the owner, block and condition changes of each snapshot (:func:`observe`)
    in ``store``. The newest record per fact is loaded from the store on the first tick,
    so a restart records no change for a fact that did not change. A failing store is
    logged and retried on the next tick: recording never stops the control loop, and the
    changes not stored are compared again (and so stored) once the store is back."""

    store: FactStore
    _last: dict[tuple[FactKind, str], FactChange] | None = field(default=None, init=False)
    _seen: dict[tuple[FactKind, str], datetime] = field(default_factory=dict, init=False)
    failures: int = field(default=0, init=False)

    def record(self, snapshot: Snapshot) -> list[FactChange]:
        """Store the snapshot's changes; returns them ([] when nothing changed or the store
        failed)."""
        try:
            if self._last is None:
                self._last = dict(self.store.last_facts())
            changes = observe(self._last, snapshot, self._seen)
            if changes:
                self.store.record_facts(changes)
        except Exception:
            self.failures += 1
            log.exception("recording task owners, blocks and conditions failed")
            return []
        for c in changes:
            self._last[c.key] = c
        for kind, subject, _, _ in current(snapshot):
            self._seen[(kind, subject)] = snapshot.now
        return changes


def history(changes: Iterable[FactChange]) -> dict[tuple[FactKind, str], tuple[FactChange, ...]]:
    """Recorded changes per fact, in time order (by when they were seen)."""
    out: dict[tuple[FactKind, str], list[FactChange]] = {}
    for c in changes:
        out.setdefault(c.key, []).append(c)
    return {k: tuple(sorted(v, key=lambda c: c.at)) for k, v in out.items()}
