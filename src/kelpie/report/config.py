"""``[report]``: the daily "cost of chaos" report (rule 8)."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kelpie.core.serde import parse_duration
from kelpie.report.verify import DEFAULT_MIN_SAMPLE, DEFAULT_REVIEW_ROLES, DEFAULT_WINDOW

KEYS = frozenset(
    {
        "timezone",
        "dir",
        "task_url",
        "hour_usd",
        "channels",
        "severity",
        "verify_window",
        "review_roles",
        "min_sample",
        "at",
        "sections",
    }
)
SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True, slots=True)
class ReportSettings:
    """``timezone``: where a day starts and ends. ``dir``: where ``kelpie report`` writes
    ``kelpie-report-<date>.md`` (None: stdout only). ``task_url``: a link template with
    ``{id}`` for task ids. ``hour_usd``: what an hour of stuck work costs (e.g. an agent
    slot), to put chaos hours into dollars; None leaves hours as hours. ``channels``: the
    ``[[alerts.channels]]`` that get the summary (None: all), sent at ``severity``.
    Before/after of fixes: ``verify_window`` on each side, review time from the sessions of
    ``review_roles``, and fewer than ``min_sample`` observations on a side is a small
    sample. ``at``: when ``kelpie serve`` runs yesterday's report (local time; None: only
    ``kelpie report`` or cron). ``sections``: ``[report.sections.<name>]`` tables for that
    report section (``SectionContext.config``); keys are checked against what the section
    declares, values by the section."""

    timezone: str = "UTC"
    dir: Path | None = None
    task_url: str | None = None
    hour_usd: float | None = None
    channels: tuple[str, ...] | None = None
    severity: str = "warning"
    verify_window: timedelta = DEFAULT_WINDOW
    review_roles: tuple[str, ...] = DEFAULT_REVIEW_ROLES
    min_sample: int = DEFAULT_MIN_SAMPLE
    at: time | None = None  # local time kelpie serve runs yesterday's report (None: never)
    sections: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

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
        hour_usd=None if hour is None else float(hour),
        channels=None if channels is None else tuple(c.strip() for c in channels),
        severity=severity,
        verify_window=window,
        review_roles=tuple(r.strip() for r in roles),
        min_sample=sample,
        at=run_at,
        sections={k: dict(v) for k, v in sections.items()},
    )
