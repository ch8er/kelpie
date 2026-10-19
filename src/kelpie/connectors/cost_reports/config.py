"""``[connectors.cost_reports.<provider>]``: providers' cost reports, read with an admin key.

::

    [connectors.cost_reports.anthropic]
    kind = "anthropic"                      # or "openai"
    admin_key_env = "KELPIE_ANTHROPIC_ADMIN_KEY"

The section name is the provider (the name ``[spend.providers]`` and the spend modes use):
a provider with a cost report pays per token, so its spend mode is ``api`` unless set
otherwise. ``kind`` picks the API shape (:mod:`kelpie.connectors.cost_reports.source`);
``api_url`` defaults to the provider's public API. The admin key is read from the variable
``admin_key_env`` names when the connector is built; a variable that is not set stops
Kelpie at startup. It must be a separate, read-only key where the provider has one
(Anthropic: an Admin API key, ``sk-ant-admin...``; OpenAI: an admin key with read access
to usage): it is never the key the agents use. ``workspaces`` (Anthropic) or
``projects`` (OpenAI) limit the figures to the farm's own when the organization runs
other things too.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie import net
from kelpie.connectors.cost_reports.source import (
    DEFAULT_API_URLS,
    DEFAULT_DAYS,
    DEFAULT_INTERVAL,
    DEFAULT_TIMEOUT,
    MAX_DAYS,
    CostReportConfig,
    CostReportSource,
    ReportKind,
)
from kelpie.core.serde import parse_duration

SECTION = "[connectors.cost_reports]"
PROVIDER_KEYS = frozenset(
    {"kind", "admin_key_env", "api_url", "interval", "timeout", "days", "workspaces", "projects"}
)
MIN_INTERVAL = timedelta(minutes=5)  # the APIs ask for at most about one poll a minute
MAX_TIMEOUT = timedelta(seconds=60)
MAX_PROVIDERS = 10
MAX_SCOPES = 50
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SCOPE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")


@dataclass(frozen=True, slots=True)
class CostReportSettings:
    """One ``[connectors.cost_reports.<provider>]`` table, before the key is read."""

    provider: str
    kind: ReportKind
    admin_key_env: str
    api_url: str
    interval: timedelta = DEFAULT_INTERVAL
    timeout: timedelta = timedelta(seconds=DEFAULT_TIMEOUT)
    days: int = DEFAULT_DAYS
    scopes: tuple[str, ...] = ()  # workspace ids (anthropic) or project ids (openai)

    def admin_key(self, environ: Mapping[str, str] = os.environ) -> str:
        """The key; raises ValueError naming the variable (never its value)."""
        value = environ.get(self.admin_key_env, "").strip()
        where = f"[connectors.cost_reports.{self.provider}]: ${self.admin_key_env}"
        if not value:
            raise ValueError(f"{where} is not set")
        if not net.valid_header_value(value):
            raise ValueError(
                f"{where} must be printable ASCII (no line breaks or other control characters)"
            )
        return value

    def build(self, environ: Mapping[str, str] = os.environ) -> CostReportConfig:
        return CostReportConfig(
            provider=self.provider,
            kind=self.kind,
            api_url=self.api_url,
            admin_key=self.admin_key(environ),
            interval=self.interval,
            timeout=self.timeout.total_seconds(),
            days=self.days,
            scopes=self.scopes,
        )


@dataclass(frozen=True, slots=True)
class CostReportsSettings:
    reports: tuple[CostReportSettings, ...] = ()

    @property
    def providers(self) -> frozenset[str]:
        return frozenset(r.provider for r in self.reports)

    def build(self, environ: Mapping[str, str] = os.environ) -> CostReportSource:
        return CostReportSource([r.build(environ) for r in self.reports])


def load_cost_reports(section: Any) -> CostReportsSettings | None:
    """Parse ``[connectors.cost_reports]``; raises ValueError naming the bad key."""
    if section is None:
        return None
    if not isinstance(section, Mapping) or not section:
        raise ValueError(f"{SECTION} must be a table of providers")
    if len(section) > MAX_PROVIDERS:
        raise ValueError(f"{SECTION} allows at most {MAX_PROVIDERS} providers")
    return CostReportsSettings(tuple(_report(name, entry) for name, entry in section.items()))


def _report(name: Any, entry: Any) -> CostReportSettings:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"{SECTION} provider names must be 1-63 letters, digits, '.', '_' or '-'")
    where = f"[connectors.cost_reports.{name}]"
    if not isinstance(entry, Mapping):
        raise ValueError(f"{where} must be a table")
    if extra := set(entry) - PROVIDER_KEYS:
        raise ValueError(f"unknown {where} keys: {sorted(extra)}")
    try:
        kind = ReportKind(entry.get("kind"))
    except ValueError:
        raise ValueError(f"{where} kind must be one of {[str(k) for k in ReportKind]}") from None
    env = entry.get("admin_key_env")
    if not isinstance(env, str) or not _ENV_NAME.fullmatch(env):
        raise ValueError(f"{where} admin_key_env must be an environment variable name")
    api_url = net.check_api_url(entry.get("api_url", DEFAULT_API_URLS[kind]), f"{where}")
    interval = _duration(entry, "interval", DEFAULT_INTERVAL, where)
    if interval < MIN_INTERVAL:
        raise ValueError(f"{where} interval must be at least {MIN_INTERVAL}")
    timeout = _duration(entry, "timeout", timedelta(seconds=DEFAULT_TIMEOUT), where)
    if timeout <= timedelta(0) or timeout > MAX_TIMEOUT:
        raise ValueError(f"{where} timeout must be more than 0 and at most {MAX_TIMEOUT}")
    days = entry.get("days", DEFAULT_DAYS)
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"{where} days must be a whole number from 1 to {MAX_DAYS}")
    own, other = (
        ("workspaces", "projects")
        if kind is ReportKind.ANTHROPIC
        else (
            "projects",
            "workspaces",
        )
    )
    if other in entry:
        raise ValueError(f"{where} kind {kind} filters by {own}, not {other}")
    scopes = entry.get(own, [])
    if (
        not isinstance(scopes, list)
        or len(scopes) > MAX_SCOPES
        or not all(isinstance(s, str) and _SCOPE_ID.fullmatch(s) for s in scopes)
    ):
        raise ValueError(f"{where} {own} must be a list of ids (letters, digits, '_', '-')")
    return CostReportSettings(
        provider=name,
        kind=kind,
        admin_key_env=env,
        api_url=api_url,
        interval=interval,
        timeout=timeout,
        days=days,
        scopes=tuple(scopes),
    )


def _duration(entry: Mapping[str, Any], key: str, default: timedelta, where: str) -> timedelta:
    if key not in entry:
        return default
    try:
        return parse_duration(entry[key])
    except (ValueError, TypeError):
        raise ValueError(f'{where} {key} must be a duration such as "1h"') from None
