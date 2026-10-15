"""``[connectors.llm_quotas]``: LLM providers to probe, as config data.

::

    [[connectors.llm_quotas.providers]]
    name = "provider-main"
    kind = "headers"                  # or "status_url"
    api_url = "https://api.provider.example"
    path = "/v1/models"               # a zero-cost endpoint: never one that generates text
    token_env = "KELPIE_PROVIDER_MAIN_KEY"

Keys are read from environment variables when the connector is built; a variable that is
not set stops Kelpie at startup. ``api_url`` must be https (plain http only to loopback).
``tick_budget`` (in ``[connectors.llm_quotas]``) caps the wall-clock time all probes of one
tick may take together.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from kelpie import net
from kelpie.connectors.llm_quotas.source import (
    DEFAULT_HEADER_FIELDS,
    DEFAULT_INTERVAL,
    DEFAULT_TICK_BUDGET,
    DEFAULT_TIMEOUT,
    FIELDS,
    MIN_PROBE_TIME,
    LlmQuotaSource,
    ProbeKind,
    ProbeObserver,
    ProviderConfig,
)
from kelpie.core.serde import parse_duration

SECTION = "[connectors.llm_quotas]"
SECTION_KEYS = frozenset({"providers", "tick_budget"})
PROVIDER_KEYS = frozenset(
    {
        "name",
        "kind",
        "api_url",
        "path",
        "token_env",
        "auth_header",
        "auth_scheme",
        "headers",
        "fields",
        "unit",
        "interval",
        "timeout",
    }
)
MAX_TIMEOUT = timedelta(seconds=60)
MIN_INTERVAL = timedelta(seconds=30)
MAX_PROVIDERS = 20
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,30}")
_JSON_PATH = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){0,9}")
_CONTROL = re.compile(r"[\x00-\x20\x7f]")
# Headers Kelpie sets itself or that only a client library should manage.
_RESERVED_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "cookie", "proxy-authorization"}
)


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """One ``[[connectors.llm_quotas.providers]]`` entry, before the key is read."""

    name: str
    kind: ProbeKind
    api_url: str
    path: str
    token_env: str
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    headers: Mapping[str, str] = field(default_factory=dict)
    fields: Mapping[str, str] = field(default_factory=dict)
    unit: str = ""
    interval: timedelta = DEFAULT_INTERVAL
    timeout: timedelta = timedelta(seconds=DEFAULT_TIMEOUT)

    @property
    def display_url(self) -> str:
        parts = urlsplit(self.api_url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def token(self, environ: Mapping[str, str] = os.environ) -> str:
        """The key; raises ValueError naming the variable (never its value)."""
        value = environ.get(self.token_env, "").strip()
        where = f"{SECTION} provider {self.name}: ${self.token_env}"
        if not value:
            raise ValueError(f"{where} is not set")
        if not net.valid_header_value(value):
            raise ValueError(
                f"{where} must be printable ASCII (no line breaks or other control characters)"
            )
        return value

    def build(self, environ: Mapping[str, str] = os.environ) -> ProviderConfig:
        return ProviderConfig(
            name=self.name,
            kind=self.kind,
            api_url=self.api_url,
            path=self.path,
            token=self.token(environ),
            auth_header=self.auth_header,
            auth_scheme=self.auth_scheme,
            headers=dict(self.headers),
            fields=dict(self.fields),
            unit=self.unit,
            interval=self.interval,
            timeout=self.timeout.total_seconds(),
        )


@dataclass(frozen=True, slots=True)
class LlmQuotaSettings:
    providers: tuple[ProviderSettings, ...] = ()
    tick_budget: timedelta = timedelta(seconds=DEFAULT_TICK_BUDGET)

    def build(
        self, observer: ProbeObserver | None = None, environ: Mapping[str, str] = os.environ
    ) -> LlmQuotaSource:
        return LlmQuotaSource(
            [p.build(environ) for p in self.providers],
            observer,
            tick_budget=self.tick_budget.total_seconds(),
        )


def load_llm_quotas(section: Any) -> LlmQuotaSettings | None:
    """Parse ``[connectors.llm_quotas]``; raises ValueError naming the bad key."""
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise ValueError(f"{SECTION} must be a table")
    if extra := set(section) - SECTION_KEYS:
        raise ValueError(f"unknown {SECTION} keys: {sorted(extra)}")
    raw = section.get("providers", [])
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{SECTION} needs at least one [[connectors.llm_quotas.providers]]")
    if len(raw) > MAX_PROVIDERS:
        raise ValueError(f"{SECTION} allows at most {MAX_PROVIDERS} providers")
    providers = tuple(_provider(p, i) for i, p in enumerate(raw))
    names = [p.name for p in providers]
    if len(set(names)) != len(names):
        raise ValueError(f"{SECTION} provider names must be unique")
    budget = _duration(section, "tick_budget", timedelta(seconds=DEFAULT_TICK_BUDGET), SECTION)
    if budget < timedelta(seconds=MIN_PROBE_TIME) or budget > MAX_TIMEOUT:
        raise ValueError(
            f"{SECTION} tick_budget must be between {MIN_PROBE_TIME}s and {MAX_TIMEOUT}"
        )
    return LlmQuotaSettings(providers, budget)


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _provider(entry: Any, index: int) -> ProviderSettings:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{SECTION} providers[{index}] must be a table")
    raw_name = entry.get("name")
    if not isinstance(raw_name, str) or not _NAME.fullmatch(raw_name):
        raise ValueError(
            f"{SECTION} providers[{index}] name must be 1-63 letters, digits, '.', '_' or '-'"
        )
    name = raw_name
    where = f"{SECTION} provider {name}:"
    if extra := set(entry) - PROVIDER_KEYS:
        raise ValueError(f"{where} unknown keys {sorted(extra)}")
    try:
        kind = ProbeKind(entry.get("kind"))
    except ValueError:
        raise ValueError(f"{where} kind must be one of {[str(k) for k in ProbeKind]}") from None
    api_url = _api_url(entry.get("api_url"), where)
    path = entry.get("path")
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or "#" in path
        or _CONTROL.search(path)
    ):
        raise ValueError(
            f"{where} path must be an absolute path on api_url (e.g. /v1/models), "
            "without spaces or a fragment"
        )
    token_env = entry.get("token_env")
    if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
        raise ValueError(f"{where} token_env must be an environment variable name")
    auth_header = entry.get("auth_header", "Authorization")
    if not isinstance(auth_header, str) or not net.valid_header_name(auth_header):
        raise ValueError(f"{where} auth_header must be a header name")
    if auth_header.lower() in _RESERVED_HEADERS:
        raise ValueError(f"{where} auth_header cannot be {auth_header}")
    auth_scheme = entry.get("auth_scheme", "Bearer")
    if not isinstance(auth_scheme, str) or (auth_scheme and not _SCHEME.fullmatch(auth_scheme)):
        raise ValueError(f'{where} auth_scheme must be a word such as "Bearer", or ""')
    headers = _headers(entry.get("headers", {}), auth_header, where)
    fields = _fields(entry.get("fields"), kind, where)
    unit = entry.get("unit", "")
    if not isinstance(unit, str) or len(unit) > 32 or _CONTROL.search(unit.replace(" ", "")):
        raise ValueError(f"{where} unit must be a short string, e.g. tokens or requests")
    interval = _duration(entry, "interval", DEFAULT_INTERVAL, where)
    if interval < MIN_INTERVAL:
        raise ValueError(f"{where} interval must be at least {MIN_INTERVAL}")
    timeout = _duration(entry, "timeout", timedelta(seconds=DEFAULT_TIMEOUT), where)
    if timeout > MAX_TIMEOUT:
        raise ValueError(f"{where} timeout must be at most {MAX_TIMEOUT}")
    return ProviderSettings(
        name=name,
        kind=kind,
        api_url=api_url,
        path=path,
        token_env=token_env,
        auth_header=auth_header,
        auth_scheme=auth_scheme,
        headers=headers,
        fields=fields,
        unit=unit.strip(),
        interval=interval,
        timeout=timeout,
    )


def _api_url(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} api_url is required")
    url = value.strip()
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"{where} api_url has an invalid port") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or port == 0:
        raise ValueError(f"{where} api_url must be an https URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{where} api_url must not contain credentials; use token_env")
    if parts.query or parts.fragment or _CONTROL.search(url):
        raise ValueError(f"{where} api_url must not contain a query, fragment or spaces")
    if parts.scheme == "http" and not _loopback(parts.hostname):
        raise ValueError(f"{where} api_url must use https (plain http only to loopback)")
    return url


def _headers(value: Any, auth_header: str, where: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} headers must be a table of header names to values")
    out: dict[str, str] = {}
    for key, text in value.items():
        if not isinstance(key, str) or not net.valid_header_name(key):
            raise ValueError(f"{where} headers: {key!r} is not a header name")
        lowered = key.lower()
        if lowered in _RESERVED_HEADERS or lowered == auth_header.lower():
            raise ValueError(f"{where} headers cannot set {key}")
        if not isinstance(text, str) or not net.valid_header_value(text):
            raise ValueError(f"{where} headers: {key} needs a printable ASCII value")
        out[key] = text
    return out


def _fields(value: Any, kind: ProbeKind, where: str) -> dict[str, str]:
    if value is None:
        if kind is ProbeKind.STATUS_URL:
            raise ValueError(f"{where} kind status_url needs [fields] (dotted JSON paths)")
        return dict(DEFAULT_HEADER_FIELDS)
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{where} fields must be a non-empty table")
    if extra := set(value) - set(FIELDS):
        raise ValueError(f"{where} unknown fields {sorted(extra)} (known: {list(FIELDS)})")
    out: dict[str, str] = {}
    for key, text in value.items():
        ok = isinstance(text, str) and (
            net.valid_header_name(text)
            if kind is ProbeKind.HEADERS
            else _JSON_PATH.fullmatch(text) is not None
        )
        if not ok:
            what = "a header name" if kind is ProbeKind.HEADERS else "a dotted JSON path"
            raise ValueError(f"{where} fields.{key} must be {what}")
        out[key] = text
    return out


def _duration(entry: Mapping[str, Any], key: str, default: timedelta, where: str) -> timedelta:
    try:
        value = parse_duration(entry.get(key, default))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{where} {key}: {exc}") from exc
    if value <= timedelta(0):
        raise ValueError(f"{where} {key} must be positive")
    return value
