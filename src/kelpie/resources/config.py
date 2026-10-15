"""``[resources]`` configuration. Off unless ``[resources.prometheus]`` is present."""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from kelpie.core.serde import parse_duration
from kelpie.resources.prometheus import (
    DEFAULT_HOST_LABEL,
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    QUERY_NAMES,
    PrometheusConfig,
    PrometheusSource,
    ReadObserver,
    valid_token,
)

DEFAULT_TOKEN_ENV = "KELPIE_PROMETHEUS_TOKEN"
MAX_TIMEOUT = timedelta(seconds=60)
MIN_INTERVAL = timedelta(seconds=5)
SECTIONS = frozenset({"prometheus"})
PROMETHEUS_KEYS = frozenset(
    {"url", "hosts", "host_label", "timeout", "interval", "token_env", "queries"}
)
_LABEL = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_QUERY_LENGTH = 2000


@dataclass(frozen=True, slots=True)
class PrometheusSettings:
    """``[resources.prometheus]``. The token comes from an environment variable."""

    url: str
    hosts: Mapping[str, str]
    host_label: str = DEFAULT_HOST_LABEL
    timeout: timedelta = timedelta(seconds=DEFAULT_TIMEOUT)
    interval: timedelta = DEFAULT_INTERVAL
    token_env: str = DEFAULT_TOKEN_ENV
    token_env_explicit: bool = False  # set in the config: the variable must then be set
    queries: Mapping[str, str] = field(default_factory=dict)

    @property
    def display_url(self) -> str:
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    def token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The bearer token, or None when Prometheus needs none.

        Raises ValueError when ``token_env`` was configured but is not set, or holds a
        value that cannot be a header (the message names the variable, never the value).
        """
        value = environ.get(self.token_env, "").strip()
        if not value:
            if self.token_env_explicit:
                raise ValueError(f"[resources.prometheus] token_env: ${self.token_env} is not set")
            return None
        if not valid_token(value):
            raise ValueError(
                f"[resources.prometheus] token_env: ${self.token_env} contains a line break "
                "or another control character"
            )
        return value

    def sends_token_in_clear(self, environ: Mapping[str, str] = os.environ) -> bool:
        """A token over plain http to a non-loopback host."""
        parts = urlsplit(self.url)
        return (
            parts.scheme == "http"
            and not _loopback(parts.hostname or "")
            and bool(environ.get(self.token_env, "").strip())
        )

    def build(
        self, observer: ReadObserver | None = None, environ: Mapping[str, str] = os.environ
    ) -> PrometheusSource:
        config = PrometheusConfig(
            url=self.url,
            hosts=dict(self.hosts),
            host_label=self.host_label,
            timeout=self.timeout.total_seconds(),
            interval=self.interval,
            token=self.token(environ),
            queries=dict(self.queries),
        )
        return PrometheusSource(config, observer)


@dataclass(frozen=True, slots=True)
class ResourceSettings:
    prometheus: PrometheusSettings | None = None  # None: no host readings


def _loopback(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def load_resources(section: Any) -> ResourceSettings:
    """Parse ``[resources]``; raises ValueError with a message naming the bad key."""
    if section is None:
        return ResourceSettings()
    if not isinstance(section, Mapping):
        raise ValueError("[resources] must be a table")
    if extra := set(section) - SECTIONS:
        raise ValueError(f"unknown [resources] entries: {sorted(extra)}")
    if "prometheus" not in section:
        return ResourceSettings()
    return ResourceSettings(_load_prometheus(section["prometheus"]))


def _string(section: Mapping[str, Any], key: str, default: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[resources.prometheus] {key} must be a non-empty string")
    return value.strip()


def _duration(section: Mapping[str, Any], key: str, default: timedelta) -> timedelta:
    try:
        value = parse_duration(section.get(key, default))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[resources.prometheus] {key}: {exc}") from exc
    if value <= timedelta(0):
        raise ValueError(f"[resources.prometheus] {key} must be positive")
    return value


def _load_prometheus(section: Any) -> PrometheusSettings:
    if not isinstance(section, Mapping):
        raise ValueError("[resources.prometheus] must be a table")
    if extra := set(section) - PROMETHEUS_KEYS:
        raise ValueError(f"unknown [resources.prometheus] keys: {sorted(extra)}")
    if "url" not in section:
        raise ValueError("[resources.prometheus] url is required")
    url = _string(section, "url", "")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("[resources.prometheus] url has an invalid port") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or port == 0:
        raise ValueError("[resources.prometheus] url must be an http(s) URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("[resources.prometheus] url must not contain credentials; use token_env")
    if parts.query or parts.fragment or _CONTROL.search(url) or " " in url:
        raise ValueError("[resources.prometheus] url must not contain a query, fragment or spaces")
    raw_hosts = section.get("hosts")
    if not isinstance(raw_hosts, Mapping) or not raw_hosts:
        raise ValueError(
            "[resources.prometheus] hosts must map each host name to its label value, "
            'e.g. host-a = "host-a:9100"'
        )
    hosts: dict[str, str] = {}
    for name, label_value in raw_hosts.items():
        if (
            not isinstance(label_value, str)
            or not label_value.strip()
            or _CONTROL.search(label_value)
            or not isinstance(name, str)
            or not name.strip()
        ):
            raise ValueError(
                f"[resources.prometheus] hosts values must be non-empty strings (host {name!r})"
            )
        hosts[name] = label_value.strip()
    if len(set(hosts.values())) != len(hosts):
        raise ValueError("[resources.prometheus] hosts: two hosts share one label value")
    host_label = _string(section, "host_label", DEFAULT_HOST_LABEL)
    if not _LABEL.fullmatch(host_label):
        raise ValueError("[resources.prometheus] host_label is not a valid Prometheus label name")
    timeout = _duration(section, "timeout", timedelta(seconds=DEFAULT_TIMEOUT))
    if timeout > MAX_TIMEOUT:
        raise ValueError(f"[resources.prometheus] timeout must be at most {MAX_TIMEOUT}")
    interval = _duration(section, "interval", DEFAULT_INTERVAL)
    if interval < MIN_INTERVAL:
        raise ValueError(f"[resources.prometheus] interval must be at least {MIN_INTERVAL}")
    token_env = _string(section, "token_env", DEFAULT_TOKEN_ENV)
    if not _ENV_NAME.fullmatch(token_env):
        raise ValueError("[resources.prometheus] token_env must be an environment variable name")
    raw_queries = section.get("queries", {})
    if not isinstance(raw_queries, Mapping):
        raise ValueError("[resources.prometheus.queries] must be a table")
    if extra := set(raw_queries) - QUERY_NAMES:
        raise ValueError(
            f"unknown [resources.prometheus.queries] keys: {sorted(extra)} "
            f"(known: {sorted(QUERY_NAMES)})"
        )
    queries: dict[str, str] = {}
    for key, text in raw_queries.items():
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_QUERY_LENGTH:
            raise ValueError(
                f"[resources.prometheus.queries] {key} must be a non-empty PromQL string "
                f"of at most {MAX_QUERY_LENGTH} characters"
            )
        queries[key] = text.strip()
    return PrometheusSettings(
        url=url,
        hosts=hosts,
        host_label=host_label,
        timeout=timeout,
        interval=interval,
        token_env=token_env,
        token_env_explicit="token_env" in section,
        queries=queries,
    )
