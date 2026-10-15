"""Rule 1 — exit contract (incident 2).

A session must end with a final status, and a block must carry an allowed reason. When an
agent exits without either, nobody is working on the task and nobody knows why it stopped,
so the task goes back to the queue with an explanation.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register

NAME = "exit_contract"

# Statuses that mean "someone is working on this"; a session ending here must say how.
_WORKING = {Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED}
DEFAULT_ALLOWED_BLOCK_REASONS = (
    "guard",
    "waiting_ci",
    "waiting_pr_merge",
    "waiting_human",
    "quota",
)


@dataclass(frozen=True, slots=True)
class ExitContract:
    """``allowed_block_reasons`` match the whole reason or its prefix before ``:`` or a space.

    A block tied to a registered condition (``block.condition_id``) is always allowed.
    ``session_exit_grace`` gives the platform time to record a status after an exit.
    """

    allowed_block_reasons: tuple[str, ...] = DEFAULT_ALLOWED_BLOCK_REASONS
    session_exit_grace: timedelta = timedelta(minutes=10)
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is Status.BLOCKED:
                actions.extend(self._check_block(task, snapshot))
            elif task.status in _WORKING:
                actions.extend(self._check_exit(task, snapshot))
        return actions

    def _check_exit(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        sessions = snapshot.sessions_for(task.id)
        if not sessions or any(s.ended_at is None for s in sessions):
            return
        last = max(sessions, key=lambda s: s.ended_at or s.started_at)
        ended = last.ended_at
        if ended is None or last.final_status is not None:
            return
        if task.status_since > ended or snapshot.now - ended < self.session_exit_grace:
            return
        yield Action(
            ActionKind.REQUEUE,
            NAME,
            f"Session {last.id} of {last.owner_id} ended at {ended:%Y-%m-%d %H:%M} UTC without "
            f"a final status ({last.exit_reason or 'no exit reason'}); the task was left "
            f"'{task.status}' with nobody working on it. Returning it to the queue.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"exit:{last.id}",
            params=_session_params(last),
        )

    def _check_block(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        block = task.block
        if block is not None and (block.condition_id or self._allowed(block.reason)):
            return
        since = block.since if block is not None else task.status_since
        if snapshot.now - since < self.session_exit_grace:
            return
        got = f"'{block.reason}'" if block is not None and block.reason else "no reason"
        yield Action(
            ActionKind.REQUEUE,
            NAME,
            f"Task blocked with {got}, which is not an allowed block reason "
            f"({', '.join(self.allowed_block_reasons)}) and has no registered condition. "
            "Nothing would ever unblock it; returning it to the queue.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"block:{task.id}:{since.isoformat()}",
            params={"block_reason": block.reason if block is not None else None},
        )

    def _allowed(self, reason: str) -> bool:
        head = reason.strip().lower()
        return any(
            head == r or head.startswith(f"{r}:") or head.startswith(f"{r} ")
            for r in (a.lower() for a in self.allowed_block_reasons)
        )


def _session_params(session: Session) -> dict[str, Any]:
    params: dict[str, Any] = {"session_id": session.id, "owner_id": session.owner_id}
    if session.dirty_worktree:
        params["dirty_worktree"] = True  # the checkpoints rule handles the WIP commit
    return params


@register(NAME)
def build(config: dict[str, Any]) -> ExitContract:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "allowed_block_reasons" in config:
        reasons = config.pop("allowed_block_reasons")
        if not isinstance(reasons, list | tuple) or not all(isinstance(r, str) for r in reasons):
            raise ValueError("allowed_block_reasons must be a list of strings")
        kwargs["allowed_block_reasons"] = tuple(reasons)
    if "session_exit_grace" in config:
        kwargs["session_exit_grace"] = parse_duration(config.pop("session_exit_grace"))
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return ExitContract(**kwargs)
