"""Rule 3 — orphan statuses and stop patterns (incident 11).

A task sits in a status where someone must be responsible for it, nobody is, and no
session is working on it. Nobody will move it, and everything that depends on it is
frozen. Kelpie alerts (critical when many open tasks wait on it) and proposes the task to an
idle agent of its role.

- ``owned_statuses`` (default ``in_progress``) are judged after ``orphan_after`` (30m).
- ``queue_statuses`` (default ``review``, ``accepted``) are hand-off queues: waiting there
  for a while is normal congestion, so they are judged only after ``queue_after`` (2h).
- Blocked and waiting tasks are left alone: their conditions say who moves them (rules 2,
  4). A task whose last session ended without a final status is rule 1's.
- An owner missing from the owner directory may live on another platform, so by default
  that only alerts; ``reassign_unknown_owner`` turns reassignment on for it.
- Only idle agents (no live session, no task in progress or review) are proposed; owner
  capacity is not modelled yet. With no idle candidate the alert says so.

The ownerless time is measured from ``status_since``: the snapshot does not record when the
owner was cleared, so this errs on the side of alerting a little early.

Stop patterns (P1): tasks that have an owner, or need none, and that nothing will move
again. Each is a lasting-state alert (``Action.state``) keyed by the task and the status
stay it is about, so it is announced once and resolved when the task moves; ``params.pattern``
names the pattern. A task gets at most one per-task alert (ownerless first, then b, then a).
A threshold set to ``false`` switches its pattern off. Tasks carrying a label in
``hold_labels`` are parked on purpose and skipped by every stop pattern; without its own list,
rule 3 uses rule 6's (``bottlenecks`` ``hold_labels`` and ``holds``). Tasks matching
``ignore_tasks`` (ids or glob patterns) are skipped by the whole rule: the escape hatch for
platforms without labels, such as Hermes, where holds cannot be expressed.

a) **In review with nothing driving it** (``review_idle_after``, 2h, counted from the later of
   the status change and the last session's end). Off until ``reviewer_roles`` names the
   roles that review: a ``review`` task whose known owner is an agent outside those roles,
   whose own role is not one of them either (no reviewer queue), with no live session and no
   open condition (an unmet condition that can still be met). A human owner counts as a
   reviewer: waiting for a person is the inbox's business.
b) **A successful run with no next action** (``next_action_after``, 30m): the task's last
   session ended declaring ``done``, ``review`` or ``accepted``, but the task still sits in
   another open status (not in progress, which rule 1 reports as an unapplied status, nor
   blocked or waiting) and nothing changed it since, so nobody acts on the result. With
   ``dependents_not_woken`` (off: a platform's backlog also holds work parked on purpose), a
   task finished ``done`` whose dependents still sit in ``backlog`` with every parent on the
   board finished: one alert per finished parent, naming them.
c) **A blocker chain that ends in a dead end** (``dead_end_chains``, on): tasks that wait,
   through other waiting tasks (dependencies, ``task_status`` and ``human_task``
   conditions), on a task the board does not show (archived, deleted or out of reach; see
   ``kelpie.rules._chains``). One alert per dead end, on the task directly in front of it,
   listing every task stuck behind it. Dead ends that rules 2 and 4 already report on the
   front task (a condition that can never be met, a condition Kelpie cannot see) are left to
   them; they carry the stuck tasks in ``params.stuck_ids``. A dependency on a cancelled or
   failed parent is not a dead end: a finished parent satisfies a dependency.
d) **Recovery tasks recovering themselves** (``max_recovery_depth``, 1). Needs recovery tasks
   to be recognisable: a label in ``recovery_labels`` (default ``recovery``) or a title
   matching one of ``recovery_titles``; Hermes has no labels, so there only titles work. A
   recovery task names what it recovers through the pattern's named group ``target``; a
   chain of such recovery tasks (the target of one is another, not finished ``done``) deeper
   than ``max_recovery_depth`` alerts. Open tasks that depend on a recovery task, or wait on
   it through a condition, are its targets too, but only count for a cycle: recovery tasks
   that merely follow each other (a staged plan) are not a loop unless they cycle. One alert
   per loop, keyed by the task the loop started from.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import ANNOUNCES
from kelpie.core.model import Condition, ConditionKind, Owner, OwnerKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._chains import NOT_FOUND, BlockerGraph
from kelpie.rules._tasks import (
    DEFAULT_STALE_AFTER,
    HANDOFF,
    is_live,
    last_exit,
    linked_conditions,
    live_session,
    responsible,
    undeclared_exit,
)

NAME = "orphan_status"

_NEVER = {Status.BLOCKED, Status.WAITING, Status.BACKLOG, Status.READY}
_BUSY = {Status.IN_PROGRESS, Status.REVIEW}
_SUCCESS = frozenset({Status.DONE, *HANDOFF})  # a session end that hands the task on
# in_progress: rule 1 reports a declared status that was never applied; parked tasks are
# their conditions' business.
_NOT_FOLLOWED_UP = frozenset({Status.IN_PROGRESS, Status.BLOCKED, Status.WAITING})
_TASK_REFS = frozenset({ConditionKind.TASK_STATUS, ConditionKind.HUMAN_TASK})
_SHOWN = 5  # ids named in an alert's text; params carry all of them


@dataclass(frozen=True, slots=True)
class OrphanStatus:
    orphan_after: timedelta = timedelta(minutes=30)
    queue_after: timedelta = timedelta(hours=2)
    owned_statuses: tuple[Status, ...] = (Status.IN_PROGRESS,)
    queue_statuses: tuple[Status, ...] = (Status.REVIEW, Status.ACCEPTED)
    critical_dependents: int = 3
    reassign: bool = True
    reassign_unknown_owner: bool = False
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    # Stop patterns; None (``false`` in config) switches one off.
    review_idle_after: timedelta | None = timedelta(hours=2)
    reviewer_roles: tuple[str, ...] = ()  # empty: review-undriven is off
    next_action_after: timedelta | None = timedelta(minutes=30)
    dependents_not_woken: bool = False
    dead_end_chains: bool = True
    max_recovery_depth: int | None = 1
    recovery_labels: tuple[str, ...] = ("recovery",)
    recovery_titles: tuple[re.Pattern[str], ...] = ()
    hold_labels: frozenset[str] = frozenset()
    ignore_tasks: tuple[str, ...] = ()  # task ids or glob patterns
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        orphans = [
            (task, gone, _open_dependents(task.id, snapshot))
            for task in snapshot.tasks
            if not self._ignored(task) and (gone := self._orphaned(task, snapshot)) is not None
        ]
        # Most urgent first, so the scarce idle agents go where they unblock the most.
        orphans.sort(key=lambda o: (-len(o[2]), o[0].status_since, o[0].id))
        idle = _idle_agents(snapshot, self.session_stale_after)
        actions: list[Action] = []
        for task, gone, waiting in orphans:
            actions.extend(self._act(task, snapshot, gone, waiting, idle))
        orphaned = {task.id for task, _, _ in orphans}
        for task in snapshot.tasks:
            if task.id in orphaned or task.status.is_terminal or self._skipped(task):
                continue
            found = self._unfollowed_run(task, snapshot) or self._undriven_review(task, snapshot)
            if found is not None:
                actions.append(found)
        if self.next_action_after is not None and self.dependents_not_woken:
            actions.extend(self._unwoken_dependents(snapshot, self.next_action_after))
        if self.dead_end_chains:
            actions.extend(self._dead_end_chains(snapshot))
        if self.max_recovery_depth is not None:
            actions.extend(self._recovery_loops(snapshot, self.max_recovery_depth))
        return actions

    def _orphaned(self, task: Task, snapshot: Snapshot) -> bool | None:
        """None if the task is fine, else whether its owner is missing from the directory."""
        if task.status in self.owned_statuses:
            after = self.orphan_after
        elif task.status in self.queue_statuses:
            after = self.queue_after
        else:
            return None
        known = {o.id for o in snapshot.owners}
        who = responsible(task)  # a task routed to someone is not ownerless
        gone = who is not None and bool(known) and who not in known
        if who is not None and not gone:
            return None
        if snapshot.now - task.status_since < after:
            return None
        if live_session(task.id, snapshot, self.session_stale_after) is not None:
            return None  # someone is on it, whatever the owner field says
        if undeclared_exit(task, snapshot, self.session_stale_after) is not None:
            return None  # rule 1 (exit contract) requeues it
        return gone

    def _act(
        self,
        task: Task,
        snapshot: Snapshot,
        gone: bool,
        waiting: list[str],
        idle: list[Owner],
    ) -> Iterable[Action]:
        hours = (snapshot.now - task.status_since).total_seconds() / 3600
        role = "owner" if task.owner_id else "assignee"
        who = f"{role} {responsible(task)} is no longer known" if gone else "it has no owner"
        may_reassign = self.reassign and (not gone or self.reassign_unknown_owner)
        candidate = None
        if may_reassign and task.role is not None:
            candidate = next((o for o in idle if o.role == task.role), None)
            if candidate is not None:
                idle.remove(candidate)  # one task per idle agent per tick
        severity = self._severity(len(waiting))
        key = f"{task.id}:{task.status_since.isoformat()}"
        if candidate is not None:
            then = f" Candidate owner: {candidate.id}, an idle {candidate.role}."
        elif may_reassign and task.role:
            then = f" No idle agent with role '{task.role}'; a human needs to pick it up."
        elif may_reassign:
            then = " The task has no role to pick an owner from; a human needs to pick it up."
        else:
            then = " A human needs to pick it up."
        yield Action(
            ActionKind.ALERT,
            NAME,
            f"Task {task.id} has been '{task.status}' for {hours:.1f}h and {who}; "
            f"{len(waiting)} open task(s) depend on it.{then}",
            task_id=task.id,
            severity=severity,
            key=f"orphan:{key}:{severity}",
            state=f"{NAME}:{key}",
            since=task.status_since,
            params={
                "dependents": len(waiting),
                "dependent_ids": waiting,
                **({ANNOUNCES: f"reassign:{key}"} if candidate is not None else {}),
            },
        )
        if candidate is not None:
            yield Action(
                ActionKind.REASSIGN,
                NAME,
                f"This '{task.status}' task had no owner for {hours:.1f}h; {candidate.id}, an "
                f"idle {candidate.role}, can take it.",
                task_id=task.id,
                key=f"reassign:{key}",
                params={"owner_id": candidate.id},
            )

    # -- stop patterns ------------------------------------------------------------------

    def _ignored(self, task: Task) -> bool:
        return any(fnmatchcase(task.id, p) for p in self.ignore_tasks)

    def _skipped(self, task: Task) -> bool:
        """Held on purpose, or ignored: no stop pattern looks at it."""
        return bool(self.hold_labels.intersection(task.labels)) or self._ignored(task)

    def _severity(self, dependents: int) -> Severity:
        return Severity.CRITICAL if dependents >= self.critical_dependents else Severity.WARNING

    def _stop_alert(
        self,
        pattern: str,
        task: Task,
        reason: str,
        since: datetime,
        snapshot: Snapshot,
        ident: str | None = None,
        waiting: list[str] | None = None,
        stay: bool = True,
        **params: Any,
    ) -> Action:
        """A lasting-state alert; severity by the open tasks behind ``task`` (``waiting``,
        else its open dependents). The state is keyed by ``ident`` (default the task id)
        and, with ``stay``, the task's current status stay."""
        if waiting is None:
            waiting = _open_dependents(task.id, snapshot)
            reason = f"{reason} {len(waiting)} open task(s) depend on {task.id}."
        severity = self._severity(len(waiting))
        key = f"{pattern}:{ident or task.id}"
        if stay:
            key = f"{key}:{task.status_since.isoformat()}"
        return Action(
            ActionKind.ALERT,
            NAME,
            reason,
            task_id=task.id,
            severity=severity,
            key=f"{key}:{severity}",
            state=f"{NAME}:{key}",
            since=since,
            params={"pattern": pattern, "dependents": len(waiting), **params},
        )

    def _unfollowed_run(self, task: Task, snapshot: Snapshot) -> Action | None:
        """b) The last session ended with success and the task did not move on."""
        after = self.next_action_after
        if after is None or task.status in _NOT_FOLLOWED_UP:
            return None
        end = last_exit(task, snapshot, self.session_stale_after)
        if end is None or end.silent:
            return None
        declared = end.session.final_status
        if declared is None or declared not in _SUCCESS or declared == task.status:
            return None
        if task.status_since > end.at or snapshot.now - end.at < after:
            return None
        return self._stop_alert(
            "no-next-action",
            task,
            f"Session {end.session.id} of task {task.id} ended {_human(snapshot.now - end.at)} "
            f"ago declaring '{declared}', but the task is still '{task.status}' and nothing "
            "moved it since: nobody acts on the result. Apply the declared status or send "
            "the task back.",
            end.at,
            snapshot,
            session_id=end.session.id,
            declared=declared.value,
        )

    def _undriven_review(self, task: Task, snapshot: Snapshot) -> Action | None:
        """a) In review with no session, no reviewer and no open condition."""
        after = self.review_idle_after
        if after is None or not self.reviewer_roles:
            return None  # which roles review is farm configuration; without it, off
        holder = responsible(task)
        if task.status is not Status.REVIEW or holder is None:
            return None
        owner = snapshot.owner(holder)
        if owner is None or owner.kind is OwnerKind.HUMAN or owner.role in self.reviewer_roles:
            return None  # unknown owner (the ownerless check's), or a reviewer has it
        if task.role in self.reviewer_roles:
            return None  # queued for reviewers: congestion is rule 6's
        if live_session(task.id, snapshot, self.session_stale_after) is not None:
            return None
        if undeclared_exit(task, snapshot, self.session_stale_after) is not None:
            return None  # rule 1 (exit contract) requeues it
        end = last_exit(task, snapshot, self.session_stale_after)
        quiet_since = max(task.status_since, end.at) if end is not None else task.status_since
        if snapshot.now - quiet_since < after:
            return None
        conditions, _ = linked_conditions(task, snapshot)
        if any(_open(c) for c in conditions):
            return None  # waiting for something that can still happen
        return self._stop_alert(
            "review-undriven",
            task,
            f"Task {task.id} has been in review for "
            f"{_human(snapshot.now - task.status_since)} and nothing is driving it: no "
            f"session, its owner {owner.id} ({owner.role}) is not a reviewer, no reviewer "
            "queue and no open condition. Assign a reviewer or move it on.",
            quiet_since,
            snapshot,
            owner_id=owner.id,
        )

    def _unwoken_dependents(self, snapshot: Snapshot, after: timedelta) -> Iterable[Action]:
        """b) A task finished done and its dependents still sit in backlog."""
        by_id = {t.id: t for t in snapshot.tasks}
        parents: dict[str, set[str]] = {}
        for d in snapshot.dependencies:
            if d.depends_on in by_id and d.depends_on != d.task_id:
                parents.setdefault(d.task_id, set()).add(d.depends_on)
        stuck: dict[str, list[str]] = {}
        for tid, ids in sorted(parents.items()):
            task = by_id.get(tid)
            if task is None or task.status is not Status.BACKLOG or self._skipped(task):
                continue
            ps = [by_id[p] for p in ids]
            if not all(p.status.is_terminal for p in ps):
                continue
            if not any(p.status is Status.DONE for p in ps):
                continue
            last = max(ps, key=lambda p: (p.status_since, p.id))
            if task.status_since > last.status_since:
                continue  # put back in backlog after its parents finished: deliberate
            if snapshot.now - last.status_since < after:
                continue
            if live_session(tid, snapshot, self.session_stale_after) is None:
                stuck.setdefault(last.id, []).append(tid)
        for pid, waiting in sorted(stuck.items()):
            parent = by_id[pid]
            if self._skipped(parent):
                continue
            yield self._stop_alert(
                "dependents-not-woken",
                parent,
                f"Task {pid} finished {_human(snapshot.now - parent.status_since)} ago, but "
                f"{len(waiting)} task(s) depending on it still sit in backlog with every "
                f"parent finished: {_names(waiting)}. Nothing promoted them to ready.",
                parent.status_since,
                snapshot,
                dependent_ids=waiting,
            )

    def _dead_end_chains(self, snapshot: Snapshot) -> Iterable[Action]:
        """c) Chains of waiting tasks that end in something that can never come."""
        graph = BlockerGraph(snapshot, self.session_stale_after)
        for tid in sorted(graph.edges):
            front = graph.by_id[tid]
            if self._skipped(front):
                continue
            # Conditions that can never be met, or that Kelpie cannot see, in front of a task
            # are rules 2 and 4's alerts (with the stuck tasks); a vanished task is rule 3's.
            dead_edges = [
                e for e in graph.edges[tid] if e.dead is not None and e.dead.kind == NOT_FOUND
            ]
            behind = graph.behind(tid) if dead_edges else {}
            behind = {k: p for k, p in behind.items() if not self._skipped(graph.by_id[k])}
            if not behind:
                continue  # no dead end of its own, or nothing (not held) waits behind it
            stuck = sorted(behind)
            longest = max(behind.values(), key=lambda p: (len(p), p))
            for edge in dead_edges:
                assert edge.dead is not None
                dead = edge.dead
                chain = [*longest, dead.ref]
                since = max(front.status_since, dead.since or front.status_since)
                yield self._stop_alert(
                    "dead-end-chain",
                    front,
                    f"Blocker chain {' -> '.join(chain)} ends in a dead end: {dead.ref} "
                    f"{dead.why} ({tid} {edge.via}). {len(stuck)} task(s) wait behind "
                    f"{tid}: {_names(stuck)}. Nothing will move them until a human replaces "
                    "the blocker.",
                    since,
                    snapshot,
                    ident=f"{tid}:{dead.ref}",
                    waiting=stuck,
                    dead_end=dead.ref,
                    chain=chain,
                    stuck_ids=stuck,
                )

    def _is_recovery(self, task: Task) -> bool:
        labels = {label.lower() for label in task.labels}
        if any(label.lower() in labels for label in self.recovery_labels):
            return True
        return any(p.search(task.title) for p in self.recovery_titles)

    def _recovery_links(
        self, task: Task, snapshot: Snapshot, by_id: dict[str, Task]
    ) -> tuple[list[str], list[str]]:
        """What a recovery task recovers: the ids its title names (explicit), and the open
        tasks that wait on it through a dependency or a condition (implicit)."""
        explicit: set[str] = set()
        for pattern in self.recovery_titles:
            match = pattern.search(task.title)
            if match and "target" in pattern.groupindex and match.group("target"):
                explicit.add(match.group("target"))
        # A target that finished done was recovered: no loop through it.
        explicit = {x for x in explicit if x not in by_id or by_id[x].status is not Status.DONE}
        waiting = {d.task_id for d in snapshot.dependencies if d.depends_on == task.id}
        waiting |= {
            c.task_id
            for c in snapshot.conditions
            if c.task_id is not None and _names_task(c, task.id)
        }
        implicit = {x for x in waiting if x in by_id and not by_id[x].status.is_terminal}
        return sorted(explicit), sorted(implicit)

    def _recovery_loops(self, snapshot: Snapshot, max_depth: int) -> Iterable[Action]:
        """d) Recovery tasks whose target is a recovery task, or themselves."""
        by_id = {t.id: t for t in snapshot.tasks}
        recovery = {t.id: t for t in snapshot.tasks if self._is_recovery(t)}
        links = {tid: self._recovery_links(t, snapshot, by_id) for tid, t in recovery.items()}
        chained = {tid: [x for x in ex if x in recovery] for tid, (ex, _) in links.items()}
        anyway = {
            tid: sorted({x for x in (*ex, *im) if x in recovery}) for tid, (ex, im) in links.items()
        }
        inner = {  # explicitly recovered by another open recovery task
            x
            for tid, xs in chained.items()
            if not recovery[tid].status.is_terminal
            for x in xs
            if x != tid
        }
        loops: dict[str, tuple[list[str], bool]] = {}
        for tid in sorted(recovery):
            task = recovery[tid]
            if task.status.is_terminal or self._skipped(task):
                continue
            cycle = _cycle(tid, anyway)
            if cycle is not None:
                chain, origin = cycle, min(cycle)
            elif tid in inner:
                continue  # an inner link of a longer chain: reported from its top
            else:
                chain = _follow(tid, chained)
                ex, im = links[chain[-1]]
                origin = next((x for x in (*ex, *im) if x not in recovery), chain[-1])
                if len(chain) <= max_depth:
                    continue
            best = loops.get(origin)
            if best is None or len(chain) > len(best[0]):
                loops[origin] = (chain, cycle is not None)
        for origin, (chain, cycle) in sorted(loops.items()):
            top = recovery[chain[0]]
            if cycle:
                what = (
                    f"Recovery tasks recover each other in a cycle: {' -> '.join(chain)}; "
                    "none of them will ever reach the work they were opened for."
                )
            else:
                path = [*chain, origin] if origin != chain[-1] else chain
                what = (
                    f"Recovery tasks recover each other {len(chain)} deep (limit "
                    f"{max_depth}): {' -> '.join(path)}. Each recovery got stuck and another "
                    f"was opened for it instead of for {origin}."
                )
            yield self._stop_alert(
                "recovery-loop",
                top,
                f"{what} A human needs to look at the original problem.",
                min(recovery[t].status_since for t in chain),
                snapshot,
                ident=origin,  # stable while the loop grows a new top
                waiting=_open_dependents(origin, snapshot),
                stay=False,
                origin=origin,
                chain=chain,
                cycle=cycle,
            )


