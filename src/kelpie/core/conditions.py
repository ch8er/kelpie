"""Which conditions belong to a task right now. Shared by rules and by connectors that
attach conditions during snapshot assembly, so both read the linkage alike."""

from __future__ import annotations

from dataclasses import replace

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
    was seen. Platform-agnostic and pure: the board is the evidence.
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
    valid = isinstance(ref, str) and bool(ref) and wanted in Status.__members__.values()
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
