"""Blocker chains: what a task waits for, and where that wait ends.

Shared by rule 3's dead-end pattern and ``kelpie why``, so both read a chain alike. A task
that is not started yet or is parked (backlog, ready, blocked, waiting), has no live session
and is not finished waits for:

- its **parents** (``snapshot.dependencies``) that are on the board and not finished; a
  finished parent satisfies the link and a parent not on the board is left out, as the
  other rules read dependencies;
- the **conditions** of its current block or wait (``linked_conditions``) that are not met.
  A ``task_status`` or ``human_task`` condition names another task (``params.task``) and
  leads to it. Any other condition (a PR, CI, a quota) waits on the outside world and ends
  the chain there.

A chain hits a **dead end** when what it waits for can never come: a condition flagged
``params.terminal`` (the task it names was cancelled or failed, a PR was closed without
merging), a condition naming a task the board does not show (``params.error`` is
``not_found``: archived, deleted or out of reach), or a block that names a condition Kelpie
cannot see. A terminal or missing-task flag counts once it was checked after the wait began.
Tasks in progress, in review or accepted are moving (or are another pattern's business), so a
chain stops at them.

Known limit: a dependency on a cancelled or failed parent is not a dead end. The model counts
any finished parent as satisfying a dependency (as rules 6 and the dispatch gate do), so the
child is free to start; whether it should is a planning decision Kelpie does not make.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from kelpie.core.model import Condition, ConditionKind, Snapshot, Status, Task
from kelpie.rules._tasks import (
    DEFAULT_STALE_AFTER,
    dead_end_text,
    dead_ends,
    is_live,
    linked_conditions,
)

WAITING = frozenset({Status.BACKLOG, Status.READY, Status.BLOCKED, Status.WAITING})
_TASK_REFS = frozenset({ConditionKind.TASK_STATUS, ConditionKind.HUMAN_TASK})


@dataclass(frozen=True, slots=True)
class DeadEnd:
    """Something a task waits for that can never come."""

    ref: str  # the task id (or condition id) the wait ends in
    why: str  # e.g. "was cancelled", "is not on the board"
    since: datetime | None = None  # when it ended, if known
    # TERMINAL (a condition flagged terminal) and MISSING (a condition Kelpie cannot see) are
    # what rules 2 and 4 alert on for the task in front; NOT_FOUND (a task the board does not
    # show) is only rule 3's.
    kind: str = "terminal"


TERMINAL, MISSING, NOT_FOUND = "terminal", "missing", "not_found"


@dataclass(frozen=True, slots=True)
class Edge:
    """One thing a task waits for: another task, a dead end, or the outside world."""

    via: str  # how the task waits: "depends on", "blocked on <condition>", ...
    task: str | None = None  # the next task in the chain
    dead: DeadEnd | None = None
    external: str | None = None  # a condition on the outside world (PR, CI, quota, time)


@dataclass(frozen=True, slots=True)
class Chain:
    """A path from a task to what its wait ends in. ``path`` starts with the task."""

    path: tuple[str, ...]
    end: Edge


class BlockerGraph:
    """Who waits for what, built once per snapshot. Pure: reads the snapshot only."""

    def __init__(self, snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER) -> None:
        self.snapshot = snapshot
        self.by_id = {t.id: t for t in snapshot.tasks}
        live = {s.task_id for s in snapshot.sessions if is_live(s, snapshot.now, stale_after)}
        parents: dict[str, set[str]] = {}
        for d in snapshot.dependencies:
            parents.setdefault(d.task_id, set()).add(d.depends_on)
        self.edges: dict[str, list[Edge]] = {}
        for task in snapshot.tasks:
            if task.status in WAITING and task.id not in live:
                self.edges[task.id] = self._edges(task, sorted(parents.get(task.id, ())))
        self.waiters: dict[str, set[str]] = {}
        for tid, edges in self.edges.items():
            for e in edges:
                if e.task is not None:
                    self.waiters.setdefault(e.task, set()).add(tid)

    def _edges(self, task: Task, parents: list[str]) -> list[Edge]:
        edges = [
            Edge("depends on", task=p)
            for p in parents
            if p != task.id and p in self.by_id and not self.by_id[p].status.is_terminal
        ]
        if task.status not in {Status.BLOCKED, Status.WAITING}:
            return edges
        conditions, missing = linked_conditions(task, self.snapshot)
        verb = "blocked on" if task.status is Status.BLOCKED else "waiting on"
        if missing is not None:
            dead = DeadEnd(missing, "is a condition Kelpie cannot see", kind=MISSING)
            return [*edges, Edge(f"{verb} condition", dead=dead)]
        since = task.block.since if task.block is not None else task.status_since
        ended = {c.id for c in dead_ends(conditions, since)}
        for c in conditions:
            if c.satisfied is True:
                continue
            edges.append(self._condition_edge(c, verb, c.id in ended, since))
        return edges

    def _condition_edge(self, c: Condition, verb: str, ended: bool, since: datetime) -> Edge:
        via = f"{verb} {c.kind} condition {c.id}"
        ref = c.params.get("task")
        if c.kind in _TASK_REFS and isinstance(ref, str) and ref:
            target = self.by_id.get(ref)
            if ended:
                status = target.status.value if target else c.params.get("status", "ended")
                at = target.status_since if target else None
                return Edge(via, dead=DeadEnd(ref, f"ended as {status}", at))
            seen = c.checked_at is not None and c.checked_at >= since
            if target is None and c.params.get("error") == "not_found" and seen:
                why = "is not on the board (archived or out of reach)"
                gone = DeadEnd(ref, why, kind=NOT_FOUND)
                return Edge(via, dead=gone)
            if target is not None:
                return Edge(via, task=ref)
            return Edge(via, external=f"{ref} (unknown)")
        if ended:
            return Edge(via, dead=DeadEnd(c.id, dead_end_text([c])))
        return Edge(via, external=c.id)

    def chains(self, task_id: str) -> list[Chain]:
        """Every end the task's wait reaches, by the shortest path, dead ends first."""
        found: list[Chain] = []
        seen = {task_id}
        queue: deque[tuple[str, ...]] = deque([(task_id,)])
        while queue:
            path = queue.popleft()
            edges = self.edges.get(path[-1], ())
            if not edges and len(path) > 1:  # a waiting task that waits for nothing visible
                status = self.by_id[path[-1]].status
                found.append(Chain(path, Edge(f"is {status} with nothing Kelpie can check")))
            for e in edges:
                if e.task is None:
                    found.append(Chain(path, e))
                elif e.task not in seen:
                    seen.add(e.task)
                    if e.task in self.edges:
                        queue.append((*path, e.task))
                    else:  # moving, finished or not waiting: the chain stops there
                        found.append(Chain((*path, e.task), e))
        return sorted(found, key=lambda c: (c.end.dead is None, len(c.path), c.path))

    def behind(self, task_id: str) -> dict[str, tuple[str, ...]]:
        """Tasks that wait for ``task_id``, transitively, with a path from each to it."""
        paths: dict[str, tuple[str, ...]] = {}
        queue: deque[tuple[str, ...]] = deque([(task_id,)])
        while queue:
            path = queue.popleft()
            for waiter in sorted(self.waiters.get(path[0], ())):
                if waiter != task_id and waiter not in paths:
                    paths[waiter] = (waiter, *path)
                    queue.append(paths[waiter])
        return paths


def behind_lookup(
    snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> Callable[[str], list[str]]:
    """Ids of the tasks waiting behind a task, sorted; the graph is built on first use, so a
    rule that never needs it pays nothing."""
    graph: BlockerGraph | None = None

    def lookup(task_id: str) -> list[str]:
        nonlocal graph
        if graph is None:
            graph = BlockerGraph(snapshot, stale_after)
        return sorted(graph.behind(task_id))

    return lookup


def behind_text(ids: list[str], shown: int = 5) -> str:
    """ " N task(s) wait behind it: a, b." for an alert, or "" when nothing does."""
    if not ids:
        return ""
    names = ", ".join(ids[:shown]) + (f" and {len(ids) - shown} more" if len(ids) > shown else "")
    return f" {len(ids)} task(s) wait behind it: {names}."
