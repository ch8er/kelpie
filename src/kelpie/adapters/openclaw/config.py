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
from kelpie.adapters.openclaw.stop import CLI_TIMEOUT, DEFAULT_CLI, DEFAULT_STOP_DIR
from kelpie.core.serde import parse_duration

SECTION = "[adapters.openclaw]"
KEYS = frozenset(
    {
        *("enabled", "state_dir", "workboard", "spool", "lookback", "token_spans"),
        *("stop_dir", "cli", "cli_timeout"),
    }
)
MAX_CLI_TIMEOUT = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class OpenClawSettings:
    state_dir: Path = DEFAULT_STATE_DIR
    # The Workboard database; None: not read (load_openclaw defaults it under state_dir).
    workboard: Path | None = None
    spool: Path | None = DEFAULT_SPOOL  # None: spool reading disabled
    lookback: timedelta = DEFAULT_LOOKBACK
    enabled: bool = True
    token_spans: bool = True
    stop_dir: Path | None = DEFAULT_STOP_DIR  # None: stop is refused
    cli: str | None = DEFAULT_CLI  # None: no abort of the run in flight
    cli_timeout: timedelta = timedelta(seconds=CLI_TIMEOUT)

    def build(self) -> OpenClawAdapter:
        return OpenClawAdapter(
            state_dir=self.state_dir,
            workboard_db=self.workboard,
            spool=self.spool,
            lookback=self.lookback,
            token_spans=self.token_spans,
            stop_dir=self.stop_dir,
            cli=self.cli,
            cli_timeout=self.cli_timeout.total_seconds(),
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
    token_spans = section.get("token_spans", True)
    for key, value in (("enabled", enabled), ("token_spans", token_spans)):
        if not isinstance(value, bool):
            raise ValueError(f"{SECTION} {key} must be true or false")
    state_dir = section.get("state_dir", str(DEFAULT_STATE_DIR))
    if not isinstance(state_dir, str) or not state_dir.strip():
        raise ValueError(f"{SECTION} state_dir must be a non-empty string")
    board = _path_or_false(section, "workboard", str(workboard_path(Path(state_dir))))
    spool = _path_or_false(section, "spool", str(DEFAULT_SPOOL))
    stop_dir = _path_or_false(section, "stop_dir", str(DEFAULT_STOP_DIR))
    cli = section.get("cli", DEFAULT_CLI)
    if cli is not False and (not isinstance(cli, str) or not cli.strip()):
        raise ValueError(f"{SECTION} cli must be a non-empty string or false")
    try:
        cli_timeout = parse_duration(section.get("cli_timeout", f"{CLI_TIMEOUT:g}s"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{SECTION} cli_timeout: {exc}") from exc
    if not timedelta(0) < cli_timeout <= MAX_CLI_TIMEOUT:
        raise ValueError(f"{SECTION} cli_timeout must be above 0s and at most 60s")
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
        token_spans=token_spans,
        stop_dir=stop_dir,
        cli=None if cli is False else cli.strip(),
        cli_timeout=cli_timeout,
    )
