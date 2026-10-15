"""Engine configuration: global dry-run and per-rule modes (observe | suggest | act)."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from kelpie.core.serde import parse_duration


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
    """``dry_run`` is the global override: when set, nothing leaves Kelpie in any mode."""

    interval: timedelta = timedelta(seconds=30)
    dry_run: bool = False
    cooldown: timedelta = timedelta(minutes=30)  # ledger: same action not repeated
    retry_after: timedelta = timedelta(minutes=5)  # ledger: retry delay after a failure
    # Ledger cap on actions with an effect, per task (else per session, else per rule).
    max_actions_per_task_per_hour: int = 6
    default_mode: Mode = Mode.OBSERVE
    modes: Mapping[str, Mode] = field(default_factory=dict)
    rules: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def mode_for(self, rule: str) -> Mode:
        return self.modes.get(rule, self.default_mode)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> EngineConfig:
        """Parse ``{interval, dry_run, default_mode, rules: {name: {mode, ...params}}}``."""
        known = {
            "interval",
            "dry_run",
            "default_mode",
            "cooldown",
            "retry_after",
            "max_actions_per_task_per_hour",
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
        dry_run = data.get("dry_run", False)
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
        # Rule names are checked against kelpie.rules.PLANNED by the CLI; core cannot import it.
        return cls(
            interval=interval,
            dry_run=dry_run,
            cooldown=cooldown,
            retry_after=retry_after,
            max_actions_per_task_per_hour=max_actions,
            default_mode=Mode(data.get("default_mode", Mode.OBSERVE)),
            modes=modes,
            rules=rules,
        )


def load_config(path: Path) -> EngineConfig:
    """Load the ``[engine]`` table of a TOML file; a missing table means all defaults."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    return EngineConfig.from_mapping(data.get("engine", {}))
