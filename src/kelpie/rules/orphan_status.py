"""Rule 3 — orphan statuses (incident 11).

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
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Owner, OwnerKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import DEFAULT_STALE_AFTER, is_live, live_session, undeclared_exit

NAME = "orphan_status"

_NEVER = {Status.BLOCKED, Status.WAITING, Status.BACKLOG, Status.READY}
_BUSY = {Status.IN_PROGRESS, Status.REVIEW}


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
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        orphans = [
            (task, gone, _open_dependents(task.id, snapshot))
            for task in snapshot.tasks
            if (gone := self._orphaned(task, snapshot)) is not None
        ]
        # Most urgent first, so the scarce idle agents go where they unblock the most.
        orphans.sort(key=lambda o: (-len(o[2]), o[0].status_since, o[0].id))
        idle = _idle_agents(snapshot, self.session_stale_after)
        actions: list[Action] = []
        for task, gone, waiting in orphans:
            actions.extend(self._act(task, snapshot, gone, waiting, idle))
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
        gone = task.owner_id is not None and bool(known) and task.owner_id not in known
        if task.owner_id is not None and not gone:
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
        who = f"owner {task.owner_id} is no longer known" if gone else "it has no owner"
        may_reassign = self.reassign and (not gone or self.reassign_unknown_owner)
        candidate = None
        if may_reassign and task.role is not None:
            candidate = next((o for o in idle if o.role == task.role), None)
            if candidate is not None:
                idle.remove(candidate)  # one task per idle agent per tick
        severity = (
            Severity.CRITICAL if len(waiting) >= self.critical_dependents else Severity.WARNING
        )
        key = f"{task.id}:{task.status_since.isoformat()}"
        if candidate is not None:
            then = f" Proposed owner: {candidate.id}."
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
            params={"dependents": len(waiting), "dependent_ids": waiting},
        )
        if candidate is not None:
            yield Action(
                ActionKind.REASSIGN,
                NAME,
                f"This '{task.status}' task had no owner for {hours:.1f}h; assigning it to "
                f"{candidate.id}, an idle {candidate.role}.",
                task_id=task.id,
                key=f"reassign:{key}",
                params={"owner_id": candidate.id},
            )


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
    for key in ("owned_statuses", "queue_statuses"):
        if key in config:
            kwargs[key] = _statuses(config.pop(key), key)
    if "critical_dependents" in config:
        n = config.pop("critical_dependents")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("critical_dependents must be a positive integer")
        kwargs["critical_dependents"] = n
    for key in ("reassign", "reassign_unknown_owner"):
        if key in config:
            kwargs[key] = _flag(config.pop(key), key)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return OrphanStatus(**kwargs)
