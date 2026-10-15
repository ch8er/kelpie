"""Rule 4 — stale blocks (incidents 1, 8).

A blocked task waits for something in the outside world: CI to pass, a PR to merge, a
guard to clear. Connectors keep the linked conditions up to date. When every condition the
block depends on holds, the block is stale: Kelpie clears it instead of letting the task
sit for hours. A grace period after the condition was met avoids racing an agent or the
platform that is about to clear the block itself.

Conditions are linked by ``block.condition_id`` or by ``condition.task_id``. A block with no
linked condition, or with any condition unsatisfied or not yet checked, is left alone: only
external state that Kelpie can see may clear a block. ``waiting`` tasks (rule 2) are not
touched here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Condition, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register

NAME = "stale_blocks"


@dataclass(frozen=True, slots=True)
class StaleBlocks:
    unblock_grace: timedelta = timedelta(minutes=5)
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
        conditions = _linked(task, snapshot)
        if not conditions or not all(c.satisfied for c in conditions):
            return
        met_at = max(_met_at(c, block.since) for c in conditions)
        if snapshot.now - met_at < self.unblock_grace:
            return
        # The block cannot be stale for longer than it existed.
        outlived = snapshot.now - max(met_at, block.since)
        met = ", ".join(f"{c.id} ({c.kind})" for c in conditions)
        yield Action(
            ActionKind.UNBLOCK,
            NAME,
            f"The block '{block.reason}' is stale: its condition is met ({met}) and the block "
            f"outlived its cause by {_human(outlived)}. Clearing it.",
            task_id=task.id,
            key=f"unblock:{task.id}:{block.since.isoformat()}",
            params={
                "conditions": [c.id for c in conditions],
                "outlived_minutes": int(outlived.total_seconds() // 60),
            },
        )


def _linked(task: Task, snapshot: Snapshot) -> list[Condition]:
    found = {c.id: c for c in snapshot.conditions if c.task_id == task.id}
    if task.block is not None and task.block.condition_id:
        c = snapshot.condition(task.block.condition_id)
        if c is None:
            return []  # the block points at a condition Kelpie cannot see: leave it alone
        found[c.id] = c
    return sorted(found.values(), key=lambda c: c.id)


def _met_at(c: Condition, fallback: datetime) -> datetime:
    """When the condition was met; if unknown, when it was last seen met."""
    return c.satisfied_at or c.checked_at or fallback


def _human(d: timedelta) -> str:
    minutes = max(0, int(d.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


@register(NAME)
def build(config: dict[str, Any]) -> StaleBlocks:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "unblock_grace" in config:
        value = config.pop("unblock_grace")
        try:
            kwargs["unblock_grace"] = parse_duration(value)
        except (AttributeError, TypeError) as exc:
            raise ValueError(f"unblock_grace: invalid duration {value!r}") from exc
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return StaleBlocks(**kwargs)
