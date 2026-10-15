"""Rule 2 — wait for a condition instead of waiting inside a session (incident 10).

An agent that needs something external (CI, a merge, a quota reset, a human) registers a
condition and exits; the task becomes ``waiting``. This rule:

- wakes a waiting task when all of its conditions hold;
- alerts when a task waits past ``max_wait`` or waits without any condition;
- spots agents that wait *inside* a live session by polling the same thing over and over,
  and tells them to register a condition and exit.

Which commands count as polling, and which condition kind each maps to, is configuration
(``poll_patterns``), never code: Kelpie does not know any particular CI.

Keeping the platform's dispatcher from declaring a waiting agent dead is the adapter's
job (it reports ``waiting`` tasks as alive); this rule never requeues waiting tasks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Condition, Session, Snapshot, Span, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._calls import call_key, error_streak, identical_streak, tool_calls

NAME = "wait_condition"


@dataclass(frozen=True, slots=True)
class WaitCondition:
    max_wait: timedelta = timedelta(hours=24)
    # condition kind -> substrings of a polled command (matched against call arguments)
    poll_patterns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Defaults are the AgentWatchdog thresholds: 8 repeats in 10 minutes, 5 errors in a row.
    poll_min_repeats: int = 8
    poll_window: timedelta = timedelta(minutes=10)
    poll_error_streak: int = 5
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is Status.WAITING:
                actions.extend(self._check_waiting(task, snapshot))
        for session in snapshot.sessions:
            if session.ended_at is None:
                actions.extend(self._check_polling(session, snapshot))
        return actions

    # --- waiting tasks ------------------------------------------------------------------

    def _check_waiting(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        conditions = _conditions_for(task, snapshot)
        if not conditions:
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} is waiting without any registered condition, so nothing "
                "will ever wake it.",
                task_id=task.id,
                severity=Severity.WARNING,
                key=f"no-condition:{task.id}",
            )
            return
        if all(c.satisfied for c in conditions):
            done = ", ".join(f"{c.id} ({c.kind})" for c in conditions)
            yield Action(
                ActionKind.WAKE,
                NAME,
                f"Every condition the task was waiting for now holds: {done}. Waking it up.",
                task_id=task.id,
                key=f"wake:{task.id}:{task.status_since.isoformat()}",
                params={"conditions": [c.id for c in conditions]},
            )
            return
        waited = snapshot.now - task.status_since
        if waited >= self.max_wait:
            pending = [c for c in conditions if not c.satisfied]
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} has waited {_hours(waited)} (limit {_hours(self.max_wait)}) "
                f"for {', '.join(f'{c.id} ({c.kind})' for c in pending)}.",
                task_id=task.id,
                severity=Severity.WARNING,
                key=f"overdue:{task.id}:{task.status_since.isoformat()}",
                params={"pending_conditions": [c.id for c in pending]},
            )

    # --- polling inside a session -------------------------------------------------------

    def _check_polling(self, session: Session, snapshot: Snapshot) -> Iterable[Action]:
        calls = tool_calls(
            snapshot.spans_for_session(session.id), after=snapshot.now - self.poll_window
        )
        streak = identical_streak(calls)
        errors = error_streak(calls)
        if streak is not None and streak.length >= self.poll_min_repeats:
            what = f"repeated the same call {streak.length} times"
            kind = self._kind_for(streak.calls[-1])
        elif len(errors) >= self.poll_error_streak and (kind := self._kind_for(errors[-1])):
            # Only failures of a known polling check: other error streaks are rule 10's.
            what = f"failed the same check {len(errors)} times in a row"
        else:
            return
        hint = (
            f"register a `{kind}` condition and exit" if kind else "register a condition and exit"
        )
        params: dict[str, Any] = {"session_id": session.id}
        if kind:
            params["condition_kind"] = kind
        yield Action(
            ActionKind.COMMENT,
            NAME,
            f"This session {what} within {_minutes(self.poll_window)}, which looks like waiting "
            f"inside the session. Please {hint}; Kelpie will wake the task when the condition "
            "holds, and the session will not be mistaken for a dead one.",
            task_id=session.task_id,
            key=f"poll:{session.id}",
            params=params,
        )

    def _kind_for(self, span: Span) -> str | None:
        """Condition kind for a call, matching ``poll_patterns`` against its command."""
        command = str(span.attributes.get("command") or call_key(span)[1])
        return next(
            (k for k, pats in self.poll_patterns.items() if any(p in command for p in pats)),
            None,
        )


def _conditions_for(task: Task, snapshot: Snapshot) -> list[Condition]:
    found = {c.id: c for c in snapshot.conditions if c.task_id == task.id}
    if task.block is not None and task.block.condition_id:
        c = snapshot.condition(task.block.condition_id)
        if c is not None:
            found[c.id] = c
    return sorted(found.values(), key=lambda c: c.id)


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:.1f}h"


def _minutes(d: timedelta) -> str:
    return f"{d.total_seconds() / 60:.0f} minutes"


def _duration(value: Any, name: str) -> timedelta:
    try:
        return parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@register(NAME)
def build(config: dict[str, Any]) -> WaitCondition:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("max_wait", "poll_window"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    for key in ("poll_min_repeats", "poll_error_streak"):
        if key in config:
            kwargs[key] = _positive_int(config.pop(key), key)
    if "poll_patterns" in config:
        raw = config.pop("poll_patterns")
        if not isinstance(raw, Mapping) or not all(
            isinstance(v, list | tuple) and all(isinstance(p, str) and p for p in v)
            for v in raw.values()
        ):
            raise ValueError("poll_patterns must map condition kinds to lists of strings")
        kwargs["poll_patterns"] = {str(k): tuple(v) for k, v in raw.items()}
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return WaitCondition(**kwargs)
