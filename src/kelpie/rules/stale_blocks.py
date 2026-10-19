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

**Blocks rule 13 lists are rule 13's** (issue #169). While rule 13 reaches people, this
rule gets its settings (``needs_you``, set by ``kelpie.app``: rule 13's config section, else
``false``) and leaves it every task it lists (:func:`~kelpie.rules.needs_you.inbox_task_ids`,
its time thresholds ignored, so this rule's own still apply): approvals (an agent's block
that asks a person, ``waiting_human: needs_input: CEO: …``, by
:func:`~kelpie.rules._tasks.asks_a_person`), agent tasks parked on a human task the person
can move now, and the person's own blocked task. For those it raises no no-condition, overdue
or dead-end alert ("nothing Kelpie can check" means nothing to a person): one message, not
two. A person's own listed task whose block can never clear (a dead end, a condition Kelpie
cannot see) carries that in the digest's "Needed" line instead. Rule 13 lists no agent task
with a dead end (the human task it waited on was cancelled), a condition Kelpie cannot see,
or another unmet condition (a PR, CI), so those alert here as before, the dead end with
what is stuck behind it. Everything else stays this
rule's too, so every block reaches a person through exactly one rule:

- a block rule 13 does not list: parked on a person's task the person cannot move yet
  (in the backlog or triage, or behind agent work), or one that names a human task it
  depends on that is not the person's to move yet;
- a block that names a condition (``needs_input: automerge PR #12``, matching
  ``condition_reasons``) or has an unmet linked condition other than a human task: it waits
  on that condition.

A condition the connector stopped checking alerts whoever lists the task (Kelpie's
plumbing, not the person's question). The block is still cleared once its conditions hold.
Without rule 13 reaching people (it is not loaded, or it runs in ``observe`` while this rule
does not), this rule alerts on every block as before, so nothing goes silent.

The dead-end and no-condition alerts list the tasks stuck behind the blocked one
(``params.stuck_ids``, through dependencies and task waits, as rule 3 reads them), so rule 3
does not raise a second alert for the same dead end.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import KelpieMark, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import needs_you, register
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
    # Rule 13 as it reaches people (kelpie.app sets it from needs_you's config while it
    # does), or None: the blocks it lists that ask a person are left to it.
    inbox: needs_you.NeedsYou | None = None
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        behind = behind_lookup(snapshot)
        listed = needs_you.inbox_task_ids(snapshot, self.inbox) if self.inbox is not None else None
        for task in snapshot.tasks:
            if task.status is Status.BLOCKED and task.block is not None:
                actions.extend(self._check(task, snapshot, behind, listed))
        return actions

    def _check(
        self,
        task: Task,
        snapshot: Snapshot,
        behind: Callable[[str], list[str]],
        listed: frozenset[str] | None = None,
    ) -> Iterable[Action]:
        block = task.block
        assert block is not None
        if block.kelpie is KelpieMark.STOP:
            return  # Kelpie's own stop waits for a human: rule 13's
        age = snapshot.now - block.since
        conditions, missing = linked_conditions(task, snapshot)
        # In the person's "needs you" digest: no alert of ours tells them the same again
        # (stale evidence still does: it is about the connector, not the question).
        theirs = listed is not None and task.id in listed
        if not conditions:
            if theirs:
                return  # a question for a person: nothing to check, rule 13 lists it
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
                    about_wait=not missing,  # a reference Kelpie cannot see is broken
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
            if theirs:
                return  # a person's own task, in their digest with its block reason
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
            if theirs:
                return  # it waits on a human task: rule 13 lists it there
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
        self,
        task: Task,
        kind: str,
        reason: str,
        params: dict[str, Any] | None = None,
        *,
        about_wait: bool = False,
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
            # Long or unexplained, not broken: a CI outage that explains the block folds it.
            about_wait=about_wait or kind == "overdue",
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


def _inbox(value: Any) -> needs_you.NeedsYou | None:
    """``needs_you``: false, or rule 13's config section (true: its defaults)."""
    if value is False:
        return None
    if value is True:
        return needs_you.build({})
    if isinstance(value, Mapping):
        return needs_you.build(dict(value))
    raise ValueError("needs_you must be false, true or rule 13's settings")


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
    if "needs_you" in config:
        kwargs["inbox"] = _inbox(config.pop("needs_you"))
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return StaleBlocks(**kwargs)
