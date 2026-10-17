"""Long-lived alert states: alert on transitions, not on every tick.

A rule that reports an ongoing condition (an orphaned task, a block nothing will clear)
marks its alert with ``Action.state`` (a stable id for the underlying condition) and
``Action.since`` (when the condition began). The engine asks the :class:`StateTracker`
what each sighting means:

- **opened**: first sighting, or first sighting since Kelpie started acting (a state seen
  only in observe/dry-run is announced again when the rule goes live) → deliver;
- **escalated**: the time since Kelpie first reported it passed the next escalation
  threshold (default 4h → 12h → 24h), or the severity rose → deliver again;
- **changed**: the same condition is now described differently (another alert key) →
  deliver;
- **ongoing**: nothing new → suppress.

A transition is committed only after the alert was handled (delivered, or logged in
observe/dry-run); if the ledger suppressed it or delivery failed, the next tick tries
again. States a rule (that ran fine) has not reported for ``resolve_after`` are resolved:
the engine sends a short "resolved" (what is true now, from the rule's ``resolved`` hook,
else that the rule no longer reports it) and closes them, again only once that notice was
handled. Open states persist so the daily report can list them and a restart does not
re-announce everything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from kelpie.core.actions import Action, Severity
from kelpie.core.config import DEFAULT_STATE_ESCALATIONS, Mode

DEFAULT_ESCALATIONS = DEFAULT_STATE_ESCALATIONS
DEFAULT_RESOLVE_AFTER = timedelta(minutes=10)
_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class Transition(StrEnum):
    OPENED = "opened"
    ESCALATED = "escalated"
    CHANGED = "changed"
    ONGOING = "ongoing"


@dataclass(frozen=True, slots=True)
class OpenState:
    id: str
    rule: str
    since: datetime  # when the underlying condition began
    opened_at: datetime  # when Kelpie first reported it (escalations count from here)
    last_seen: datetime
    level: int  # escalation thresholds already announced
    severity: Severity
    summary: str  # reason of the alert that last announced it
    task_id: str | None = None
    key: str | None = None  # alert key of the last announcement
    mode: Mode = Mode.OBSERVE  # effective mode of the last announcement


class StateStore(Protocol):
    def open_states(self) -> Sequence[OpenState]: ...

    def get_state(self, state_id: str) -> OpenState | None: ...

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

    def get_state(self, state_id: str) -> OpenState | None:
        return self.states.get(state_id)

    def save(self, state: OpenState) -> None:
        self.states[state.id] = state

    def close_state(self, state_id: str, at: datetime) -> None:
        if (state := self.states.pop(state_id, None)) is not None:
            self.closed.append((state, at))


@dataclass(slots=True)
class StateTracker:
    store: StateStore
    escalations: tuple[timedelta, ...] = DEFAULT_ESCALATIONS
    resolve_after: timedelta = DEFAULT_RESOLVE_AFTER

    def peek(
        self, action: Action, now: datetime, mode: Mode
    ) -> tuple[Transition, OpenState, OpenState | None]:
        """What this sighting means, the state to commit if its alert is handled, and the
        state as stored now (None if new). Nothing is saved."""
        assert action.state is not None
        current = self.store.get_state(action.state)
        live = mode is not Mode.OBSERVE
        if current is None or (live and current.mode is Mode.OBSERVE):
            state = OpenState(
                id=action.state,
                rule=action.rule,
                since=action.since or (current.since if current else now),
                opened_at=now,
                last_seen=now,
                level=0,
                severity=action.severity,
                summary=action.reason,
                task_id=action.task_id,
                key=action.key,
                mode=mode,
            )
            return Transition.OPENED, state, current
        level = sum(1 for t in self.escalations if now - current.opened_at >= t)
        worse = _RANK[action.severity] > _RANK[current.severity]
        if level > current.level or worse:
            transition = Transition.ESCALATED
        elif action.key != current.key:
            transition = Transition.CHANGED
        else:
            return Transition.ONGOING, replace(current, last_seen=now), current
        state = replace(
            current,
            since=action.since or current.since,
            last_seen=now,
            level=max(level, current.level),
            severity=action.severity if worse else current.severity,
            summary=action.reason,
            key=action.key,
            mode=mode,
        )
        return transition, state, current

    def commit(self, state: OpenState) -> None:
        self.store.save(state)

    def touch(self, current: OpenState | None, now: datetime) -> None:
        """Record a sighting without committing a transition (retry it next tick)."""
        if current is not None:
            self.store.save(replace(current, last_seen=now))

    def gone(self, rules: Iterable[str], seen: set[str], now: datetime) -> list[OpenState]:
        """Open states of ``rules`` not reported for ``resolve_after``."""
        names = set(rules)
        return [
            s
            for s in self.store.open_states()
            if s.rule in names and s.id not in seen and now - s.last_seen >= self.resolve_after
        ]

    def resolve(self, state: OpenState, now: datetime) -> None:
        self.store.close_state(state.id, now)
