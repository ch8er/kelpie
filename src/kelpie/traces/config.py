"""``[traces]`` configuration: OTLP ingest, the snapshot window and span retention."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.serde import parse_duration
from kelpie.traces.ingest import DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_CONCURRENT
from kelpie.traces.semconv import DEFAULT_SESSION_ATTRIBUTE, DEFAULT_TASK_ATTRIBUTE, MappingOptions
from kelpie.traces.store import DEFAULT_MAX_SNAPSHOT_SPANS

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
        "max_snapshot_spans",
        "max_concurrent_requests",
        "allow_unauthenticated",
    }
)
ALLOW_UNAUTHENTICATED_ENV = "KELPIE_TRACES_ALLOW_UNAUTHENTICATED"
_TRUE = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True, slots=True)
class TraceSettings:
    enabled: bool = True
    token_env: str = DEFAULT_TOKEN_ENV
    token_env_explicit: bool = False  # set in the config: the variable must then be set
    window: timedelta = timedelta(minutes=30)  # raw spans merged into each snapshot
    totals_window: timedelta = timedelta(hours=24)  # per-session aggregates in the snapshot
    retention: timedelta = timedelta(days=7)
    max_rows: int = 1_000_000
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    task_attribute: str = DEFAULT_TASK_ATTRIBUTE
    session_attribute: str = DEFAULT_SESSION_ATTRIBUTE
    max_snapshot_spans: int = DEFAULT_MAX_SNAPSHOT_SPANS
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT
    # Accept spans without a token on a non-loopback address (e.g. behind a proxy).
    allow_unauthenticated: bool = False

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

    def unauthenticated_allowed(self, environ: Mapping[str, str] = os.environ) -> bool:
        env = environ.get(ALLOW_UNAUTHENTICATED_ENV, "").strip().lower()
        return self.allow_unauthenticated or env in _TRUE

    def check_exposure(self, host: str, environ: Mapping[str, str] = os.environ) -> None:
        """Refuse an open ingest endpoint on a non-loopback address.

        Raises ValueError unless traces are disabled, a token is set, the bind host is
        loopback, or ``allow_unauthenticated`` (or its env var) says so on purpose.
        """
        if not self.enabled or self.token(environ) or is_loopback(host):
            return
        if self.unauthenticated_allowed(environ):
            return
        raise ValueError(
            f"OTLP ingest would accept spans from anyone on {host}: set ${self.token_env}, "
            f"bind to 127.0.0.1 (--host), set [traces] allow_unauthenticated = true "
            f"(or ${ALLOW_UNAUTHENTICATED_ENV}=1) behind a trusted network, or disable "
            f"[traces]"
        )


def is_loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


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
    for key, default in (("window", "30m"), ("totals_window", "24h"), ("retention", "7d")):
        try:
            durations[key] = parse_duration(section.get(key, default))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"[traces] {key}: {exc}") from exc
        if durations[key] <= timedelta(0):
            raise ValueError(f"[traces] {key} must be positive")
    if durations["retention"] < max(durations["window"], durations["totals_window"]):
        raise ValueError("[traces] retention must cover window and totals_window")
    ints: dict[str, int] = {}
    for key, default in (
        ("max_rows", 1_000_000),
        ("max_body_bytes", DEFAULT_MAX_BODY_BYTES),
        ("max_snapshot_spans", DEFAULT_MAX_SNAPSHOT_SPANS),
        ("max_concurrent_requests", DEFAULT_MAX_CONCURRENT),
    ):
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
        max_snapshot_spans=ints["max_snapshot_spans"],
        max_concurrent_requests=ints["max_concurrent_requests"],
        allow_unauthenticated=_flag(section, "allow_unauthenticated"),
    )


def _flag(section: Mapping[str, Any], key: str) -> bool:
    value = section.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"[traces] {key} must be true or false")
    return value
