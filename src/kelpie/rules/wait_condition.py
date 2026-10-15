"""Rule 2 — wait for a condition instead of waiting inside a session (incident 10).

An agent that needs something external (CI, a merge, a quota reset, a human) registers a
condition and exits; the task becomes ``waiting``. This rule:

- wakes a waiting task when every condition of its *current* wait holds, on fresh evidence
  (checked since the wait began and within ``max_condition_age``);
- alerts when a task waits past ``max_wait``, or (after a short grace) waits on nothing;
- spots agents that wait *inside* a live session by polling a known check, and tells them
  to register a condition and exit.

Polling detection only looks at calls matching ``poll_patterns`` (condition kind → command
substrings), which is configuration, never code: Kelpie knows no particular CI. Without
patterns it stays silent. Generic loops are rule 10's (``loop_breaker``).

Keeping the dispatcher from declaring a waiting task dead is the adapter's job (see
:class:`kelpie.core.ports.PlatformAdapter`); this rule never requeues waiting tasks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Session, Snapshot, Span, SpanKind, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._calls import CallKey, call_key, command, error_streak, tool_calls
from kelpie.rules._tasks import (
    DEFAULT_MAX_CONDITION_AGE,
    DEFAULT_STALE_AFTER,
    fresh,
    is_live,
    linked_conditions,
)

NAME = "wait_condition"


@dataclass(frozen=True, slots=True)
class WaitCondition:
    max_wait: timedelta = timedelta(hours=24)
    no_condition_grace: timedelta = timedelta(minutes=10)
    # condition kind -> substrings of a polled command
    poll_patterns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Defaults are the AgentWatchdog thresholds: 8 repeats in 10 minutes, 5 errors in a row.
    poll_min_repeats: int = 8
    poll_window: timedelta = timedelta(minutes=10)
    poll_error_streak: int = 5
    max_condition_age: timedelta = DEFAULT_MAX_CONDITION_AGE
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is Status.WAITING:
                actions.extend(self._check_waiting(task, snapshot))
        if self.poll_patterns:
            waiting = {t.id for t in snapshot.tasks if t.status is Status.WAITING}
            for session in snapshot.sessions:
                if session.task_id in waiting:
                    continue  # already did the right thing; the wake will follow
                if is_live(session, snapshot.now, self.session_stale_after):
                    actions.extend(self._check_polling(session, snapshot))
        return actions

    # --- waiting tasks ------------------------------------------------------------------

    def _check_waiting(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        waited = snapshot.now - task.status_since
        since = task.status_since.isoformat()
        conditions, missing = linked_conditions(task, snapshot)
        if not conditions:
            if waited < self.no_condition_grace:
                return  # the connector may not have synced the new condition yet
            what = (
                f"points at condition {missing}, which Kelpie cannot see"
                if missing
                else "is waiting without any registered condition"
            )
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} {what}, so nothing will ever wake it.",
                task_id=task.id,
                severity=Severity.WARNING,
                key=f"no-condition:{task.id}:{since}",
            )
            return
        if all(
            c.satisfied and fresh(c, task.status_since, snapshot.now, self.max_condition_age)
            for c in conditions
        ):
            done = ", ".join(f"{c.id} ({c.kind})" for c in conditions)
            yield Action(
                ActionKind.WAKE,
                NAME,
                f"Every condition the task was waiting for now holds: {done}. Waking it up.",
                task_id=task.id,
                key=f"wake:{task.id}:{since}",
                params={"conditions": [c.id for c in conditions]},
            )
            return
        if waited >= self.max_wait:
            pending = [c for c in conditions if not c.satisfied]
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} has waited {_hours(waited)} (limit {_hours(self.max_wait)}) "
                f"for {', '.join(f'{c.id} ({c.kind})' for c in pending)}.",
                task_id=task.id,
                severity=Severity.WARNING,
                key=f"overdue:{task.id}:{since}",
                params={"pending_conditions": [c.id for c in pending]},
            )

    # --- polling inside a session -------------------------------------------------------

    def _check_polling(self, session: Session, snapshot: Snapshot) -> Iterable[Action]:
        spans = snapshot.spans_for_session(session.id)
        calls = tool_calls(spans, after=snapshot.now - self.poll_window)
        found = (
            self._repeated_check(calls)
            or self._failing_check(calls)
            or self._blocking_check(spans, snapshot)
        )
        if found is None:
            return
        what, kind = found
        yield Action(
            ActionKind.COMMENT,
            NAME,
            f"This session {what}, which looks like waiting inside the session. Please "
            f"register a `{kind}` condition and exit: the task will show as waiting rather "
            "than dead, and Kelpie will wake it when the condition holds.",
            task_id=session.task_id,
            key=f"poll:{session.id}",
            params={"session_id": session.id, "condition_kind": kind},
        )

    def _repeated_check(self, calls: Sequence[Span]) -> tuple[str, str] | None:
        """The same known check called often within the window, other calls in between."""
        groups: dict[CallKey, list[Span]] = defaultdict(list)
        for span in calls:
            key = call_key(span)
            if key is not None and self._kind_for(span):
                groups[key].append(span)
        if not groups:
            return None
        best = max(groups.values(), key=len)
        if len(best) < self.poll_min_repeats:
            return None
        kind = self._kind_for(best[-1])
        assert kind is not None
        return f"ran the same check {len(best)} times within {_minutes(self.poll_window)}", kind

    def _failing_check(self, calls: Sequence[Span]) -> tuple[str, str] | None:
        """The same known check failing over and over (e.g. "still pending" exit codes)."""
        errors = error_streak(calls)
        if len(errors) < self.poll_error_streak:
            return None
        keys = {call_key(s) for s in errors}
        kind = self._kind_for(errors[-1])
        if len(keys) != 1 or None in keys or kind is None:
            return None
        return f"failed the same check {len(errors)} times in a row", kind

    def _blocking_check(self, spans: Sequence[Span], snapshot: Snapshot) -> tuple[str, str] | None:
        """One known check that has been blocking (still running) for a whole window."""
        for span in spans:
            if span.kind is not SpanKind.TOOL or span.end is not None:
                continue
            running = snapshot.now - span.start
            if running >= self.poll_window and (kind := self._kind_for(span)):
                return f"has been blocked on one check for {_minutes(running)}", kind
        return None

    def _kind_for(self, span: Span) -> str | None:
        text = command(span) or span.args_hash or ""
        return next(
            (k for k, pats in self.poll_patterns.items() if any(p in text for p in pats)),
            None,
        )


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:.1f}h"


def _minutes(d: timedelta) -> str:
    return f"{d.total_seconds() / 60:.0f} minutes"


def _duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return result


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@register(NAME)
def build(config: dict[str, Any]) -> WaitCondition:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in (
        "max_wait",
        "no_condition_grace",
        "poll_window",
        "max_condition_age",
        "session_stale_after",
    ):
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
