"""Rule 6 — bottlenecks and idle time per role (incidents 9, 10).

Kelpie looks at the farm as a queueing system and finds where capacity is wasted:

- **Sequential chain, idle agents** (incident 9). The longest chain of open tasks in the
  dependency graph (the critical path) is at least ``min_chain_length`` tasks long while
  at least ``min_idle_agents`` agents of the chain's roles sit idle with no queued work to
  take. The shape of the plan, not a failure, wastes the farm: Kelpie alerts with the
  critical path and suggests splitting the chain so idle agents can work in parallel.
- **Overloaded role, idle roles elsewhere** (incident 10). A role's queue (ready tasks,
  review and accepted hand-offs nobody owns) reaches ``min_queue_depth`` while none of its
  agents is idle but agents of other roles are: Kelpie alerts with a rebalancing suggestion.

Per role, an agent is *busy* with a live session or any open task it owns (in progress,
ready, accepted, waiting, blocked, and also the whole of a review: a reviewer who owns a
review task is busy until it moves). Otherwise it is *idle*, but only counts as such after
``min_idle_time`` (default 30m) since its last session, so a short gap between two tasks does
not open and close the alert (an agent that never ran counts at once). A role's *queue
depth* is the number of ownerless tasks waiting in a hand-off status (``ready`` only once
all its dependencies are finished). Idle agents that the role's own queue can absorb are not
spare. An alert lasts as long as spare capacity exists.

Tasks carrying any of ``hold_labels`` (e.g. a merge-first label that parks many tasks on
purpose) are left out of both the queue depth and the critical path.

The critical path is the longest chain by task count (ties go to the larger sum of
estimates, then the larger id), computed with Kahn's algorithm in O(tasks + dependencies).
Only a chain that can actually run counts: its head must be ready, in progress, in review or
accepted, and no task in it may be blocked, waiting or held (a held, blocked or waiting task
freezes everything behind it). Later steps may still sit in the backlog. Dependencies on
finished or unknown tasks are satisfied and ignored; tasks on a dependency cycle (or behind
one) never run and are left out of the path.

Both alerts are lasting states with stable keys, so they fire when the situation appears,
not on every tick.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import OwnerKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import is_live, last_seen

NAME = "bottlenecks"

_RUNNABLE = frozenset({Status.READY, Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED})
_FROZEN = frozenset({Status.BLOCKED, Status.WAITING})  # wait for the outside world
_SHOWN = 5  # ids kept from each end of a long chain
_QUEUED = frozenset({Status.READY, Status.REVIEW, Status.ACCEPTED})  # hand-off queues


@dataclass(frozen=True, slots=True)
class RoleLoad:
    """Capacity and demand of one role at ``snapshot.now``."""

    role: str
    agents: tuple[str, ...]
    busy: tuple[str, ...]
    idle: tuple[str, ...]  # idle for at least ``min_idle_time``, sorted
    queue: tuple[str, ...]  # ids of ownerless tasks waiting for the role, oldest first
    waiting_since: datetime | None  # when the oldest queued task began waiting
    now: datetime

    @property
    def oldest_wait(self) -> timedelta | None:
        return None if self.waiting_since is None else self.now - self.waiting_since

    @property
    def spare(self) -> tuple[str, ...]:
        """Idle agents that the role's own queue cannot absorb."""
        return self.idle[len(self.queue) :]


def role_loads(
    snapshot: Snapshot,
    min_idle_time: timedelta = timedelta(0),
    hold_labels: frozenset[str] = frozenset(),
) -> dict[str, RoleLoad]:
    """Idle agents and queue depth per role, in one pass over owners, sessions and tasks."""
    now = snapshot.now
    live_owners: set[str] = set()
    last_session: dict[str, datetime] = {}
    for s in snapshot.sessions:
        if is_live(s, now):
            live_owners.add(s.owner_id)
        seen = last_seen(s)
        if s.owner_id not in last_session or seen > last_session[s.owner_id]:
            last_session[s.owner_id] = seen
    # An agent that owns any open task is taken, even if it has not started it yet.
    assigned = {t.owner_id for t in snapshot.tasks if t.owner_id and not t.status.is_terminal}
    open_ids = {t.id for t in snapshot.tasks if not t.status.is_terminal}
    blocked_by = _open_dependencies(snapshot, open_ids)

    queues: dict[str, list[Task]] = {}
    for t in snapshot.tasks:
        if t.role is None or t.owner_id is not None or t.status not in _QUEUED:
            continue
        if hold_labels.intersection(t.labels):
            continue  # parked on purpose
        if t.status is Status.READY and blocked_by.get(t.id):
            continue  # not runnable yet
        queues.setdefault(t.role, []).append(t)

    members: dict[str, list[str]] = {}
    for o in snapshot.owners:
        if o.kind is OwnerKind.AGENT:
            members.setdefault(o.role, []).append(o.id)

    loads: dict[str, RoleLoad] = {}
    for role in sorted(members.keys() | queues.keys()):
        agents = sorted(members.get(role, ()))
        busy = [a for a in agents if a in live_owners or a in assigned]
        idle = [
            a
            for a in agents
            if a not in busy and (a not in last_session or now - last_session[a] >= min_idle_time)
        ]
        waiting = sorted(queues.get(role, ()), key=lambda t: (t.status_since, t.id))
        loads[role] = RoleLoad(
            role,
            tuple(agents),
            tuple(busy),
            tuple(idle),
            tuple(t.id for t in waiting),
            waiting[0].status_since if waiting else None,
            now,
        )
    return loads


