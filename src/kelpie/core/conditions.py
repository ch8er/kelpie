"""Which conditions belong to a task right now. Shared by rules and by connectors that
attach conditions during snapshot assembly, so both read the linkage alike."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from kelpie.core.model import Block, Condition, ConditionKind, Snapshot, Status, Task

# Keys :func:`check_task_status` writes into ``Condition.params``; cleared before each check.
_TASK_STATUS_RESULTS = ("observed_status", "error", "terminal")


def bare_block(task: Task, snapshot: Snapshot) -> bool:
    """A blocked task whose block names no condition and has none linked."""
    if task.status is not Status.BLOCKED or task.block is None or task.block.condition_id:
        return False
    conditions, _ = linked_conditions(task, snapshot)
    return not conditions


def linked_conditions(task: Task, snapshot: Snapshot) -> tuple[list[Condition], str | None]:
    """Conditions of the task's current block or wait, and a referenced id Kelpie cannot see.

    The condition named by the block wins. Otherwise conditions linked by ``task_id`` count,
    except those registered before the task entered its current status (left over from an
    earlier wait). Returns ``([], missing_id)`` when the block names an unknown condition.
    """
    if task.block is not None and task.block.condition_id:
        c = snapshot.condition(task.block.condition_id)
        return ([c], None) if c is not None else ([], task.block.condition_id)
    current = [
        c
        for c in snapshot.conditions
        if c.task_id == task.id
        and (c.registered_at is None or c.registered_at >= task.status_since)
    ]
    return sorted(current, key=lambda c: c.id), None


def check_task_status(snapshot: Snapshot) -> Snapshot:
    """Evaluate ``task_status`` conditions against the board, as a snapshot step.

    A condition with ``params.task`` (a task id on the board) and ``params.status`` (a
    status value, default ``done``) holds once that task has that status. A task that ended
    in another terminal status can never get there: ``params.terminal``. A task not on the
    board, or bad params, is unknown (``params.error``). ``observed_status`` records what
    was seen. ``satisfied_at`` is when the task entered that status, which may be before the
    wait began (rules judge freshness by ``checked_at``). A condition naming its own task is
    invalid. Platform-agnostic and pure: the board is the evidence.
    """
    if not any(c.kind is ConditionKind.TASK_STATUS for c in snapshot.conditions):
        return snapshot
    return replace(
        snapshot,
        conditions=tuple(
            _check_task_status(c, snapshot) if c.kind is ConditionKind.TASK_STATUS else c
            for c in snapshot.conditions
        ),
    )


def _check_task_status(condition: Condition, snapshot: Snapshot) -> Condition:
    params = {k: v for k, v in condition.params.items() if k not in _TASK_STATUS_RESULTS}
    ref, wanted = params.get("task"), params.get("status", Status.DONE.value)
    valid = (
        isinstance(ref, str)
        and bool(ref)
        and ref != condition.task_id  # a task cannot wait for itself
        and wanted in Status.__members__.values()
    )
    target = snapshot.task(ref) if valid and isinstance(ref, str) else None
    if target is None:
        params["error"] = "not_found" if valid else "invalid_params"
        return replace(
            condition, params=params, satisfied=None, satisfied_at=None, checked_at=snapshot.now
        )
    params["observed_status"] = target.status.value
    if target.status == wanted:
        since = condition.satisfied_at if condition.satisfied else None
        return replace(
            condition,
            params=params,
            satisfied=True,
            satisfied_at=since or target.status_since,
            checked_at=snapshot.now,
        )
    if target.status.is_terminal:
        params["terminal"] = True  # ended another way: it will never get there
    return replace(
        condition, params=params, satisfied=False, satisfied_at=None, checked_at=snapshot.now
    )


# --- carrying a failed step's conditions forward ------------------------------------------


# A failed step's conditions are carried for this long at most (from their check): past it
# the tick is as if the step attached and checked nothing, as before carrying existed.
CARRY_FOR = timedelta(hours=6)
# Only waits and blocks are carried: their alerts are what a failed tick would flap. A ready
# task's gate or a review's conditions are left out, so carrying never parks anything.
_CARRIED_STATUSES = frozenset({Status.WAITING, Status.BLOCKED})


@dataclass(frozen=True, slots=True)
class Link:
    """A block a step linked to a condition it attached, as the block was before."""

    condition_id: str
    task_id: str
    block: Block


@dataclass(frozen=True, slots=True)
class Contribution:
    """What a condition step put into the snapshot on its last good tick: the conditions it
    attached or checked (``inputs`` holds a checked one as the step was given it), and the
    blocks it linked to the ones it attached."""

    conditions: tuple[Condition, ...] = ()
    inputs: tuple[Condition, ...] = ()
    links: tuple[Link, ...] = ()


def contribution(before: Snapshot, after: Snapshot) -> Contribution:
    """What a step changed between its input ``before`` and its output ``after``."""
    was = {c.id: c for c in before.conditions}
    changed = tuple(c for c in after.conditions if was.get(c.id) != c)
    inputs = tuple(was[c.id] for c in changed if c.id in was)
    blocks = {t.id: t.block for t in before.tasks}
    links = tuple(
        Link(t.block.condition_id, t.id, old)
        for t in after.tasks
        if t.block is not None
        and t.block.condition_id
        and t.block.condition_id not in was
        and (old := blocks.get(t.id)) is not None
        and old.condition_id is None
    )
    return Contribution(changed, inputs, links)


def widen(new: Contribution, last: Contribution, snapshot: Snapshot) -> Contribution:
    """``new``, from a tick that did not see the whole farm (``snapshot``: an adapter or an
    agent missing), with what ``last`` held for tasks not on that board, so a failure on the
    next tick still finds them; within :data:`CARRY_FOR` of their check, so what a farm that
    is never whole keeps stays bounded."""
    on_board = {t.id for t in snapshot.tasks}
    taken = {c.id for c in new.conditions}
    task_of = {link.condition_id: link.task_id for link in last.links}
    keep = {
        c.id
        for c in last.conditions
        if c.id not in taken
        and task_of.get(c.id, c.task_id) not in on_board
        and _recent(c, snapshot.now)
    }
    return Contribution(
        new.conditions + tuple(c for c in last.conditions if c.id in keep),
        new.inputs + tuple(c for c in last.inputs if c.id in keep),
        new.links + tuple(link for link in last.links if link.condition_id in keep),
    )


def carry_forward(snapshot: Snapshot, last: Contribution) -> Snapshot:
    """``snapshot`` with what a step put in on its last good tick, for a tick it failed.

    Only checks that found a condition unmet or unknown, for a task that waits or is
    blocked, are carried: they keep their ``checked_at``, so they go stale on their own and
    no rule acts on them once they are older than its ``max_condition_age``, and meanwhile
    they keep the alerts about that wait or block quiet. A check that found a condition met
    is never carried, nor anything for a task in another status (a ready task's gate), so a
    failed step never wakes, unblocks, releases or parks anything. A check is carried only
    onto a condition still exactly as the step was given it (not one registered again,
    changed or checked meanwhile) and named once; an attached condition only onto the same
    block, unchanged since it was linked. Nothing older than :data:`CARRY_FOR` is carried.
    """
    conditions = list(snapshot.conditions)
    counts = Counter(c.id for c in conditions)
    at = {c.id: i for i, c in enumerate(conditions)}
    tasks = list(snapshot.tasks)
    where = {t.id: i for i, t in enumerate(tasks) if t.status in _CARRIED_STATUSES}
    held = {
        t.block.condition_id
        for t in tasks
        if t.id in where and t.block is not None and t.block.condition_id
    }
    inputs = {c.id: c for c in last.inputs}
    links = {link.condition_id: link for link in last.links}
    added: list[Condition] = []
    for old in last.conditions:
        if old.satisfied or not _recent(old, snapshot.now):
            continue
        if old.id in at:
            now = conditions[at[old.id]]
            if (
                counts[old.id] == 1
                and inputs.get(old.id) == now
                and (old.id in held or now.task_id in where)
            ):
                conditions[at[old.id]] = old
            continue
        link = links.get(old.id)
        i = where.get(link.task_id) if link is not None else None
        if link is None or i is None or tasks[i].block != link.block:
            continue
        tasks[i] = replace(tasks[i], block=replace(link.block, condition_id=old.id))
        added.append(old)
    return replace(snapshot, tasks=tuple(tasks), conditions=tuple(conditions) + tuple(added))


def _recent(condition: Condition, now: datetime) -> bool:
    return condition.checked_at is not None and now - condition.checked_at <= CARRY_FOR
