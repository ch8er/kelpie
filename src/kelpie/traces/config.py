"""``[traces]`` configuration: OTLP ingest, the snapshot window, span retention,
``[[traces.forward]]`` destinations and ``[traces.outputs]`` (huge tool outputs)."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from kelpie.core.serde import parse_duration
from kelpie.traces.auth import (
    TokenFileError,
    looks_like_token,
    parse_token_file,
    read_token_file,
    valid_name,
)
from kelpie.traces.forward import (
    DEFAULT_QUEUE_BYTES,
    DEFAULT_QUEUE_SIZE,
    DEFAULT_RETRIES,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    Destination,
    valid_header_name,
    valid_header_value,
)
from kelpie.traces.ingest import DEFAULT_MAX_BODY_BYTES, DEFAULT_MAX_CONCURRENT
from kelpie.traces.outputs import Filter, OutputSettings, normalize_platform
from kelpie.traces.semconv import DEFAULT_SESSION_ATTRIBUTE, DEFAULT_TASK_ATTRIBUTE, MappingOptions
from kelpie.traces.store import DEFAULT_MAX_SNAPSHOT_SPANS

MAX_RETENTION = timedelta(days=3650)
DEFAULT_TOKEN_ENV = "KELPIE_TRACES_TOKEN"
DEFAULT_TOKEN_GRACE = timedelta(hours=2)
MAX_TOKEN_GRACE = timedelta(days=7)
KEYS = frozenset(
    {
        "enabled",
        "token_env",
        "token_file",
        "token_grace",
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
        "forward",
        "commands",
        "outputs",
    }
)
OUTPUT_KEYS = frozenset(
    {"threshold_tokens", "min_calls", "repeat_days", "window", "min_sample", "owners", "filters"}
)
FILTER_KEYS = frozenset({"name", "how", "commands", "reduction", "platforms", "advice"})
ALLOW_UNAUTHENTICATED_ENV = "KELPIE_TRACES_ALLOW_UNAUTHENTICATED"
_TRUE = frozenset({"1", "true", "yes", "on"})
FORWARD_KEYS = frozenset(
    {
        "name",
        "endpoint",
        "headers",
        "headers_env",
        "timeout",
        "queue_size",
        "queue_bytes",
        "retries",
    }
)


@dataclass(frozen=True, slots=True)
class ForwardSettings:
    """One ``[[traces.forward]]`` destination. Secret header values come from env vars."""

    name: str
    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict)  # static, non-secret
    headers_env: Mapping[str, str] = field(default_factory=dict)  # header -> env var name
    timeout: timedelta = timedelta(seconds=DEFAULT_TIMEOUT)
    queue_size: int = DEFAULT_QUEUE_SIZE
    retries: int = DEFAULT_RETRIES
    queue_bytes: int = DEFAULT_QUEUE_BYTES

    @property
    def display_endpoint(self) -> str:
        """The endpoint without its query string (which may carry a key)."""
        parts = urlsplit(self.endpoint)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @property
    def sends_secrets_in_clear(self) -> bool:
        """Credentials from env vars over plain http to a non-loopback host."""
        parts = urlsplit(self.endpoint)
        return bool(self.headers_env) and parts.scheme == "http" and not _loopback(parts)

    def destination(self, environ: Mapping[str, str] = os.environ) -> Destination:
        """Resolve header values; raises ValueError when a named env var is not set or
        holds a value that cannot be a header (the message names the variable, never the
        value)."""
        headers = dict(self.headers)
        for header, var in self.headers_env.items():
            value = environ.get(var, "").strip()
            if not value:
                raise ValueError(f"[[traces.forward]] {self.name}: ${var} ({header}) is not set")
            if not valid_header_value(value):
                raise ValueError(
                    f"[[traces.forward]] {self.name}: ${var} ({header}) contains a line break "
                    "or another control character"
                )
            headers[header] = value
        return Destination(
            name=self.name,
            endpoint=self.endpoint,
            headers=headers,
            timeout=self.timeout.total_seconds(),
            queue_size=self.queue_size,
            retries=self.retries,
            queue_bytes=self.queue_bytes,
        )


def _loopback(parts: SplitResult) -> bool:
    return is_loopback(parts.hostname or "")


@dataclass(frozen=True, slots=True)
class TraceSettings:
    enabled: bool = True
    token_env: str = DEFAULT_TOKEN_ENV
    token_env_explicit: bool = False  # set in the config: the variable must then be set
    # An env file holding <token_env>=<token> (kelpie.traces.auth): read instead of the
    # environment, again when it changes; `kelpie traces rotate-token` writes it.
    token_file: Path | None = None
    token_grace: timedelta = DEFAULT_TOKEN_GRACE  # a rotated-out token stays valid this long
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
    forward: tuple[ForwardSettings, ...] = ()  # empty: forwarding disabled
    commands: bool = True  # keep the grouping prefix of shell commands on tool spans
    outputs: OutputSettings = field(default_factory=OutputSettings)

    @property
    def options(self) -> MappingOptions:
        return MappingOptions(self.task_attribute, self.session_attribute, self.commands)

    def token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The bearer token ingest requires, or None when it is open.

        Raises ValueError when ``token_env`` was configured explicitly but is not set, or
        ``token_file`` cannot be read or has no token: the operator asked for
        authentication, so Kelpie refuses to start without it. The message names the
        variable or the file, never a token.
        """
        if self.token_file is not None:
            if not self.enabled:
                return None
            try:
                text = read_token_file(self.token_file)
                found = parse_token_file(text, self.token_env, str(self.token_file)).token
            except OSError as exc:
                raise ValueError(
                    f"[traces] token_file {self.token_file}: cannot read it ({exc.strerror}); "
                    "`kelpie traces rotate-token` creates it"
                ) from None
            except TokenFileError as exc:
                raise ValueError(f"[traces] token_file {exc}") from None
            if found is None:
                raise ValueError(
                    f"[traces] token_file {self.token_file}: no {self.token_env}= line; "
                    "`kelpie traces rotate-token` writes one"
                )
            return found
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
    if durations["retention"] > MAX_RETENTION:
        raise ValueError("[traces] retention must be at most 3650d")
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
    outputs = load_outputs(section.get("outputs"))
    needed = max(2 * outputs.window, timedelta(days=outputs.repeat_days))
    if "outputs" in section and durations["retention"] < needed:
        raise ValueError(
            "[traces] retention must cover [traces.outputs] repeat_days and twice its window"
        )
    if looks_like_token(strings["token_env"]):  # never echoed
        raise ValueError("[traces] token_env looks like a token: name the variable instead")
    if not valid_name(strings["token_env"]):  # never echoed: it may be a pasted token
        raise ValueError("[traces] token_env must be the name of an environment variable")
    token_file = section.get("token_file")
    if token_file is not None and (not isinstance(token_file, str) or not token_file.strip()):
        raise ValueError("[traces] token_file must be a path")
    if token_file is not None and looks_like_token(Path(token_file.strip()).name):
        raise ValueError("[traces] token_file looks like a token, not a path to a file")
    try:
        token_grace = parse_duration(section.get("token_grace", "2h"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[traces] token_grace: {exc}") from exc
    if not timedelta(0) <= token_grace <= MAX_TOKEN_GRACE:
        raise ValueError("[traces] token_grace must be between 0s and 7d")
    forward = _load_forward(section.get("forward", []))
    if forward and not enabled:
        raise ValueError(
            "[[traces.forward]] needs OTLP ingest: remove it or set [traces] enabled = true"
        )
    return TraceSettings(
        enabled=enabled,
        token_env=strings["token_env"],
        token_env_explicit="token_env" in section,
        token_file=Path(token_file.strip()).expanduser() if token_file is not None else None,
        token_grace=token_grace,
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
        forward=forward,
        commands=_flag_default(section, "commands"),
        outputs=outputs,
    )


def _flag(section: Mapping[str, Any], key: str) -> bool:
    value = section.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"[traces] {key} must be true or false")
    return value


def _flag_default(section: Mapping[str, Any], key: str, default: bool = True) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"[traces] {key} must be true or false")
    return value