def _open_dependencies(snapshot: Snapshot, open_ids: set[str]) -> dict[str, set[str]]:
    """For each open task, the open tasks it still waits for (finished/unknown are met)."""
    deps: dict[str, set[str]] = {}
    for d in snapshot.dependencies:
        if d.task_id in open_ids and d.depends_on in open_ids:
            deps.setdefault(d.task_id, set()).add(d.depends_on)
    return deps


@dataclass(frozen=True, slots=True)
class CriticalPath:
    tasks: tuple[Task, ...]  # in execution order
    estimate: timedelta | None  # sum of estimates, None unless every task has one

    @property
    def length(self) -> int:
        return len(self.tasks)


def critical_path(
    snapshot: Snapshot, hold_labels: frozenset[str] = frozenset()
) -> CriticalPath | None:
    """The longest runnable chain of open tasks over the dependency graph, or None.

    A task is *frozen* when it is blocked, waiting or held (``hold_labels``), and so is
    everything behind it; a chain must also start from a runnable or in-flight task.
    """
    by_id = {t.id: t for t in snapshot.tasks if not t.status.is_terminal}
    waiting_for = _open_dependencies(snapshot, set(by_id))
    successors: dict[str, list[str]] = {}
    for task_id, deps in waiting_for.items():
        for dep in deps:
            successors.setdefault(dep, []).append(task_id)
    missing = {tid: len(waiting_for.get(tid, ())) for tid in by_id}

    # best[id] = (tasks on the longest chain ending here, summed estimate seconds, previous)
    best: dict[str, tuple[int, float, str | None]] = {}
    frozen: set[str] = set()
    ready = deque(sorted(tid for tid, n in missing.items() if n == 0))
    while ready:  # Kahn: tasks on a cycle (or behind one) never reach zero and are skipped
        tid = ready.popleft()
        task = by_id[tid]
        preds = waiting_for.get(tid, ())
        if (
            task.status in _FROZEN
            or hold_labels.intersection(task.labels)
            or (not preds and task.status not in _RUNNABLE)  # a head nobody is releasing
            or not frozen.isdisjoint(preds)
        ):
            frozen.add(tid)
        else:
            prev = max(preds, key=lambda p: (best[p][0], best[p][1], p), default=None)
            base = best[prev] if prev is not None else (0, 0.0, None)
            own = task.estimate.total_seconds() if task.estimate else 0.0
            best[tid] = (base[0] + 1, base[1] + own, prev)
        for nxt in successors.get(tid, ()):
            missing[nxt] -= 1
            if missing[nxt] == 0:
                ready.append(nxt)
    if not best:
        return None
    end = max(best, key=lambda tid: (best[tid][0], best[tid][1], tid))
    chain: list[Task] = []
    cursor: str | None = end
    while cursor is not None:
        chain.append(by_id[cursor])
        cursor = best[cursor][2]
    chain.reverse()
    estimates = [t.estimate for t in chain]
    total = sum((e for e in estimates if e is not None), timedelta(0))
    return CriticalPath(tuple(chain), total if all(e is not None for e in estimates) else None)


