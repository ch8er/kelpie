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
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import DEFAULT_STALE_AFTER, is_live, last_seen, linked_conditions

NAME = "exit_contract"

_HANDOFF = {Status.REVIEW, Status.ACCEPTED}  # queues for the next stage
DEFAULT_ALLOWED_BLOCK_REASONS = (
    "guard",
    "waiting_ci",
    "waiting_pr_merge",
    "waiting_human",
    "quota",
)


@dataclass(frozen=True, slots=True)
class _Exit:
    session: Session
    at: datetime  # ended_at, or the last sign of life of a silent session
    silent: bool


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
            if task.status in {Status.IN_PROGRESS, Status.BLOCKED, *_HANDOFF}:
                end = self._last_exit(task, snapshot)
                if end is not None and snapshot.now - end.at >= self.session_exit_grace:
                    actions.extend(self._judge(task, end, snapshot))
        return actions

    def _last_exit(self, task: Task, snapshot: Snapshot) -> _Exit | None:
        """The task's most recent session if it is over, else None (someone is on it)."""
        exits: list[_Exit] = []
        for s in snapshot.sessions_for(task.id):
            if is_live(s, snapshot.now, self.session_stale_after):
                return None  # a live session: the task is being worked on
            exits.append(_Exit(s, last_seen(s), silent=s.ended_at is None))
        return max(exits, key=lambda e: (e.at, e.session.started_at), default=None)

    def _judge(self, task: Task, end: _Exit, snapshot: Snapshot) -> Iterable[Action]:
        last = end.session
        if task.status_since > end.at:
            return  # changed by someone after the session
        # A status stamped at the same moment as the exit was set by the session itself.
        changed_during = task.status_since >= last.started_at
        if task.status is Status.BLOCKED:
            block = task.block
            if not changed_during and last.final_status is not Status.BLOCKED:
                return  # the block predates the session: not this session's doing
            # Tied to a condition (via the block or the task) means something will clear it.
            conditions, missing = linked_conditions(task, snapshot)
            if conditions or missing or (block is not None and self._allowed(block.reason)):
                return
            yield self._requeue_block(task, last)
        elif last.final_status is not None:
            return  # the session declared how it ended
        elif task.status in _HANDOFF:
            if changed_during:
                return  # the agent handed the task on during the session
            yield self._requeue_exit(task, end)  # a reviewer/merger left without a verdict
        else:
            yield self._requeue_exit(task, end)

    def _requeue_exit(self, task: Task, end: _Exit) -> Action:
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
