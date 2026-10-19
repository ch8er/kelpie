"""``[report]``: the daily "cost of chaos" report (rule 8)."""

from __future__ import annotations

import math
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kelpie.alerts.render import check_url_template
from kelpie.core.serde import parse_duration
from kelpie.report.verify import DEFAULT_MIN_SAMPLE, DEFAULT_REVIEW_ROLES, DEFAULT_WINDOW

KEYS = frozenset(
    {
        "timezone",
        "dir",
        "task_url",
        "url",
        "hour_usd",
        "channels",
        "severity",
        "verify_window",
        "review_roles",
        "min_sample",
        "at",
        "sections",
        "llm",
    }
)
SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True, slots=True)
class ReportSettings:
    """``timezone``: where a day starts and ends. ``dir``: where ``kelpie report`` writes
    ``kelpie-report-<date>.md`` (None: stdout only). ``task_url``: a link template with
    ``{id}`` for task ids. ``url``: where the full report of a day can be read, an http(s)
    template with ``{date}`` (YYYY-MM-DD), linked from the chat summary (None: no link).
    ``hour_usd``: what an hour of stuck work costs (e.g. an agent slot), to put chaos hours
    into dollars; None leaves hours as hours. ``channels``: the
    ``[[alerts.channels]]`` that get the summary (None: all), sent at ``severity``.
    Before/after of fixes: ``verify_window`` on each side, review time from the sessions of
    ``review_roles``, and fewer than ``min_sample`` observations on a side is a small
    sample. ``at``: when ``kelpie serve`` runs yesterday's report (local time; None: only
    ``kelpie report`` or cron). ``sections``: ``[report.sections.<name>]`` tables for that
    report section (``SectionContext.config``); keys are checked against what the section
    declares, values by the section. ``llm``: the optional root-cause section
    (:mod:`kelpie.report.root_cause`); None: off."""

    timezone: str = "UTC"
    dir: Path | None = None
    task_url: str | None = None
    url: str | None = None  # the full report's address, with {date}: the summary's link
    hour_usd: float | None = None
    channels: tuple[str, ...] | None = None
    severity: str = "warning"
    verify_window: timedelta = DEFAULT_WINDOW
    review_roles: tuple[str, ...] = DEFAULT_REVIEW_ROLES
    min_sample: int = DEFAULT_MIN_SAMPLE
    at: time | None = None  # local time kelpie serve runs yesterday's report (None: never)
    sections: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    llm: LlmSettings | None = None  # [report.llm]: the optional LLM root-cause section

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def load_report(section: Any) -> ReportSettings:
    """Parse ``[report]``; raises ValueError naming the bad key."""
    if section is None:
        return ReportSettings()
    if not isinstance(section, Mapping):
        raise ValueError("[report] must be a table")
    if extra := set(section) - KEYS:
        raise ValueError(f"unknown [report] keys: {sorted(extra)}")
    tz = section.get("timezone", "UTC")
    if not isinstance(tz, str) or not tz.strip():
        raise ValueError("[report] timezone must be an IANA name such as Europe/Berlin")
    try:
        ZoneInfo(tz.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"[report] timezone: unknown time zone {tz!r}") from exc
    out_dir = section.get("dir")
    if out_dir is not None and (not isinstance(out_dir, str) or not out_dir.strip()):
        raise ValueError("[report] dir must be a non-empty path")
    url = section.get("task_url")
    if url is not None and (not isinstance(url, str) or "{id}" not in url):
        raise ValueError("[report] task_url must be a string containing {id}")
    report_url = section.get("url")
    if report_url is not None:
        check_url_template(report_url, "{date}", "[report] url")
    hour = section.get("hour_usd")
    if hour is not None and (
        isinstance(hour, bool)
        or not isinstance(hour, int | float)
        or not math.isfinite(hour)
        or hour <= 0
    ):
        raise ValueError("[report] hour_usd must be a positive number")
    channels = section.get("channels")
    if channels is not None and (
        not isinstance(channels, list)
        or not channels
        or not all(isinstance(c, str) and c.strip() for c in channels)
    ):
        raise ValueError("[report] channels must be a non-empty list of channel names")
    severity = section.get("severity", "warning")
    if severity not in SEVERITIES:
        raise ValueError(f"[report] severity must be one of {list(SEVERITIES)}")
    raw = section.get("verify_window", "1d")
    try:
        window = parse_duration(raw) if isinstance(raw, str) else None
    except ValueError:
        window = None
    if window is None or window <= timedelta(0):
        raise ValueError('[report] verify_window must be a duration such as "1d"')
    roles = section.get("review_roles", list(DEFAULT_REVIEW_ROLES))
    if not isinstance(roles, list) or not all(isinstance(r, str) and r.strip() for r in roles):
        raise ValueError("[report] review_roles must be a list of role names")
    at = section.get("at")
    run_at = None
    if at is not None:
        try:
            strict = isinstance(at, str) and re.fullmatch(r"\d\d:\d\d", at) is not None
            run_at = time.fromisoformat(at) if strict else None
        except ValueError:
            run_at = None
        if run_at is None:
            raise ValueError('[report] at must be a local time "HH:MM", e.g. "00:15"')
    sample = section.get("min_sample", DEFAULT_MIN_SAMPLE)
    if isinstance(sample, bool) or not isinstance(sample, int) or sample <= 0:
        raise ValueError("[report] min_sample must be a positive integer")
    sections = section.get("sections", {})
    if not isinstance(sections, Mapping) or not all(
        isinstance(k, str) and k and isinstance(v, Mapping) for k, v in sections.items()
    ):
        raise ValueError("[report] sections must be [report.sections.<name>] tables")
    return ReportSettings(
        timezone=tz.strip(),
        dir=None if out_dir is None else Path(out_dir.strip()).expanduser(),
        task_url=url,
        url=report_url,
        hour_usd=None if hour is None else float(hour),
        channels=None if channels is None else tuple(c.strip() for c in channels),
        severity=severity,
        verify_window=window,
        review_roles=tuple(r.strip() for r in roles),
        min_sample=sample,
        at=run_at,
        sections={k: dict(v) for k, v in sections.items()},
        llm=load_llm(section.get("llm")),
    )