def _names_task(condition: Condition, task_id: str) -> bool:
    return condition.kind in _TASK_REFS and condition.params.get("task") == task_id


def _follow(start: str, links: dict[str, list[str]]) -> list[str]:
    """The chain of recovery tasks from ``start``, following the first link not yet in it."""
    chain = [start]
    while (nxt := next((x for x in links[chain[-1]] if x not in chain), None)) is not None:
        chain.append(nxt)
    return chain


def _cycle(start: str, links: dict[str, list[str]]) -> list[str] | None:
    """The shortest cycle through ``start`` (``[start, ..., start]``), or None."""
    queue: deque[list[str]] = deque([[start]])
    seen = {start}
    while queue:
        path = queue.popleft()
        for x in links.get(path[-1], ()):
            if x == start:
                return [*path, start]
            if x not in seen:
                seen.add(x)
                queue.append([*path, x])
    return None


def _open(condition: Condition) -> bool:
    """An unmet condition that can still be met and that Kelpie can evaluate."""
    params = condition.params
    return (
        condition.satisfied is False
        and params.get("terminal") is not True
        and "error" not in params
    )


def _names(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:_SHOWN])
    return shown + (f" and {len(ids) - _SHOWN} more" if len(ids) > _SHOWN else "")


def _human(d: timedelta) -> str:
    minutes = max(0, int(d.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


def _open_dependents(task_id: str, snapshot: Snapshot) -> list[str]:
    """Non-terminal tasks that depend on ``task_id``, directly or transitively."""
    found: set[str] = set()
    frontier = [task_id]
    while frontier:
        for dep in snapshot.dependents(frontier.pop()):
            if dep.id != task_id and dep.id not in found and not dep.status.is_terminal:
                found.add(dep.id)
                frontier.append(dep.id)
    return sorted(found)


def _idle_agents(snapshot: Snapshot, stale_after: timedelta) -> list[Owner]:
    """Agents with nothing in progress or review and no live session, by id."""
    busy = {t.owner_id for t in snapshot.tasks if t.status in _BUSY}
    busy |= {s.owner_id for s in snapshot.sessions if is_live(s, snapshot.now, stale_after)}
    idle = [o for o in snapshot.owners if o.kind is OwnerKind.AGENT and o.id not in busy]
    return sorted(idle, key=lambda o: o.id)


def _duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return result


def _statuses(value: Any, name: str) -> tuple[Status, ...]:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{name} must be a list of statuses")
    statuses = tuple(Status(s) for s in value)
    if bad := [s for s in statuses if s.is_terminal or s in _NEVER]:
        raise ValueError(f"{name} cannot include {', '.join(bad)}")
    return statuses


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


@register(NAME)
def build(config: dict[str, Any]) -> OrphanStatus:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("orphan_after", "queue_after", "session_stale_after"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    for key in ("review_idle_after", "next_action_after"):
        if key in config:
            value = config.pop(key)
            kwargs[key] = None if value is False else _duration(value, key)  # false: off
    for key in ("owned_statuses", "queue_statuses"):
        if key in config:
            kwargs[key] = _statuses(config.pop(key), key)
    if "critical_dependents" in config:
        n = config.pop("critical_dependents")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("critical_dependents must be a positive integer")
        kwargs["critical_dependents"] = n
    if "max_recovery_depth" in config:
        n = config.pop("max_recovery_depth")
        if n is not False and (not isinstance(n, int) or isinstance(n, bool) or n < 1):
            raise ValueError("max_recovery_depth must be a positive integer or false")
        kwargs["max_recovery_depth"] = None if n is False else n
    for key in ("reassign", "reassign_unknown_owner", "dead_end_chains", "dependents_not_woken"):
        if key in config:
            kwargs[key] = _flag(config.pop(key), key)
    for key in ("reviewer_roles", "recovery_labels", "ignore_tasks"):
        if key in config:
            kwargs[key] = _strings(config.pop(key), key)
    if "hold_labels" in config:
        kwargs["hold_labels"] = frozenset(_strings(config.pop("hold_labels"), "hold_labels"))
    if "recovery_titles" in config:
        patterns = _strings(config.pop("recovery_titles"), "recovery_titles")
        try:
            kwargs["recovery_titles"] = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        except re.error as exc:
            raise ValueError(f"recovery_titles: invalid pattern: {exc}") from exc
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return OrphanStatus(**kwargs)
