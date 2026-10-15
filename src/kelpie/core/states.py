"""Long-lived alert states: alert on transitions, not on every tick.

A rule that reports an ongoing condition (an orphaned task, a block nothing will clear)
marks its alert with ``Action.state``. The engine passes each such alert through the
:class:`StateTracker`, which decides:

- **opened**: first time Kelpie sees the state → deliver;
- **escalated**: the state's age (from ``Action.since``) passed the next escalation
  threshold (default 4h → 12h → 24h), or its severity rose → deliver again;
- **ongoing**: nothing new → suppress.

States a rule stops reporting (while the rule itself evaluated fine) are **resolved**: the
engine sends a short "resolved" alert and closes them. Open states are persisted so the
daily report can list them and a restart does not re-announce everything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from kelpie.core.actions import Action, Severity

DEFAULT_ESCALATIONS = (timedelta(hours=4), timedelta(hours=12), timedelta(hours=24))
_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class Transition(StrEnum):
    OPENED = "opened"
    ESCALATED = "escalated"
    ONGOING = "ongoing"


@dataclass(frozen=True, slots=True)
class OpenState:
    id: str
    rule: str
    since: datetime  # when the underlying condition began
    opened_at: datetime  # when Kelpie first reported it
    last_seen: datetime
    level: int  # escalation thresholds already announced
    severity: Severity
    summary: str  # reason of the alert that opened (or last escalated) it
    task_id: str | None = None


class StateStore(Protocol):
    def open_states(self) -> Sequence[OpenState]: ...

    def save(self, state: OpenState) -> None:
        """Insert or update an open state."""
        ...

    def close_state(self, state_id: str, at: datetime) -> None: ...


@dataclass(slots=True)
class MemoryStateStore:
    states: dict[str, OpenState] = field(default_factory=dict)
    closed: list[tuple[OpenState, datetime]] = field(default_factory=list)

    def open_states(self) -> Sequence[OpenState]:
        return list(self.states.values())

    def save(self, state: OpenState) -> None:
        self.states[state.id] = state

    def close_state(self, state_id: str, at: datetime) -> None:
        if (state := self.states.pop(state_id, None)) is not None:
            self.closed.append((state, at))


@dataclass(slots=True)
class StateTracker:
    store: StateStore
    escalations: tuple[timedelta, ...] = DEFAULT_ESCALATIONS

    def observe(self, action: Action, now: datetime) -> tuple[Transition, OpenState]:
        """Record a sighting of ``action.state`` and say what kind of transition it is."""
        assert action.state is not None
        since = action.since or now
        level = sum(1 for t in self.escalations if now - since >= t)
        current = {s.id: s for s in self.store.open_states()}.get(action.state)
        if current is None:
            # Thresholds already behind the state when Kelpie first sees it are not news.
            state = OpenState(
                id=action.state,
                rule=action.rule,
                since=since,
                opened_at=now,
                last_seen=now,
                level=level,
                severity=action.severity,
                summary=action.reason,
                task_id=action.task_id,
            )
            self.store.save(state)
            return Transition.OPENED, state
        worse = _RANK[action.severity] > _RANK[current.severity]
        if level > current.level or worse:
            state = replace(
                current,
                last_seen=now,
                level=max(level, current.level),
                severity=action.severity if worse else current.severity,
                summary=action.reason,
            )
            self.store.save(state)
            return Transition.ESCALATED, state
        state = replace(current, last_seen=now)
        self.store.save(state)
        return Transition.ONGOING, state

    def gone(self, rules: Iterable[str], seen: set[str]) -> list[OpenState]:
        """Open states of ``rules`` that were not reported this time."""
        names = set(rules)
        return [s for s in self.store.open_states() if s.rule in names and s.id not in seen]

    def resolve(self, state: OpenState, now: datetime) -> None:
        self.store.close_state(state.id, now)
