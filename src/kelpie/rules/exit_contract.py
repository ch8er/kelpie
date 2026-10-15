"""Rule 1 — exit contract (incident 2).

When an agent's session ends, it must leave the task in a declared state: a final status,
a hand-off to the next stage, or a block with an allowed reason. Otherwise nobody works on
the task and nobody knows why it stopped, so it goes back to the queue with an explanation.

Only what the *last session* left behind is judged:

- the task is still ``in_progress`` (or still ``review``/``accepted`` when a reviewer or
  merger took it and left without a verdict) → requeue;
- the session left the task ``blocked`` with no allowed reason and no registered
  condition → requeue.

Blocks set by people or the platform outside an agent session are rule 4's business. A
session that never reported its end counts as ended once its heartbeat is older than
``session_stale_after``: crashed sessions are exactly the ones that skip the end hook.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import (
    DEFAULT_STALE_AFTER,
    Exit,
    last_exit,
    linked_conditions,
    undeclared_exit,
)

NAME = "exit_contract"

DEFAULT_ALLOWED_BLOCK_REASONS = (
    "guard",
    "waiting_ci",
    "waiting_pr_merge",
    "waiting_human",
    "quota",
)


@dataclass(frozen=True, slots=True)
class ExitContract:
    """``allowed_block_reasons`` match the whole reason, or its first word followed by
    ``:`` or a space, ignoring case ("guard: wait for CI" is a ``guard`` block). A block
    tied to a registered condition (named by the block or linked to the task) is always
    allowed. ``session_exit_grace`` gives the platform time to record a status after an
    exit, and applies to blocks left behind too."""

    allowed_block_reasons: tuple[str, ...] = DEFAULT_ALLOWED_BLOCK_REASONS
    session_exit_grace: timedelta = timedelta(minutes=10)
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is Status.BLOCKED:
                actions.extend(self._check_block(task, snapshot))
                continue
            end = undeclared_exit(task, snapshot, self.session_stale_after)
            if end is not None and self._past_grace(end, snapshot):
                actions.append(self._requeue_exit(task, end))
            elif end is None:
                actions.extend(self._check_unapplied(task, snapshot))
        return actions

    def _past_grace(self, end: Exit, snapshot: Snapshot) -> bool:
        return snapshot.now - end.at >= self.session_exit_grace

    def _check_block(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        end = last_exit(task, snapshot, self.session_stale_after)
        if end is None or task.status_since > end.at or not self._past_grace(end, snapshot):
            return
        last = end.session
        # A status stamped at the same moment as the exit was set by the session itself.
        if task.status_since < last.started_at and last.final_status is not Status.BLOCKED:
            return  # the block predates the session: not this session's doing
        # Tied to a condition (via the block or the task) means something will clear it.
        conditions, missing = linked_conditions(task, snapshot)
        block = task.block
        if conditions or missing or (block is not None and self._allowed(block.reason)):
            return
        yield self._requeue_block(task, last)

    def _check_unapplied(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        """The session declared an end, but the platform never applied it."""
        if task.status is not Status.IN_PROGRESS:
            return
        end = last_exit(task, snapshot, self.session_stale_after)
        if end is None or task.status_since > end.at or not self._past_grace(end, snapshot):
            return
        declared = end.session.final_status
        if declared is None or declared is task.status:
            return
        yield Action(
            ActionKind.ALERT,
            NAME,
            f"Session {end.session.id} ended declaring '{declared}', but the task is still "
            f"'{task.status}' {_hours(snapshot.now - end.at)} later: the platform did not "
            "apply the declared status.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"unapplied:{end.session.id}",
            params={"session_id": end.session.id, "declared": str(declared)},
        )

    def _requeue_exit(self, task: Task, end: Exit) -> Action:
        s = end.session
        when = f"{end.at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
        how = (
            f"stopped sending heartbeats at {when}"
            if end.silent
            else f"ended at {when} ({s.exit_reason or 'no exit reason'})"
        )
        params: dict[str, Any] = {"session_id": s.id, "owner_id": s.owner_id}
        note = ""
        if s.dirty_worktree:
            params["dirty_worktree"] = True
            note = " The worktree has uncommitted changes: commit them as WIP before reassigning."
        return Action(
            ActionKind.REQUEUE,
            NAME,
            f"Session {s.id} of {s.owner_id} {how} without a final status, leaving the task "
            f"'{task.status}' with nobody on it. Returning it to the queue.{note}",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"exit:{s.id}",
            params=params,
        )

    def _requeue_block(self, task: Task, session: Session) -> Action:
        block = task.block
        got = f"'{block.reason}'" if block is not None and block.reason else "no reason"
        return Action(
            ActionKind.REQUEUE,
            NAME,
            f"Session {session.id} of {session.owner_id} left the task blocked with {got}, "
            f"which is not an allowed block reason ({', '.join(self.allowed_block_reasons)}) "
            "and has no registered condition. Nothing would ever unblock it; returning it to "
            "the queue.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"block:{task.id}:{task.status_since.isoformat()}",
            params={
                "session_id": session.id,
                "owner_id": session.owner_id,
                "block_reason": block.reason if block is not None else None,
            },
        )

    def _allowed(self, reason: str) -> bool:
        head = reason.strip().lower()
        return any(
            head == r or head.startswith(f"{r}:") or head.startswith(f"{r} ")
            for r in (a.strip().lower() for a in self.allowed_block_reasons)
        )


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:.1f}h"


def _duration(value: Any, name: str) -> timedelta:
    try:
        return parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc


@register(NAME)
def build(config: dict[str, Any]) -> ExitContract:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "allowed_block_reasons" in config:
        reasons = config.pop("allowed_block_reasons")
        if not isinstance(reasons, list | tuple) or not all(
            isinstance(r, str) and r.strip() for r in reasons
        ):
            raise ValueError("allowed_block_reasons must be a list of non-empty strings")
        kwargs["allowed_block_reasons"] = tuple(reasons)
    for key in ("session_exit_grace", "session_stale_after"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return ExitContract(**kwargs)
