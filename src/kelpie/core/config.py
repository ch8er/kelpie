"""Engine configuration: global dry-run and per-rule modes (observe | suggest | act)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Any

from kelpie.core.serde import parse_duration

# Module constant, not a class attribute: with slots=True a field's class attribute is a
# member descriptor, so `cls.state_escalations` is not the default value.
DEFAULT_STATE_ESCALATIONS: tuple[timedelta, ...] = (
    timedelta(hours=4),
    timedelta(hours=12),
    timedelta(hours=24),
)


class Mode(StrEnum):
    """How far a rule may go.

    - ``observe``: actions are only recorded and logged.
    - ``suggest``: alerts are delivered; side effects are sent to a human as suggestions.
    - ``act``: actions are executed.
    """

    OBSERVE = "observe"
    SUGGEST = "suggest"
    ACT = "act"


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """``dry_run`` is the global override: when set, nothing leaves Kelpie in any mode.

    Both safety switches default to the safe side: dry-run on, every rule in observe.
    """

    interval: timedelta = timedelta(seconds=30)
    dry_run: bool = True
    cooldown: timedelta = timedelta(minutes=30)  # ledger: same action not repeated
    retry_after: timedelta = timedelta(minutes=5)  # ledger: retry delay after a failure
    # Ledger cap on actions with an effect, per task (else per session, else per rule).
    max_actions_per_task_per_hour: int = 6
    # Ledger: a ``once`` action is done once per fingerprint within this window, and is
    # given up after this many failed attempts.
    once_window: timedelta = timedelta(days=7)
    once_attempts: int = 3
    # Long-lived state alerts re-fire only when their age passes one of these.
    state_escalations: tuple[timedelta, ...] = DEFAULT_STATE_ESCALATIONS
    # A state no rule reported for this long is resolved (absorbs one-tick gaps).
    state_resolve_after: timedelta = timedelta(minutes=10)
    default_mode: Mode = Mode.OBSERVE
    modes: Mapping[str, Mode] = field(default_factory=dict)
    rules: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def mode_for(self, rule: str) -> Mode:
        return self.modes.get(rule, self.default_mode)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EngineConfig:
        """Build from the ``[engine]`` table (``kelpie.app.load_settings`` reads the file)."""
        if not isinstance(data, Mapping):
            raise ValueError("[engine] must be a table")
        return cls._from_mapping(data)

    @classmethod
    def _from_mapping(cls, data: Mapping[str, Any]) -> EngineConfig:
        """Parse ``{interval, dry_run, default_mode, rules: {name: {mode, ...params}}}``."""
        known = {
            "interval",
            "dry_run",
            "default_mode",
            "cooldown",
            "retry_after",
            "max_actions_per_task_per_hour",
            "once_window",
            "once_attempts",
            "state_escalations",
            "state_resolve_after",
            "rules",
        }
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown engine config keys: {sorted(unknown)}")
        raw_rules = data.get("rules", {})
        if not isinstance(raw_rules, Mapping) or not all(
            isinstance(v, Mapping) for v in raw_rules.values()
        ):
            raise ValueError("engine.rules must map rule names to tables")
        rules = {name: dict(section) for name, section in raw_rules.items()}
        modes = {name: Mode(s.pop("mode")) for name, s in rules.items() if "mode" in s}
        dry_run = data.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ValueError(f"engine.dry_run must be true or false, got {dry_run!r}")
        interval = parse_duration(data.get("interval", 30))
        if interval <= timedelta(0):
            raise ValueError("engine.interval must be positive")
        cooldown = parse_duration(data.get("cooldown", "30m"))
        retry_after = parse_duration(data.get("retry_after", "5m"))
        max_actions = data.get("max_actions_per_task_per_hour", 6)
        if cooldown < timedelta(0) or retry_after < timedelta(0):
            raise ValueError("engine.cooldown and engine.retry_after must not be negative")
        if not isinstance(max_actions, int) or isinstance(max_actions, bool) or max_actions < 1:
            raise ValueError("engine.max_actions_per_task_per_hour must be a positive integer")
        once_window = parse_duration(data.get("once_window", "7d"))
        if once_window <= timedelta(0):
            raise ValueError("engine.once_window must be positive")
        once_attempts = data.get("once_attempts", 3)
        if isinstance(once_attempts, bool) or not isinstance(once_attempts, int):
            raise ValueError("engine.once_attempts must be a positive integer")
        if once_attempts < 1:
            raise ValueError("engine.once_attempts must be a positive integer")
        escalations = DEFAULT_STATE_ESCALATIONS
        if "state_escalations" in data:
            raw = data["state_escalations"]
            if not isinstance(raw, list | tuple):
                raise ValueError("engine.state_escalations must be a list of durations")
            escalations = tuple(parse_duration(v) for v in raw)
            if any(t <= timedelta(0) for t in escalations) or list(escalations) != sorted(
                set(escalations)
            ):
                raise ValueError("engine.state_escalations must be positive and increasing")
        resolve_after = parse_duration(data.get("state_resolve_after", "10m"))
        if resolve_after < timedelta(0):
            raise ValueError("engine.state_resolve_after must not be negative")
        # Core cannot import kelpie.rules, so rule names are not checked against PLANNED
        # here; whoever wires the engine (CLI/server) must do it.
        return cls(
            interval=interval,
            dry_run=dry_run,
            cooldown=cooldown,
            retry_after=retry_after,
            max_actions_per_task_per_hour=max_actions,
            once_window=once_window,
            once_attempts=once_attempts,
            state_escalations=escalations,
            state_resolve_after=resolve_after,
            default_mode=Mode(data.get("default_mode", Mode.OBSERVE)),
            modes=modes,
            rules=rules,
        )
