"""``[adapters.openclaw]``: where the OpenClaw state, the Workboard and the hooks spool live."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.openclaw.adapter import (
    DEFAULT_LOOKBACK,
    DEFAULT_SPOOL,
    DEFAULT_STATE_DIR,
    OpenClawAdapter,
    workboard_path,
)
from kelpie.core.serde import parse_duration

SECTION = "[adapters.openclaw]"
KEYS = frozenset({"enabled", "state_dir", "workboard", "spool", "lookback"})


@dataclass(frozen=True, slots=True)
class OpenClawSettings:
    state_dir: Path = DEFAULT_STATE_DIR
    # The Workboard database; None: not read. Default: under state_dir.
    workboard: Path | None = None
    spool: Path | None = DEFAULT_SPOOL  # None: spool reading disabled
    lookback: timedelta = DEFAULT_LOOKBACK
    enabled: bool = True

    def build(self) -> OpenClawAdapter:
        return OpenClawAdapter(
            state_dir=self.state_dir,
            workboard_db=self.workboard,
            spool=self.spool,
            lookback=self.lookback,
        )


def _path_or_false(section: Mapping[str, Any], key: str, default: str) -> Path | None:
    value = section.get(key, default)
    if value is False:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{SECTION} {key} must be a non-empty string or false")
    return Path(value)


def load_openclaw(section: Any) -> OpenClawSettings:
    """Parse the section; raises ValueError with a message naming the bad key."""
    if not isinstance(section, Mapping):
        raise ValueError(f"{SECTION} must be a table")
    if extra := set(section) - KEYS:
        raise ValueError(f"unknown {SECTION} keys: {sorted(extra)}")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{SECTION} enabled must be true or false")
    state_dir = section.get("state_dir", str(DEFAULT_STATE_DIR))
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ValueError(f"{SECTION} state_dir must be a non-empty string")
    board = _path_or_false(section, "workboard", str(workboard_path(Path(state_dir))))
    spool = _path_or_false(section, "spool", str(DEFAULT_SPOOL))
    try:
        lookback = parse_duration(section.get("lookback", DEFAULT_LOOKBACK))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{SECTION} lookback: {exc}") from exc
    if lookback <= timedelta(0):
        raise ValueError(f"{SECTION} lookback must be positive")
    return OpenClawSettings(
        state_dir=Path(state_dir),
        workboard=board,
        spool=spool,
        lookback=lookback,
        enabled=enabled,
    )