def load_outputs(section: Any) -> OutputSettings:
    """Parse ``[traces.outputs]``; ``[[traces.outputs.filters]]`` replaces the default
    filter table as a whole (order matters: the first match wins)."""
    if section is None:
        return OutputSettings()
    if not isinstance(section, Mapping):
        raise ValueError("[traces.outputs] must be a table")
    if extra := set(section) - OUTPUT_KEYS:
        raise ValueError(f"unknown [traces.outputs] keys: {sorted(extra)}")
    defaults = OutputSettings()
    ints: dict[str, int] = {}
    for key in ("threshold_tokens", "min_calls", "repeat_days", "min_sample"):
        value = section.get(key, getattr(defaults, key))
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"[traces.outputs] {key} must be a positive integer")
        ints[key] = value
    raw_window = section.get("window", "1d")
    if not isinstance(raw_window, str):
        raise ValueError('[traces.outputs] window must be a duration string such as "1d"')
    try:
        window = parse_duration(raw_window)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[traces.outputs] window: {exc}") from exc
    if window <= timedelta(0):
        raise ValueError("[traces.outputs] window must be positive")
    owners = section.get("owners", {})
    if not isinstance(owners, Mapping) or not all(
        isinstance(k, str) and k and isinstance(v, str) and v.strip() for k, v in owners.items()
    ):
        raise ValueError("[traces.outputs] owners must map roles to owner names")
    filters = defaults.filters
    if "filters" in section:
        filters = _load_filters(section["filters"])
    return OutputSettings(
        threshold_tokens=ints["threshold_tokens"],
        min_calls=ints["min_calls"],
        repeat_days=ints["repeat_days"],
        min_sample=ints["min_sample"],
        window=window,
        filters=filters,
        owners={k: v.strip() for k, v in owners.items()},
    )


