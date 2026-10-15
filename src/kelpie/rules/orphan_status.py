"""Rule 3 — orphan statuses (incident 11).

A task sits in a status that needs an owner (in progress, review, accepted, blocked) but
has none, or its owner is no longer known, for longer than ``orphan_after``. Nobody will
move it, and everything that depends on it is frozen. Kelpie alerts (critical when many
tasks wait on it) and reassigns it to the least loaded owner of the task's role.

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

NAME = "orphan_status"

DEFAULT_OWNED_STATUSES = (Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED, Status.BLOCKED)


@dataclass(frozen=True, slots=True)
class OrphanStatus:
    orphan_after: timedelta = timedelta(minutes=30)
    critical_dependents: int = 3
    owned_statuses: tuple[Status, ...] = DEFAULT_OWNED_STATUSES
    reassign: bool = True
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        known = {o.id for o in snapshot.owners}
        for task in snapshot.tasks:
            if task.status not in self.owned_statuses:
                continue
            gone = task.owner_id is not None and bool(known) and task.owner_id not in known
            if task.owner_id is not None and not gone:
                continue
            if snapshot.now - task.status_since < self.orphan_after:
                continue
            actions.extend(self._act(task, snapshot, gone))
        return actions

    def _act(self, task: Task, snapshot: Snapshot, gone: bool) -> Iterable[Action]:
        waiting = _open_dependents(task.id, snapshot)
        hours = (snapshot.now - task.status_since).total_seconds() / 3600
        who = f"owner {task.owner_id} is no longer known" if gone else "it has no owner"
        candidate = _least_loaded(task, snapshot) if self.reassign else None
        key = f"{task.id}:{task.status_since.isoformat()}"
        critical = len(waiting) >= self.critical_dependents
        then = f" Reassigning it to {candidate.id}." if candidate else self._no_candidate(task)
        yield Action(
            ActionKind.ALERT,
            NAME,
            f"Task {task.id} has been '{task.status}' for {hours:.1f}h and {who}; "
            f"{len(waiting)} open task(s) depend on it.{then}",
            task_id=task.id,
            severity=Severity.CRITICAL if critical else Severity.WARNING,
            key=f"orphan:{key}",
            params={"dependents": len(waiting), "dependent_ids": waiting},
        )
        if candidate is not None:
            yield Action(
                ActionKind.REASSIGN,
                NAME,
                f"Nobody owned this '{task.status}' task for {hours:.1f}h; assigning it to "
                f"{candidate.id} ({candidate.role}), the least loaded owner of that role.",
                task_id=task.id,
                key=f"reassign:{key}",
                params={"owner_id": candidate.id},
            )

    def _no_candidate(self, task: Task) -> str:
        if not self.reassign:
            return ""
        role = f"role '{task.role}'" if task.role else "an unknown role"
        return f" No owner with {role} to reassign it to; a human needs to pick it up."


def _open_dependents(task_id: str, snapshot: Snapshot) -> list[str]:
    """Non-terminal tasks that depend on ``task_id``, directly or transitively."""
    found: set[str] = set()
    frontier = [task_id]
    while frontier:
        for dep in snapshot.dependents(frontier.pop()):
            if dep.id not in found and not dep.status.is_terminal:
                found.add(dep.id)
                frontier.append(dep.id)
    return sorted(found)


def _least_loaded(task: Task, snapshot: Snapshot) -> Owner | None:
    """An agent owner of the task's role with the fewest open tasks (ties by id)."""
    if task.role is None:
        return None
    pool = [o for o in snapshot.owners if o.role == task.role and o.kind is OwnerKind.AGENT]
    load = {o.id: 0 for o in pool}
    for t in snapshot.tasks:
        if t.owner_id in load and not t.status.is_terminal:
            load[t.owner_id] += 1
    return min(pool, key=lambda o: (load[o.id], o.id), default=None)


@register(NAME)
def build(config: dict[str, Any]) -> OrphanStatus:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "orphan_after" in config:
        value = config.pop("orphan_after")
        try:
            kwargs["orphan_after"] = parse_duration(value)
        except (AttributeError, TypeError) as exc:
            raise ValueError(f"orphan_after: invalid duration {value!r}") from exc
    if "critical_dependents" in config:
        n = config.pop("critical_dependents")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("critical_dependents must be a positive integer")
        kwargs["critical_dependents"] = n
    if "owned_statuses" in config:
        raw = config.pop("owned_statuses")
        if not isinstance(raw, list | tuple) or not raw:
            raise ValueError("owned_statuses must be a non-empty list of statuses")
        statuses = tuple(Status(s) for s in raw)
        if any(s.is_terminal for s in statuses):
            raise ValueError("owned_statuses cannot include terminal statuses")
        kwargs["owned_statuses"] = statuses
    if "reassign" in config:
        flag = config.pop("reassign")
        if not isinstance(flag, bool):
            raise ValueError("reassign must be true or false")
        kwargs["reassign"] = flag
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return OrphanStatus(**kwargs)
