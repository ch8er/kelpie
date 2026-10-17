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

Conditions are linked by the shared rule semantics (``kelpie.rules._tasks``). So that no
block sits unnoticed, Kelpie alerts on blocks it cannot clear: no visible condition after
``unlinked_alert_after`` (4h), a condition still unmet after ``max_block`` (8h), and
conditions the connector stopped checking (``stale_evidence_after``, 1h). Each can be set to
``false``. ``waiting`` tasks are rule 2's. A condition a connector flagged
``params.terminal`` (a PR closed without merging, a human's task that failed or was
cancelled), seen since the block was set, can never be met: Kelpie alerts at once instead of
after ``max_block``, and keeps the alert while the connector is slow to check again.

A block whose reason only says it waits ("waiting for CI/merge of #123") gets its condition
before the rule runs: ``[[block_conditions]]`` patterns attach one during snapshot assembly
(``kelpie.connectors.github.blocks``), linked through ``Block.condition_id``, so such a
block is handled like any other block with a condition. It is then a declared wait for
rule 1 too, which does not requeue blocks tied to a condition.

A block Kelpie set itself (``Block.kelpie``, from a signed marker the adapter verified: a
stop of a runaway run) is not a stale block: it waits for a human to look at the task, not
for a condition, so rule 13 lists it in the "needs you" inbox and this rule leaves it alone.

The dead-end and no-condition alerts list the tasks stuck behind the blocked one
(``params.stuck_ids``, through dependencies and task waits, as rule 3 reads them), so rule 3
does not raise a second alert for the same dead end.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import KelpieMark, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._chains import behind_lookup, behind_text
from kelpie.rules._tasks import (
    DEFAULT_MAX_CONDITION_AGE,
    dead_end_text,
    dead_ends,
    fresh,
    linked_conditions,
)

NAME = "stale_blocks"


@dataclass(frozen=True, slots=True)
class StaleBlocks:
    unblock_grace: timedelta = timedelta(minutes=5)
    max_condition_age: timedelta = DEFAULT_MAX_CONDITION_AGE
    # Blocks nothing will clear on its own get an alert, so none sits unnoticed:
    unlinked_alert_after: timedelta | None = timedelta(hours=4)  # no condition Kelpie sees
    max_block: timedelta | None = timedelta(hours=8)  # condition still not met
    stale_evidence_after: timedelta | None = timedelta(hours=1)  # connector stopped checking
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        behind = behind_lookup(snapshot)
        for task in snapshot.tasks:
            if task.status is Status.BLOCKED and task.block is not None:
                actions.extend(self._check(task, snapshot, behind))
        return actions

    def _check(
        self, task: Task, snapshot: Snapshot, behind: Callable[[str], list[str]]
    ) -> Iterable[Action]:
        block = task.block
        assert block is not None
        if block.kelpie is KelpieMark.STOP:
            return  # Kelpie's own stop waits for a human: rule 13's
        age = snapshot.now - block.since
        conditions, missing = linked_conditions(task, snapshot)
        if not conditions:
            if self.unlinked_alert_after is not None and age >= self.unlinked_alert_after:
                what = (
                    f"names condition {missing}, which Kelpie cannot see"
                    if missing
                    else "has no condition Kelpie can check"
                )
                stuck = behind(task.id)
                yield self._alert(
                    task,
                    "unlinked",
                    f"Task {task.id} has been blocked for {_human(age)} on "
                    f"'{block.reason}' and {what}, so nothing will clear it automatically."
                    + behind_text(stuck),
                    {"stuck_ids": stuck},
                )
            return
        stale = [
            c
            for c in conditions
            if c.checked_at is None or snapshot.now - c.checked_at > self.max_condition_age
        ]
        if (
            stale
            and self.stale_evidence_after is not None
            and age >= self.stale_evidence_after
            and all(
                c.checked_at is None or snapshot.now - c.checked_at >= self.stale_evidence_after
                for c in stale
            )
        ):
            names = ", ".join(c.id for c in stale)
            yield self._alert(
                task,
                "stale-evidence",
                f"Task {task.id} has been blocked for {_human(age)}, but "
                f"condition(s) {names} have not been checked for over "
                f"{_human(self.stale_evidence_after)}: is the connector running?",
            )
            return
        ended = dead_ends(conditions, block.since)
        if ended:
            stuck = behind(task.id)
            yield self._alert(
                task,
                "dead-end",
                f"Task {task.id} is blocked on '{block.reason}', but {dead_end_text(ended)}: "
                "the block will never clear on its own." + behind_text(stuck),
                {"stuck_ids": stuck},
            )
            return
        if not all(c.satisfied for c in conditions):
            if self.max_block is not None and age >= self.max_block:
                pending = ", ".join(f"{c.id} ({c.kind})" for c in conditions if not c.satisfied)
                yield self._alert(
                    task,
                    "overdue",
                    f"Task {task.id} has been blocked for {_human(age)} "
                    f"(limit {_human(self.max_block)}) waiting for {pending}.",
                )
            return
        if not all(fresh(c, block.since, snapshot.now, self.max_condition_age) for c in conditions):
            return  # no evidence gathered since the block was set, or not recent
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
            f"outlived its cause by {by}.",
            task_id=task.id,
            key=f"unblock:{task.id}:{block.since.isoformat()}",
            params={
                "conditions": [c.id for c in conditions],
                "outlived_minutes": int(outlived.total_seconds() // 60),
            },
        )

    def _alert(
        self, task: Task, kind: str, reason: str, params: dict[str, Any] | None = None
    ) -> Action:
        block = task.block
        assert block is not None
        return Action(
            ActionKind.ALERT,
            NAME,
            reason,
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"{kind}:{task.id}:{block.since.isoformat()}",
            # One state per block: overdue / stale evidence / unlinked are its descriptions.
            state=f"{NAME}:{task.id}:{block.since.isoformat()}",
            since=block.since,
            params=params or {},
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
    if "max_condition_age" in config:
        kwargs["max_condition_age"] = _duration(
            config.pop("max_condition_age"), "max_condition_age"
        )
    for key in ("unlinked_alert_after", "max_block", "stale_evidence_after"):
        if key in config:
            value = config.pop(key)
            # false switches the alert off
            kwargs[key] = None if value is False else _duration(value, key)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return StaleBlocks(**kwargs)
