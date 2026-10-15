"""Rule 4 — stale blocks (incidents 1, 8).

A blocked task waits for something in the outside world: CI to pass, a PR to merge, a
guard to clear. Connectors keep the linked conditions up to date. When every condition the
block depends on holds, the block is stale: Kelpie clears it instead of letting the task
sit for hours.

Kelpie only clears a block on *fresh* evidence: every linked condition must have been
checked after the block was set and within ``max_condition_age``. Data from before the block
(CI was green for the previous commit) or from a connector that stopped polling never
clears a block. A grace period, counted from when the last condition was met but never
from before the block started, avoids racing an agent or the platform that is about to
clear the block itself.

Conditions are linked by the shared rule semantics (``kelpie.rules._tasks``). A block with no
linked condition is left alone; with ``unlinked_alert_after`` set, such a block gets an alert
once it is that old, since nothing Kelpie can see will ever clear it. ``waiting`` tasks are
rule 2's.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import linked_conditions

NAME = "stale_blocks"


@dataclass(frozen=True, slots=True)
class StaleBlocks:
    unblock_grace: timedelta = timedelta(minutes=5)
    max_condition_age: timedelta = timedelta(minutes=15)
    unlinked_alert_after: timedelta | None = None
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is Status.BLOCKED and task.block is not None:
                actions.extend(self._check(task, snapshot))
        return actions

    def _check(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        block = task.block
        assert block is not None
        conditions, missing = linked_conditions(task, snapshot)
        if not conditions:
            if missing is None:
                yield from self._unlinked(task, snapshot)
            return  # a condition Kelpie cannot see: leave the block alone
        if not all(c.satisfied for c in conditions):
            return
        for c in conditions:
            checked = c.checked_at
            if checked is None or checked < block.since:
                return  # no evidence gathered since the block was set
            if snapshot.now - checked > self.max_condition_age:
                return  # the connector has not looked recently
        met_at = max(max(c.satisfied_at or block.since, block.since) for c in conditions)
        if snapshot.now - met_at < self.unblock_grace:
            return
        exact = all(c.satisfied_at is not None for c in conditions)
        outlived = snapshot.now - met_at
        met = ", ".join(f"{c.id} ({c.kind})" for c in conditions)
        by = _human(outlived) if exact else f"at least {_human(outlived)}"
        yield Action(
            ActionKind.UNBLOCK,
            NAME,
            f"The block '{block.reason}' is stale: its condition is met ({met}) and the block "
            f"outlived its cause by {by}. Clearing it.",
            task_id=task.id,
            key=f"unblock:{task.id}:{block.since.isoformat()}",
            params={
                "conditions": [c.id for c in conditions],
                "outlived_minutes": int(outlived.total_seconds() // 60),
            },
        )

    def _unlinked(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        block = task.block
        assert block is not None
        if self.unlinked_alert_after is None:
            return
        age = snapshot.now - block.since
        if age >= self.unlinked_alert_after:
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} has been blocked for {_human(age)} on '{block.reason}' with "
                "no condition Kelpie can check, so nothing will clear it automatically.",
                task_id=task.id,
                severity=Severity.WARNING,
                key=f"unlinked:{task.id}:{block.since.isoformat()}",
            )


def _human(d: timedelta) -> str:
    minutes = max(0, int(d.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


def _duration(value: Any, name: str, allow_zero: bool = False) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result < timedelta(0) or (result == timedelta(0) and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


@register(NAME)
def build(config: dict[str, Any]) -> StaleBlocks:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "unblock_grace" in config:
        kwargs["unblock_grace"] = _duration(config.pop("unblock_grace"), "unblock_grace", True)
    for key in ("max_condition_age", "unlinked_alert_after"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return StaleBlocks(**kwargs)
