"""Rule 6 — bottlenecks and idle time per role (incidents 9, 10, 14, 16).

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

- **Idle with ready work** (incident 14). Agents have been idle for ``min_idle_time`` while
  unheld ready work they could take (ownerless and queued in their role, or with no role)
  has waited ``idle_alert_after``: the dispatcher is not handing it out, and Kelpie alerts
  with the idle agents and the waiting work. Held work is parked on purpose and does not
  count until its hold expires (below).
- **Holds with an owner and an expiry** (incident 14). A hold is a label that parks tasks on
  purpose, listed in ``holds`` with an owner and an expected length (``expires_after``,
  else ``hold_expiry``, else 4h). Bare ``hold_labels`` only park tasks, with no expiry
  unless ``hold_expiry`` is set. Kelpie cannot see when a label was set, so a hold's age is
  how long its oldest queued task (ready, review, accepted) has not moved. Past the expiry
  Kelpie alerts once, naming the owner, the idle agents that could take the held work and
  the idle agent minutes since the expiry (for the cost-of-chaos report). Lifting a hold in
  act mode needs an executor that owns the label; none exists yet, so it is only an alert.
- **Human tasks on the critical path** (incident 16). For each open human-owned task, the
  agent tasks behind it, transitively, through dependencies and the ``human_task``
  conditions of blocked or waiting tasks. A human task the human can do now (ready or in
  progress, nothing open before it) that has waited ``human_gate_after`` (1h) with
  ``human_gate_min_tasks`` (3) or more agent tasks behind it is escalated to its owner
  with the count and the estimated agent hours. When idle agents have no ready work they
  could take while such gates exist, Kelpie also alerts with the idle capacity and the
  gates that hold the most work. Kelpie never removes or rewrites a dependency: it shows
  its cost and asks whether the link is needed. Human tasks with nothing behind them are
  not flagged, and a chain through a human task gets no parallelization advice.

``slots`` (optional) is the farm's concurrency limit: once that many agents have a live
session, idle agents are not free capacity, and no idle-capacity alert fires (chain,
overloaded role, idle with ready work, human-gated).

Held tasks are left out of both the queue depth and the critical path.

The critical path is the longest chain by task count (ties go to the larger sum of
estimates, then the larger id), computed with Kahn's algorithm in O(tasks + dependencies).
Only a chain that can actually run counts: its head must be ready, in progress, in review or
accepted, and no task in it may be blocked, waiting or held (a held, blocked or waiting task
freezes everything behind it). Later steps may still sit in the backlog. Dependencies on
finished or unknown tasks are satisfied and ignored; tasks on a dependency cycle (or behind
one) never run and are left out of the path.

All alerts are lasting states with stable keys, so they fire when the situation appears,
not on every tick.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import ConditionKind, OwnerKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import is_live, last_seen, linked_conditions

NAME = "bottlenecks"

_RUNNABLE = frozenset({Status.READY, Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED})
_FROZEN = frozenset({Status.BLOCKED, Status.WAITING})  # wait for the outside world
_SHOWN = 5  # ids kept from each end of a long chain
_QUEUED = frozenset({Status.READY, Status.REVIEW, Status.ACCEPTED})  # hand-off queues
DEFAULT_HOLD_EXPIRY = timedelta(hours=4)


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
class Hold:
    """A label that parks tasks on purpose, who owns it and how long it should last."""

    label: str
    owner: str | None = None
    expires_after: timedelta | None = None  # None: the rule's ``hold_expiry``


@dataclass(frozen=True, slots=True)
class HumanGate:
    """An open human task and the open agent tasks that wait behind it, transitively."""

    task: Task
    behind: tuple[str, ...]  # agent task ids, sorted
    estimate: timedelta | None  # summed estimates of the tasks behind that have one
    actionable: bool  # the human can do it now: ready or in progress, nothing open before it


def human_gates(snapshot: Snapshot) -> list[HumanGate]:
    """Open human-owned tasks with agent work behind them, the most work first.

    Behind means reachable through dependencies (``depends_on`` -> ``task_id``) or an unmet
    ``human_task`` condition of a blocked or waiting task's current wait (as
    ``linked_conditions`` reads it) whose ``params.task`` names the human task, through any
    open task. Agent tasks are open tasks not owned by a human.
    """
    humans = {o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN}
    by_id = {t.id: t for t in snapshot.tasks if not t.status.is_terminal}
    gates = [t for t in by_id.values() if t.owner_id in humans]
    if not gates:
        return []
    successors: dict[str, set[str]] = {}
    for d in snapshot.dependencies:
        if d.task_id in by_id and d.depends_on in by_id:
            successors.setdefault(d.depends_on, set()).add(d.task_id)
    for t in by_id.values():
        if t.status not in _FROZEN:
            continue
        for c in linked_conditions(t, snapshot)[0]:
            target = c.params.get("task")
            if c.kind is ConditionKind.HUMAN_TASK and c.satisfied is not True and target in by_id:
                successors.setdefault(str(target), set()).add(t.id)
    open_deps = _open_dependencies(snapshot, set(by_id))
    result: list[HumanGate] = []
    for gate in gates:
        seen: set[str] = set()
        todo = [gate.id]
        while todo:
            for nxt in successors.get(todo.pop(), ()):
                if nxt not in seen and nxt != gate.id:
                    seen.add(nxt)
                    todo.append(nxt)
        behind = sorted(tid for tid in seen if by_id[tid].owner_id not in humans)
        if not behind:
            continue  # nothing waits for it: not a gate
        known = [e for tid in behind if (e := by_id[tid].estimate) is not None]
        actionable = gate.status in {Status.READY, Status.IN_PROGRESS} and not open_deps.get(
            gate.id
        )
        estimate = sum(known, timedelta(0)) if known else None
        result.append(HumanGate(gate, tuple(behind), estimate, actionable))
    result.sort(key=lambda g: (-len(g.behind), g.task.status_since, g.task.id))
    return result


@dataclass(frozen=True, slots=True)
class Bottlenecks:
    min_idle_agents: int = 2
    min_chain_length: int = 4
    min_queue_depth: int = 3
    min_idle_time: timedelta = timedelta(minutes=30)
    hold_labels: frozenset[str] = frozenset()
    holds: tuple[Hold, ...] = ()
    # Expected length of a hold without its own ``expires_after``. None: 4h for ``holds``
    # entries, no expiry for bare ``hold_labels`` (they park tasks on purpose).
    hold_expiry: timedelta | None = None
    idle_alert_after: timedelta = timedelta(minutes=30)
    human_gate_min_tasks: int = 3
    human_gate_after: timedelta = timedelta(hours=1)  # a human task waits this long first
    slots: int | None = None
    name: str = NAME

    @property
    def held_labels(self) -> frozenset[str]:
        return self.hold_labels | {h.label for h in self.holds}

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        held = self.held_labels
        loads = role_loads(snapshot, self.min_idle_time, held)
        now = snapshot.now
        gates = [
            g
            for g in human_gates(snapshot)
            if g.actionable and now - g.task.status_since >= self.human_gate_after
        ]
        free = self._free_capacity(snapshot)
        actions: list[Action] = []
        if free:
            actions.extend(self._chain(snapshot, loads, held))
            actions.extend(self._overloaded(loads))
            actions.extend(self._idle_with_ready_work(snapshot, loads, held))
            actions.extend(self._human_gated(snapshot, loads, held, gates))
        actions.extend(self._expired_holds(snapshot, loads if free else {}))
        actions.extend(self._escalate_gates(gates, now))
        return actions

    def _running(self, snapshot: Snapshot) -> int:
        """Slots in use: agents with a live session."""
        return len({s.owner_id for s in snapshot.sessions if is_live(s, snapshot.now)})

    def _free_capacity(self, snapshot: Snapshot) -> bool:
        return self.slots is None or self._running(snapshot) < self.slots

    def _chain(
        self, snapshot: Snapshot, loads: dict[str, RoleLoad], held: frozenset[str]
    ) -> list[Action]:
        path = critical_path(snapshot, held)
        if path is None or path.length < self.min_chain_length:
            return []
        humans = {o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN}
        if any(t.owner_id in humans for t in path.tasks):
            return []  # gated by a human task: splitting will not help, the gate alert says so
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

    def _takeable(
        self, snapshot: Snapshot, idle_roles: Collection[str], held: bool, labels: frozenset[str]
    ) -> list[Task]:
        """Ownerless queued work an idle agent could take (its role, or no role), held or
        not held by ``labels``, waiting at least ``idle_alert_after``, oldest first."""
        now = snapshot.now
        open_ids = {t.id for t in snapshot.tasks if not t.status.is_terminal}
        blocked_by = _open_dependencies(snapshot, open_ids)
        return sorted(
            (
                t
                for t in snapshot.tasks
                if t.owner_id is None
                and t.status in _QUEUED
                and not (t.status is Status.READY and blocked_by.get(t.id))
                and (t.role is None or t.role in idle_roles)
                and bool(labels.intersection(t.labels)) is held
                and now - t.status_since >= self.idle_alert_after
            ),
            key=lambda t: (t.status_since, t.id),
        )

    def _idle_with_ready_work(
        self, snapshot: Snapshot, loads: dict[str, RoleLoad], held: frozenset[str]
    ) -> list[Action]:
        """Idle agents while unheld ready work they could take has waited
        ``idle_alert_after``. Held work is the hold alert's, once the hold expires."""
        now = snapshot.now
        idle_roles = {r: load.idle for r, load in loads.items() if load.idle}
        work = self._takeable(snapshot, idle_roles, False, held) if idle_roles else []
        if not work:
            return []
        roles, idle = _who_could_take(work, idle_roles)
        oldest = work[0].status_since
        ids = [t.id for t in work]
        return [
            Action(
                ActionKind.ALERT,
                NAME,
                f"{len(idle)} agent(s) idle ({_names(idle)}) while {len(work)} ready task(s) "
                f"wait (oldest {_human(now - oldest)}): {_names(ids)}. Likely cause: the "
                "dispatcher is not handing it out (paused, a guard, or no free slot).",
                severity=Severity.WARNING,
                key=f"{NAME}:idle-ready",
                state=f"{NAME}:idle-ready",
                since=oldest,
                params={
                    "idle_agents": idle,
                    "idle_roles": roles,
                    "ready_tasks": ids[: _SHOWN * 2],
                    "ready_count": len(ids),
                    "oldest_wait_minutes": int((now - oldest).total_seconds() // 60),
                    "likely_cause": "dispatch",
                },
            )
        ]

    def _expired_holds(self, snapshot: Snapshot, loads: dict[str, RoleLoad]) -> list[Action]:
        """Holds past their expiry. A hold's age is how long its oldest queued task (ready,
        review, accepted) has not moved; backlog tasks carrying the label do not count."""
        holds = {h.label: h for h in self.holds}
        if self.hold_expiry is not None:
            for label in self.hold_labels - holds.keys():
                holds[label] = Hold(label)
        idle_roles = {r: load.idle for r, load in loads.items() if load.idle}
        last = _last_session(snapshot)
        actions: list[Action] = []
        for label in sorted(holds):
            hold = holds[label]
            tasks = sorted(
                (t for t in snapshot.tasks if t.status in _QUEUED and label in t.labels),
                key=lambda t: (t.status_since, t.id),
            )
            if not tasks:
                continue
            since = tasks[0].status_since
            expiry = hold.expires_after or self.hold_expiry or DEFAULT_HOLD_EXPIRY
            age = snapshot.now - since
            if age < expiry:
                continue
            ids = [t.id for t in tasks]
            waiting = self._takeable(snapshot, idle_roles, True, frozenset({label}))
            _, idle = _who_could_take(waiting, idle_roles) if waiting else ([], [])
            expired_at = since + expiry
            # Idle time the hold caused: each idle agent since the hold expired or its own
            # last session, whichever is later.
            idle_minutes = sum(
                int((snapshot.now - max(expired_at, last.get(a, expired_at))).total_seconds()) // 60
                for a in idle
            )
            owner = f"Owner: {hold.owner}." if hold.owner else "No owner is configured for it."
            meanwhile = (
                f" Meanwhile {len(idle)} agent(s) sit idle ({_names(idle)})." if idle else ""
            )
            actions.append(
                Action(
                    ActionKind.ALERT,
                    NAME,
                    f"Hold '{label}' looks forgotten: its {len(ids)} task(s) ({_names(ids)}) "
                    f"have not moved for {_human(age)}, past its expected {_human(expiry)}."
                    f"{meanwhile} {owner} Lift the hold, or set a new expiry.",
                    severity=Severity.WARNING,
                    key=f"{NAME}:hold:{label}",
                    state=f"{NAME}:hold:{label}",
                    since=expired_at,
                    params={
                        "hold": label,
                        "owner": hold.owner,
                        "held_tasks": ids[: _SHOWN * 2],
                        "held_count": len(ids),
                        "held_minutes": int(age.total_seconds() // 60),
                        "expires_after_minutes": int(expiry.total_seconds() // 60),
                        "idle_agents": idle,
                        "idle_agent_minutes": idle_minutes,
                    },
                )
            )
        return actions

    def _human_gated(
        self,
        snapshot: Snapshot,
        loads: dict[str, RoleLoad],
        held: frozenset[str],
        gates: list[HumanGate],
    ) -> list[Action]:
        """Idle agents with no ready work they could take, while ready human tasks gate
        agent work."""
        if not gates:
            return []
        idle_roles = {r: load.idle for r, load in loads.items() if load.idle}
        if not idle_roles:
            return []
        humans = {o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN}
        open_ids = {t.id for t in snapshot.tasks if not t.status.is_terminal}
        blocked_by = _open_dependencies(snapshot, open_ids)
        takeable = [
            t
            for t in snapshot.tasks
            if t.owner_id is None
            and t.status in _QUEUED
            and not (t.status is Status.READY and blocked_by.get(t.id))
            and not held.intersection(t.labels)
            and (t.role is None or t.role in idle_roles)
        ]
        if takeable:
            return []  # idle agents have work they could take: the idle-ready alert's case
        ready_human = sorted(
            t.id
            for t in snapshot.tasks
            if t.status is Status.READY and t.owner_id in humans and not blocked_by.get(t.id)
        )
        idle = sorted({a for agents in idle_roles.values() for a in agents})
        agents = sorted({a for load in loads.values() for a in load.agents})
        running = self._running(snapshot)
        total = self.slots if self.slots is not None else len(agents)
        behind = sorted({tid for g in gates for tid in g.behind})
        top = gates[:_SHOWN]
        listed = "; ".join(
            f"{g.task.id} ('{g.task.title}', {g.task.owner_id}): {len(g.behind)} agent task(s)"
            for g in top
        )
        return [
            Action(
                ActionKind.ALERT,
                NAME,
                f"{running} of {total} agent slot(s) running, {_names(idle)} idle with no "
                f"ready work. Human tasks gate {len(behind)} agent task(s): {listed}. "
                "Kelpie does not change dependencies: is each link needed?",
                severity=Severity.WARNING,
                key=f"{NAME}:human-gated",
                state=f"{NAME}:human-gated",
                params={
                    "idle_agents": idle,
                    "busy_slots": running,
                    "slots": total,
                    "human_tasks": [g.task.id for g in top],
                    "blocked_agent_tasks": len(behind),
                    "ready_human_tasks": ready_human,
                    "recommendation": "unblock_human_tasks",
                },
            )
        ]

    def _escalate_gates(self, gates: list[HumanGate], now: datetime) -> list[Action]:
        actions: list[Action] = []
        for gate in gates:
            if len(gate.behind) < self.human_gate_min_tasks:
                continue
            t = gate.task
            hours = (
                None if gate.estimate is None else round(gate.estimate.total_seconds() / 3600, 1)
            )
            work = f", about {_human(gate.estimate)} of agent work" if gate.estimate else ""
            actions.append(
                Action(
                    ActionKind.ESCALATE,
                    NAME,
                    f"Human task {t.id} ('{t.title}', owner {t.owner_id}, {t.status} for "
                    f"{_human(now - t.status_since)}) gates {len(gate.behind)} agent task(s)"
                    f"{work} ({_names(gate.behind)}). Do it first, or remove the link if the "
                    "agent work does not need it. Kelpie does not change dependencies.",
                    task_id=t.id,
                    severity=Severity.WARNING,
                    key=f"{NAME}:human-gate:{t.id}",
                    state=f"{NAME}:human-gate:{t.id}",
                    since=t.status_since,
                    params={
                        "owner": t.owner_id,
                        "blocked_agent_tasks": len(gate.behind),
                        "blocked_tasks": list(gate.behind[: _SHOWN * 2]),
                        "agent_hours": hours,
                    },
                )
            )
        return actions


def _who_could_take(
    work: Sequence[Task], idle_roles: dict[str, tuple[str, ...]]
) -> tuple[list[str], list[str]]:
    """Roles and idle agents that could take ``work``: any idle agent for work without a
    role, otherwise the idle agents of the work's roles."""
    if any(t.role is None for t in work):
        roles = sorted(idle_roles)
    else:
        roles = sorted({t.role for t in work if t.role in idle_roles})
    return roles, sorted({a for r in roles for a in idle_roles[r]})


def _last_session(snapshot: Snapshot) -> dict[str, datetime]:
    """The last sign of life of each owner's sessions."""
    last: dict[str, datetime] = {}
    for s in snapshot.sessions:
        seen = last_seen(s)
        if s.owner_id not in last or seen > last[s.owner_id]:
            last[s.owner_id] = seen
    return last


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


def _hold(raw: Any) -> Hold:
    if not isinstance(raw, dict):
        raise ValueError("holds entries must be tables with a label")
    raw = dict(raw)
    label = raw.pop("label", None)
    if not isinstance(label, str) or not label:
        raise ValueError("holds: label must be a non-empty string")
    owner = raw.pop("owner", None)
    if owner is not None and (not isinstance(owner, str) or not owner):
        raise ValueError(f"holds.{label}: owner must be a non-empty string")
    expires = raw.pop("expires_after", None)
    expires_after = None if expires is None else _duration(expires, f"holds.{label}.expires_after")
    if expires_after is not None and expires_after <= timedelta(0):
        raise ValueError(f"holds.{label}.expires_after must be positive")
    if raw:
        raise ValueError(f"holds.{label}: unknown keys {sorted(raw)}")
    return Hold(label, owner, expires_after)


@register(NAME)
def build(config: dict[str, Any]) -> Bottlenecks:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("min_idle_agents", "min_chain_length", "min_queue_depth", "human_gate_min_tasks"):
        if key in config:
            kwargs[key] = _positive_int(config.pop(key), key)
    if "slots" in config:
        kwargs["slots"] = _positive_int(config.pop("slots"), "slots")
    for key in ("min_idle_time", "idle_alert_after", "human_gate_after"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if "hold_expiry" in config:
        kwargs["hold_expiry"] = _duration(config.pop("hold_expiry"), "hold_expiry")
        if kwargs["hold_expiry"] <= timedelta(0):
            raise ValueError("hold_expiry must be positive")
    if "hold_labels" in config:
        labels = config.pop("hold_labels")
        if not isinstance(labels, list | tuple) or not all(
            isinstance(label, str) and label for label in labels
        ):
            raise ValueError("hold_labels must be a list of non-empty strings")
        kwargs["hold_labels"] = frozenset(labels)
    if "holds" in config:
        raw = config.pop("holds")
        if not isinstance(raw, list | tuple):
            raise ValueError("holds must be a list of tables ([[...holds]])")
        holds = tuple(_hold(h) for h in raw)
        labels = [h.label for h in holds]
        if len(set(labels)) != len(labels):
            raise ValueError("holds: duplicate label")
        kwargs["holds"] = holds
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return Bottlenecks(**kwargs)
