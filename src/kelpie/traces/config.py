"""``[traces]`` configuration: OTLP ingest, the snapshot window and span retention."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.serde import parse_duration
from kelpie.traces.ingest import DEFAULT_MAX_BODY_BYTES
from kelpie.traces.semconv import DEFAULT_SESSION_ATTRIBUTE, DEFAULT_TASK_ATTRIBUTE, MappingOptions

DEFAULT_TOKEN_ENV = "KELPIE_TRACES_TOKEN"
KEYS = frozenset(
    {
        "enabled",
        "token_env",
        "window",
        "totals_window",
        "retention",
        "max_rows",
        "max_body_bytes",
        "task_attribute",
        "session_attribute",
    }
)


@dataclass(frozen=True, slots=True)
class TraceSettings:
    enabled: bool = True
    token_env: str = DEFAULT_TOKEN_ENV
    token_env_explicit: bool = False  # set in the config: the variable must then be set
    window: timedelta = timedelta(hours=2)  # raw spans merged into each snapshot
    totals_window: timedelta = timedelta(hours=24)  # per-session aggregates in the snapshot
    retention: timedelta = timedelta(days=7)
    max_rows: int = 1_000_000
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    task_attribute: str = DEFAULT_TASK_ATTRIBUTE
    session_attribute: str = DEFAULT_SESSION_ATTRIBUTE

    @property
    def options(self) -> MappingOptions:
        return MappingOptions(self.task_attribute, self.session_attribute)

    def token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The bearer token ingest requires, or None when it is open.

        Raises ValueError when ``token_env`` was configured explicitly but is not set: the
        operator asked for authentication, so Kelpie refuses to start without it.
        """
        token = environ.get(self.token_env, "").strip()
        if token:
            return token
        if self.token_env_explicit and self.enabled:
            raise ValueError(f"[traces] token_env: ${self.token_env} is not set")
        return None


def load_traces(section: Any) -> TraceSettings:
    """Parse ``[traces]``; raises ValueError with a message naming the bad key."""
    if section is None:
        return TraceSettings()
    if not isinstance(section, Mapping):
        raise ValueError("[traces] must be a table")
    if extra := set(section) - KEYS:
        raise ValueError(f"unknown [traces] keys: {sorted(extra)}")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("[traces] enabled must be true or false")
    strings: dict[str, str] = {}
    for key, default in (
        ("token_env", DEFAULT_TOKEN_ENV),
        ("task_attribute", DEFAULT_TASK_ATTRIBUTE),
        ("session_attribute", DEFAULT_SESSION_ATTRIBUTE),
    ):
        value = section.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"[traces] {key} must be a non-empty string")
        strings[key] = value.strip()
    durations: dict[str, timedelta] = {}
    for key, default in (("window", "2h"), ("totals_window", "24h"), ("retention", "7d")):
        try:
            durations[key] = parse_duration(section.get(key, default))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"[traces] {key}: {exc}") from exc
        if durations[key] <= timedelta(0):
            raise ValueError(f"[traces] {key} must be positive")
    if durations["retention"] < max(durations["window"], durations["totals_window"]):
        raise ValueError("[traces] retention must cover window and totals_window")
    ints: dict[str, int] = {}
    for key, default in (("max_rows", 1_000_000), ("max_body_bytes", DEFAULT_MAX_BODY_BYTES)):
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"[traces] {key} must be a positive integer")
        ints[key] = value
    return TraceSettings(
        enabled=enabled,
        token_env=strings["token_env"],
        token_env_explicit="token_env" in section,
        window=durations["window"],
        totals_window=durations["totals_window"],
        retention=durations["retention"],
        max_rows=ints["max_rows"],
        max_body_bytes=ints["max_body_bytes"],
        task_attribute=strings["task_attribute"],
        session_attribute=strings["session_attribute"],
    )