@dataclass(frozen=True, slots=True)
class Bottlenecks:
    min_idle_agents: int = 2
    min_chain_length: int = 4
    min_queue_depth: int = 3
    min_idle_time: timedelta = timedelta(minutes=30)
    hold_labels: frozenset[str] = frozenset()
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        loads = role_loads(snapshot, self.min_idle_time, self.hold_labels)
        actions: list[Action] = []
        actions.extend(self._chain(snapshot, loads))
        actions.extend(self._overloaded(loads))
        return actions

    def _chain(self, snapshot: Snapshot, loads: dict[str, RoleLoad]) -> list[Action]:
        path = critical_path(snapshot, self.hold_labels)
        if path is None or path.length < self.min_chain_length:
            return []
        roles = {t.role for t in path.tasks if t.role} or set(loads)
        idle_roles = sorted(r for r in roles if r in loads and loads[r].spare)
        idle_agents = sorted(a for r in idle_roles for a in loads[r].spare)
        if len(idle_agents) < self.min_idle_agents:
            return []
        ids = [t.id for t in path.tasks]
        tail = ids[-1]
        eta = f", about {_human(path.estimate)} of sequential work" if path.estimate else ""
        return [
            Action(
                ActionKind.ALERT,
                NAME,
                f"{len(idle_agents)} idle agent(s) ({_names(idle_agents)}) while the work is a "
                f"chain of {path.length} tasks ({ids[0]} -> ... -> {tail}){eta}. Consider "
                f"splitting it so independent parts can run in parallel.",
                severity=Severity.INFO,
                key=f"{NAME}:chain:{tail}",
                state=f"{NAME}:chain:{tail}",
                params={
                    "critical_path_length": path.length,
                    "critical_path_head": ids[:_SHOWN],
                    "critical_path_tail": ids[-_SHOWN:],
                    "critical_path_minutes": (
                        None if path.estimate is None else int(path.estimate.total_seconds() // 60)
                    ),
                    "idle_roles": idle_roles,
                    "idle_agents": idle_agents,
                    "recommendation": "parallelize",
                },
            )
        ]

    def _overloaded(self, loads: dict[str, RoleLoad]) -> list[Action]:
        actions: list[Action] = []
        for role, load in loads.items():
            if len(load.queue) < self.min_queue_depth or load.idle:
                continue  # the queue is short, or an idle agent of the role will take it
            helpers = sorted(r for r, other in loads.items() if r != role and other.spare)
            if not helpers:
                continue
            idle_agents = sorted(a for r in helpers for a in loads[r].spare)
            assert load.oldest_wait is not None
            capacity = (
                f"{len(load.busy)} of {len(load.agents)} agent(s) busy, none idle"
                if load.agents
                else "no agents"
            )
            actions.append(
                Action(
                    ActionKind.ALERT,
                    NAME,
                    f"Role '{role}' is the bottleneck: {len(load.queue)} task(s) queued "
                    f"(oldest waiting {_human(load.oldest_wait)}) and {capacity}, "
                    f"while {_names(idle_agents)} "
                    f"({', '.join(helpers)}) sit idle. Consider rebalancing: assign idle "
                    f"agents to '{role}' work.",
                    severity=Severity.WARNING,
                    key=f"{NAME}:overloaded:{role}",
                    state=f"{NAME}:overloaded:{role}",
                    since=min(load.waiting_since, load.now),
                    params={
                        "overloaded_role": role,
                        "queue_depth": len(load.queue),
                        "queued_tasks": list(load.queue),
                        "oldest_wait_minutes": int(load.oldest_wait.total_seconds() // 60),
                        "idle_roles": helpers,
                        "idle_agents": idle_agents,
                        "recommendation": "rebalance",
                    },
                )
            )
        return actions


def _names(names: Sequence[str], shown: int = 5) -> str:
    more = len(names) - shown
    return ", ".join(names[:shown]) + (f" +{more} more" if more > 0 else "")


def _human(d: timedelta) -> str:
    minutes = max(0, int(d.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result < timedelta(0):
        raise ValueError(f"{name} must be non-negative")
    return result


@register(NAME)
def build(config: dict[str, Any]) -> Bottlenecks:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("min_idle_agents", "min_chain_length", "min_queue_depth"):
        if key in config:
            kwargs[key] = _positive_int(config.pop(key), key)
    if "min_idle_time" in config:
        kwargs["min_idle_time"] = _duration(config.pop("min_idle_time"), "min_idle_time")
    if "hold_labels" in config:
        labels = config.pop("hold_labels")
        if not isinstance(labels, list | tuple) or not all(
            isinstance(label, str) and label for label in labels
        ):
            raise ValueError("hold_labels must be a list of non-empty strings")
        kwargs["hold_labels"] = frozenset(labels)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return Bottlenecks(**kwargs)
