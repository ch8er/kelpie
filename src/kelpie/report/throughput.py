"""Farm throughput: tasks per hour per role, task time by stage, queue length and
utilization per role, rework rate (cost per completed task is priced by the report
section). Pure and deterministic; no LLM.

**Where the time comes from.** Kelpie does not store a task's status history: the board
reports each task's current status and since when, its returns, and the sessions that
worked on it (start, end, the status each declared at its end). The day of every task is
rebuilt from those and from Kelpie's own long-lived alert states, cut into segments at
every known change; each segment gets one stage, the first that applies:

1. ``working`` / ``review``: a session of the task runs (``review`` when its owner has one
   of ``review_roles``);
2. ``ownerless``: an ``orphan_status`` state of the task is open;
3. ``waiting``: a ``wait_condition`` or ``stale_blocks`` state is open (waiting for
   conditions);
4. else the status at that time: the current one from ``status_since`` on (``in_progress``
   without an owner is ``ownerless``), before that the status the last session declared at
   its end (none: ready). ``ready`` and ``in_progress``
   outside a session are ``queue`` (waiting for an agent), ``review`` and ``accepted`` are
   ``review`` (waiting for a reviewer or a merge), ``blocked`` and ``waiting`` are
   ``waiting``; ``backlog`` and finished statuses are not counted.

A task is counted from its first known moment within the day (its first session, alert
state or current status, whichever is earliest): when it was created is not known, so queue
time before its first pickup is left out and the queue is a lower bound. The board only
looks back so far (Hermes ``lookback``): older sessions and finished tasks are missing. A
session without an end or a heartbeat counts as running until the end of the day. Hours
are task-hours: two tasks in the queue for an hour are two queue hours, and the *mean queue
length* of a role is its queue hours over the day's hours.

**Per role** (the task's role, else its owner's): tasks completed (reached ``done`` within
the day) and per hour; *hand-offs*, sessions of the role's agents that ended within the
day with a declared status, and per hour (a reviewer's throughput is reviews, not tasks
done; a review that sends a task back declares no status, so every review session that
ended counts); *session utilization*, the hours the role's agents had a session running
over agents x hours; *rework rate*, the share of the tasks the role worked on within the
day (its agents ran a non-review session on them; with no session known, the task's own
role) that came back to it: a return counts for the role that redoes the work, not the
reviewer that sent it back (:func:`kelpie.core.stages.rework_role`; a task in review often
carries the reviewer's role by then). A role that only reviews has no rework rate.

**Live** (``/metrics``) gauges measure related but different things from the current
snapshot (agents with work, the ownerless queue, returns per done task); see
:mod:`kelpie.metrics`. Stages are shared: :mod:`kelpie.core.stages`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from itertools import pairwise

from kelpie.core.model import Owner, OwnerKind, Session, Status, Task
from kelpie.core.stages import STAGES, UNKNOWN_ROLE, rework_role, status_stage, task_roles
from kelpie.core.states import OpenState

OWNERLESS_RULES = frozenset({"orphan_status"})
WAITING_RULES = frozenset({"wait_condition", "stale_blocks"})
_UNDECLARED = frozenset({Status.IN_PROGRESS, Status.READY, Status.BACKLOG})
type _RoleOf = Callable[[Session], str | None]


@dataclass(frozen=True, slots=True)
class RoleThroughput:
    role: str
    hours: float  # the day's length
    agents: int = 0
    busy_hours: float = 0.0  # agent-hours with a session running
    completed: tuple[str, ...] = ()
    handoffs: int = 0
    stages: Mapping[str, float] = field(default_factory=dict)  # task-hours per stage
    active: tuple[str, ...] = ()  # tasks with stage time or completed within the day
    reworked: tuple[str, ...] = ()  # tasks the role redid after a return within the day
    worked: tuple[str, ...] = ()  # tasks the role worked on (not as a reviewer) in the day
    returns: int = 0
    reopened: int = 0  # done tasks moved back out of done within the day
    queue_now: int = 0  # tasks of the role in the queue stage at the end of the day
    # waiting task-hours by cause (kelpie.report.waits), and the tasks that waited for each
    waits: Mapping[str, float] = field(default_factory=dict)
    waited: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def tasks_per_hour(self) -> float:
        return len(self.completed) / self.hours if self.hours else 0.0

    @property
    def handoffs_per_hour(self) -> float:
        return self.handoffs / self.hours if self.hours else 0.0

    @property
    def utilization(self) -> float | None:
        capacity = self.agents * self.hours
        return min(self.busy_hours / capacity, 1.0) if capacity else None

    @property
    def mean_queue(self) -> float:
        return self.stages.get("queue", 0.0) / self.hours if self.hours else 0.0

    @property
    def rework_rate(self) -> float | None:
        """Share of the tasks the role worked on that came back to it; None for a role
        that only reviews."""
        return len(self.reworked) / len(self.worked) if self.worked else None


def session_end(session: Session, end: datetime) -> datetime:
    """When a session stopped: its end, else its last heartbeat, else (still running or
    unknown) ``end``."""
    found = session.ended_at or session.last_heartbeat or end
    return max(found, session.started_at)


def throughput(
    tasks: Sequence[Task],
    sessions: Sequence[Session],
    owners: Sequence[Owner],
    states: Iterable[OpenState],
    start: datetime,
    end: datetime,
    review_roles: Sequence[str] = ("reviewer",),
    session_roles: Mapping[str, str] | None = None,
    wait_cause: Callable[[Task], str] | None = None,
) -> list[RoleThroughput]:
    """Throughput per role for the day ``[start, end)``, roles in name order.
    ``session_roles`` names the role of sessions whose owner is not in ``owners``;
    ``wait_cause`` names what a task waited for (its ``waiting`` hours count under it)."""
    hours = (end - start).total_seconds() / 3600
    roles_of_task = task_roles(tasks, owners)
    owner_roles = {o.id: o.role for o in owners}
    extra = session_roles or {}

    def own_role(s: Session) -> str | None:
        """The role of the session's owner; never the task's (rework attribution)."""
        return owner_roles.get(s.owner_id) or extra.get(s.id)

    def role_of_session(s: Session) -> str | None:
        return own_role(s) or roles_of_task.get(s.task_id)

    by_task: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        by_task[s.task_id].append(s)
    windows: dict[tuple[str, str], list[tuple[datetime, datetime]]] = defaultdict(list)
    for state in states:
        if state.task_id and (state.rule in OWNERLESS_RULES or state.rule in WAITING_RULES):
            kind = "ownerless" if state.rule in OWNERLESS_RULES else "waiting"
            windows[(state.task_id, kind)].append((state.since, state.last_seen))

    acc: dict[str, _Acc] = defaultdict(_Acc)
    for task in tasks:
        role = roles_of_task.get(task.id, UNKNOWN_ROLE)
        a = acc[role]
        mine = sorted(by_task.get(task.id, ()), key=lambda s: (s.started_at, s.id))
        stages = _task_stages(task, mine, windows, start, end, review_roles, role_of_session)
        done = task.status is Status.DONE and start <= task.status_since < end
        for stage, h in stages.items():
            a.stages[stage] += h
        if wait_cause is not None and stages.get("waiting"):
            cause = wait_cause(task)
            a.waits[cause] += stages["waiting"]
            a.waited[cause].add(task.id)
        if done:
            a.completed.append(task.id)
        if stages or done:
            a.active.append(task.id)
            # Rework: who worked the task within the day (non-review sessions), and for
            # each return the role that redid it (core.stages.rework_role).
            worked = {
                r
                for s in mine
                if (r := own_role(s)) and r not in review_roles and _overlaps(s, start, end)
            }
            if not worked and role not in review_roles:
                worked = {role}  # no session known: the task's own role did it
            for r in worked:
                acc[r].worked.add(task.id)
            for back in task.returns:
                if start <= back.at < end:
                    r = rework_role(task, back.at, mine, own_role, review_roles, end)
                    acc[r].reworked.add(task.id)
                    acc[r].worked.add(task.id)
                    acc[r].returns += 1
        a.reopened += sum(1 for at in task.reopened_at if start <= at < end)
        if _stage_at(task, mine, windows, end, review_roles, role_of_session, end) == "queue":
            a.queue_now += 1

    agents: dict[str, list[str]] = defaultdict(list)
    for o in owners:
        if o.kind is OwnerKind.AGENT:
            agents[o.role].append(o.id)
    busy: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for s in sessions:
        a_, b_ = max(s.started_at, start), min(session_end(s, end), end)
        if b_ > a_:
            busy[s.owner_id].append((a_, b_))
        role = role_of_session(s)
        ended = s.ended_at is not None and start <= s.ended_at < end
        declared = s.final_status is not None and s.final_status not in _UNDECLARED
        if role and ended and (declared or role in review_roles):
            acc[role].handoffs += 1  # a review that sends a task back declares nothing
    for role, ids in agents.items():
        acc[role].agents = len(ids)
        acc[role].busy = sum(_union(busy.get(i, [])) for i in ids)

    out = []
    for role in sorted(acc):
        a = acc[role]
        if not (a.agents or a.active or a.handoffs or a.worked):
            continue
        out.append(
            RoleThroughput(
                role=role,
                hours=hours,
                agents=a.agents,
                busy_hours=a.busy,
                completed=tuple(sorted(a.completed)),
                handoffs=a.handoffs,
                stages={s: a.stages[s] for s in STAGES if a.stages[s] > 0},
                active=tuple(sorted(a.active)),
                reworked=tuple(sorted(a.reworked)),
                worked=tuple(sorted(a.worked)),
                returns=a.returns,
                reopened=a.reopened,
                queue_now=a.queue_now,
                waits=dict(a.waits),
                waited={c: tuple(sorted(ids)) for c, ids in a.waited.items()},
            )
        )
    return out


def _overlaps(s: Session, start: datetime, end: datetime) -> bool:
    return s.started_at < end and session_end(s, end) > start


@dataclass(slots=True)
class _Acc:
    agents: int = 0
    busy: float = 0.0
    completed: list[str] = field(default_factory=list)
    handoffs: int = 0
    stages: Counter[str] = field(default_factory=Counter)
    active: list[str] = field(default_factory=list)
    reworked: set[str] = field(default_factory=set)
    worked: set[str] = field(default_factory=set)
    returns: int = 0
    reopened: int = 0
    queue_now: int = 0
    waits: Counter[str] = field(default_factory=Counter)
    waited: defaultdict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


def _union(intervals: list[tuple[datetime, datetime]]) -> float:
    """Hours covered by the intervals."""
    total = 0.0
    cur: tuple[datetime, datetime] | None = None
    for a, b in sorted(intervals):
        if cur is None or a > cur[1]:
            if cur is not None:
                total += (cur[1] - cur[0]).total_seconds()
            cur = (a, b)
        else:
            cur = (cur[0], max(cur[1], b))
    if cur is not None:
        total += (cur[1] - cur[0]).total_seconds()
    return total / 3600


def _task_stages(
    task: Task,
    sessions: Sequence[Session],
    windows: Mapping[tuple[str, str], Sequence[tuple[datetime, datetime]]],
    start: datetime,
    end: datetime,
    review_roles: Sequence[str],
    role_of_session: _RoleOf,
) -> dict[str, float]:
    """Hours of the task per stage within ``[start, end)`` (see the module docs)."""
    if task.status.is_terminal and task.status_since < start:
        return {}
    points = {end, task.status_since}
    for s in sessions:
        points.update((s.started_at, session_end(s, end)))
    for kind in ("ownerless", "waiting"):
        for a, b in windows.get((task.id, kind), ()):
            points.update((a, b))
    # from the task's first known moment: when it was created is not known
    first = max(start, min(points))
    cuts = sorted(p for p in points | {first} if first <= p <= end)
    out: Counter[str] = Counter()
    for a, b in pairwise(cuts):
        stage = _stage_at(task, sessions, windows, a, review_roles, role_of_session, end)
        if stage is not None:
            out[stage] += (b - a).total_seconds() / 3600
    return dict(out)


def _stage_at(
    task: Task,
    sessions: Sequence[Session],
    windows: Mapping[tuple[str, str], Sequence[tuple[datetime, datetime]]],
    at: datetime,
    review_roles: Sequence[str],
    role_of_session: _RoleOf,
    end: datetime,
) -> str | None:
    """The task's stage from ``at`` until the next known change."""
    if task.status.is_terminal and at >= task.status_since:
        return None
    for s in sessions:
        if s.started_at <= at < session_end(s, end):
            return "review" if role_of_session(s) in review_roles else "working"
    for kind in ("ownerless", "waiting"):
        if any(a <= at < b for a, b in windows.get((task.id, kind), ())):
            return kind
    if at >= task.status_since:
        return status_stage(task, task.status)
    before = [s for s in sessions if session_end(s, end) <= at]
    last = max(before, key=lambda s: (session_end(s, end), s.id)) if before else None
    status = last.final_status if last and last.final_status else Status.READY
    return status_stage(task, status) if status is not Status.IN_PROGRESS else "queue"
