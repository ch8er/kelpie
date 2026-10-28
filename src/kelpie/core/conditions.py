"""Which conditions belong to a task right now. Shared by rules and by connectors that
attach conditions during snapshot assembly, so both read the linkage alike."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from kelpie.core.model import Condition, ConditionKind, Snapshot, Status, Task

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


@dataclass(frozen=True, slots=True)
class Link:
    """A block a step linked to a condition it attached, as the block was then."""

    task_id: str
    condition_id: str
    reason: str
    since: datetime


@dataclass(frozen=True, slots=True)
class Contribution:
    """What a condition step put into the snapshot on its last good tick: the conditions it
    attached or checked (``inputs`` holds a checked one as the step was given it), and the
    blocks it linked to the conditions it attached."""

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
        Link(t.id, t.block.condition_id, old.reason, old.since)
        for t in after.tasks
        if t.block is not None
        and t.block.condition_id
        and (old := blocks.get(t.id)) is not None
        and old.condition_id != t.block.condition_id
    )
    return Contribution(changed, inputs, links)


def carry_forward(snapshot: Snapshot, last: Contribution) -> Snapshot:
    """``snapshot`` with what a step put in on its last good tick, for a tick it failed.

    Its checks keep their ``checked_at``, so they go stale on their own and no rule acts on
    them once they are older than its ``max_condition_age``; the conditions it attached come
    back with their links. A check is carried only onto a condition that is still exactly
    as the step was given it (not one registered again, changed or checked meanwhile), and
    an attached condition only onto a block unchanged since it was linked, or (linked by
    ``task_id`` alone) while its task is on the board.
    """
    conditions = list(snapshot.conditions)
    at = {c.id: i for i, c in enumerate(conditions)}
    tasks = list(snapshot.tasks)
    where = {t.id: i for i, t in enumerate(tasks)}
    inputs = {c.id: c for c in last.inputs}
    links = {link.condition_id: link for link in last.links}
    added: list[Condition] = []
    for old in last.conditions:
        if old.id in at:
            if inputs.get(old.id) == conditions[at[old.id]]:
                conditions[at[old.id]] = old
            continue
        if old.id in inputs:
            continue  # the step checked it, and it is gone from the board
        link = links.get(old.id)
        if link is None:
            if old.task_id is not None and old.task_id in where:
                added.append(old)
            continue
        i = where.get(link.task_id)
        block = tasks[i].block if i is not None else None
        if (
            i is not None
            and block is not None
            and block.condition_id is None
            and (block.reason, block.since) == (link.reason, link.since)
        ):
            tasks[i] = replace(tasks[i], block=replace(block, condition_id=old.id))
            added.append(old)
    return replace(snapshot, tasks=tuple(tasks), conditions=tuple(conditions) + tuple(added))