# --- [report.llm]: the optional LLM root-cause section (kelpie.report.root_cause) --------

LLM_PROVIDERS: Mapping[str, tuple[str, str]] = {  # name -> (default base_url, default key env)
    "anthropic": ("https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    "openai": ("https://api.openai.com", "OPENAI_API_KEY"),
}
LLM_KEYS = frozenset({"provider", "model", "api_key_env", "base_url", "timeout", "max_tokens"})
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True, slots=True)
class LlmSettings:
    """``api_key_env``: where the key is read at report time; None sends no key (a server
    on this host). Kept apart from the client so the control loop never loads it."""

    provider: str
    model: str
    api_key_env: str | None
    base_url: str
    timeout: timedelta = timedelta(seconds=60)
    max_tokens: int = 1000

    @property
    def default_endpoint(self) -> bool:
        return self.base_url == LLM_PROVIDERS[self.provider][0]


def load_llm(section: Any) -> LlmSettings | None:
    """Parse ``[report.llm]`` (None when absent); raises ValueError naming the bad key.

    The provider's key goes only to the provider's own API: with another ``base_url``,
    ``api_key_env`` must be named explicitly, except on this host (localhost), where no
    key is sent unless one is named."""
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise ValueError("[report.llm] must be a table")
    if extra := set(section) - LLM_KEYS:
        raise ValueError(f"unknown [report.llm] keys: {sorted(extra)}")
    provider = section.get("provider")
    if not isinstance(provider, str) or provider not in LLM_PROVIDERS:
        raise ValueError(f"[report.llm] provider must be one of {sorted(LLM_PROVIDERS)}")
    model = section.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("[report.llm] model is required, e.g. a model id of the provider")
    default_url, default_env = LLM_PROVIDERS[provider]
    url = section.get("base_url", default_url)
    if not isinstance(url, str) or not _valid_base(url):
        raise ValueError(
            "[report.llm] base_url must be an https URL (http only on localhost), "
            "without credentials, query or fragment"
        )
    url = url.rstrip("/")
    env = section.get("api_key_env")
    if env is not None and (
        not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env)
    ):
        raise ValueError("[report.llm] api_key_env must be an environment variable name")
    if env is None and url != default_url:
        if urllib.parse.urlsplit(url).hostname not in _LOOPBACK:
            raise ValueError(
                f"[report.llm] api_key_env must be named for base_url {url!r}: the "
                f"provider's ${default_env} is only sent to {default_url}"
            )
    elif env is None:
        env = default_env
    raw = section.get("timeout", "60s")
    try:
        timeout = parse_duration(raw) if isinstance(raw, str) else None
    except ValueError:
        timeout = None
    if timeout is None or not timedelta(0) < timeout <= timedelta(minutes=10):
        raise ValueError('[report.llm] timeout must be a duration up to 10m, e.g. "60s"')
    tokens = section.get("max_tokens", 1000)
    if isinstance(tokens, bool) or not isinstance(tokens, int) or not 1 <= tokens <= 32000:
        raise ValueError("[report.llm] max_tokens must be an integer from 1 to 32000")
    return LlmSettings(provider, model.strip(), env, url, timeout, tokens)


def _valid_base(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.username or parts.password or parts.query or parts.fragment or not host:
        return False
    return parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK)
