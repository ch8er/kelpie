"""``[adapters.claude_code]``: where the task lists and the hooks spool live."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.claude_code.adapter import (
    DEFAULT_LOOKBACK,
    DEFAULT_SPOOL,
    DEFAULT_TASKS_DIR,
    ClaudeCodeAdapter,
)
from kelpie.core.serde import parse_duration

SECTION = "[adapters.claude_code]"
KEYS = frozenset({"enabled", "tasks_dir", "task_lists", "spool", "lookback"})


@dataclass(frozen=True, slots=True)
class ClaudeCodeSettings:
    tasks_dir: Path = DEFAULT_TASKS_DIR
    task_lists: tuple[str, ...] = ()
    spool: Path | None = DEFAULT_SPOOL  # None: spool reading disabled
    lookback: timedelta = DEFAULT_LOOKBACK
    enabled: bool = True

    def build(self) -> ClaudeCodeAdapter:
        return ClaudeCodeAdapter(
            tasks_dir=self.tasks_dir,
            task_lists=self.task_lists,
            spool=self.spool,
            lookback=self.lookback,
        )


def load_claude_code(section: Any) -> ClaudeCodeSettings:
    """Parse the section; raises ValueError with a message naming the bad key."""
    if not isinstance(section, Mapping):
        raise ValueError(f"{SECTION} must be a table")
    if extra := set(section) - KEYS:
        raise ValueError(f"unknown {SECTION} keys: {sorted(extra)}")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{SECTION} enabled must be true or false")
    tasks_dir = section.get("tasks_dir", str(DEFAULT_TASKS_DIR))
    if not isinstance(tasks_dir, str) or not tasks_dir.strip():
        raise ValueError(f"{SECTION} tasks_dir must be a non-empty string")
    spool = section.get("spool", str(DEFAULT_SPOOL))
    if spool is not False and (not isinstance(spool, str) or not spool.strip()):
        raise ValueError(f"{SECTION} spool must be a non-empty string or false")
    lists = section.get("task_lists", [])
    if (
        not isinstance(lists, list)
        or not all(isinstance(x, str) and x.strip() for x in lists)
        or len({x.strip() for x in lists}) != len(lists)
    ):
        raise ValueError(f"{SECTION} task_lists must be a list of distinct non-empty strings")
    try:
        lookback = parse_duration(section.get("lookback", DEFAULT_LOOKBACK))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{SECTION} lookback: {exc}") from exc
    if lookback <= timedelta(0):
        raise ValueError(f"{SECTION} lookback must be positive")
    return ClaudeCodeSettings(
        tasks_dir=Path(tasks_dir),
        task_lists=tuple(x.strip() for x in lists),
        spool=None if spool is False else Path(spool),
        lookback=lookback,
        enabled=enabled,
    )