def _load_filters(raw: Any) -> tuple[Filter, ...]:
    if not isinstance(raw, list) or not raw or not all(isinstance(x, Mapping) for x in raw):
        raise ValueError("[[traces.outputs.filters]] must be a non-empty array of tables")
    out: list[Filter] = []
    for i, entry in enumerate(raw):
        where = f"[[traces.outputs.filters]] #{i + 1}"
        if extra := set(entry) - FILTER_KEYS:
            raise ValueError(f"unknown {where} keys: {sorted(extra)}")
        strings = {}
        for key in ("name", "how"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{where}: {key} must be a non-empty string")
            strings[key] = value.strip()
        lists: dict[str, tuple[str, ...] | None] = {}
        for key in ("commands", "platforms"):
            value = entry.get(key)
            if value is None and key == "platforms":
                lists[key] = None
                continue
            if (
                not isinstance(value, list)
                or not value
                or not all(isinstance(v, str) and v.strip() for v in value)
            ):
                raise ValueError(f"{where}: {key} must be a non-empty list of strings")
            lists[key] = tuple(v.strip() for v in value)
        reduction = entry.get("reduction")
        if isinstance(reduction, bool) or not isinstance(reduction, int | float):
            raise ValueError(f"{where}: reduction must be a number between 0 and 1")
        if not 0 < reduction < 1:
            raise ValueError(f"{where}: reduction must be between 0 and 1")
        platforms = lists["platforms"]
        commands = lists["commands"]
        assert commands is not None
        advice = entry.get("advice")
        if advice is not None and (not isinstance(advice, str) or not advice.strip()):
            raise ValueError(f"{where}: advice must be a non-empty string")
        out.append(
            Filter(
                name=strings["name"],
                how=strings["how"],
                commands=commands,
                reduction=float(reduction),
                platforms=None if platforms is None else tuple(map(_platform, platforms)),
                advice=None if advice is None else advice.strip(),
            )
        )
    return tuple(out)


def _platform(name: str) -> str:
    return normalize_platform(name) or name


def _load_forward(raw: Any) -> tuple[ForwardSettings, ...]:
    if not isinstance(raw, list) or not all(isinstance(x, Mapping) for x in raw):
        raise ValueError("[[traces.forward]] must be an array of tables")
    out: list[ForwardSettings] = []
    for i, entry in enumerate(raw):
        where = f"[[traces.forward]] #{i + 1}"
        if extra := set(entry) - FORWARD_KEYS:
            raise ValueError(f"unknown {where} keys: {sorted(extra)}")
        endpoint = entry.get("endpoint")
        if not isinstance(endpoint, str):
            raise ValueError(f"{where}: endpoint is required")
        parsed = urlsplit(endpoint.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"{where}: endpoint must be an http(s) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                f"{where}: endpoint must not contain credentials; use headers_env "
                "(e.g. Authorization from an env var)"
            )
        name = entry.get("name", parsed.hostname)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"{where}: name must be a non-empty string")
        headers: dict[str, dict[str, str]] = {}
        for key in ("headers", "headers_env"):
            table = entry.get(key, {})
            if not isinstance(table, Mapping) or not all(
                isinstance(k, str) and k and isinstance(v, str) and v for k, v in table.items()
            ):
                raise ValueError(f"{where}: {key} must be a table of non-empty strings")
            if bad := [k for k in table if not valid_header_name(k)]:
                raise ValueError(f"{where}: {key} has invalid header names {bad}")
            if key == "headers" and (
                bad := [k for k, v in table.items() if not valid_header_value(v)]
            ):
                raise ValueError(f"{where}: headers {bad} contain control characters")
            headers[key] = dict(table)
        try:
            timeout = parse_duration(entry.get("timeout", DEFAULT_TIMEOUT))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"{where} timeout: {exc}") from exc
        if timeout <= timedelta(0):
            raise ValueError(f"{where}: timeout must be positive")
        ints: dict[str, int] = {}
        for key, default, low, high in (
            ("queue_size", DEFAULT_QUEUE_SIZE, 1, None),
            ("queue_bytes", DEFAULT_QUEUE_BYTES, 1, None),
            ("retries", DEFAULT_RETRIES, 0, MAX_RETRIES),
        ):
            value = entry.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int) or value < low:
                raise ValueError(f"{where}: {key} must be an integer >= {low}")
            if high is not None and value > high:
                raise ValueError(f"{where}: {key} must be at most {high}")
            ints[key] = value
        out.append(
            ForwardSettings(
                name=name.strip(),
                endpoint=endpoint.strip(),
                headers=headers["headers"],
                headers_env=headers["headers_env"],
                timeout=timeout,
                queue_size=ints["queue_size"],
                retries=ints["retries"],
                queue_bytes=ints["queue_bytes"],
            )
        )
    names = [f.name for f in out]
    if len(names) != len(set(names)):
        raise ValueError("[[traces.forward]] names must be unique (they label metrics)")
    return tuple(out)
