"""``[report]``: the daily "cost of chaos" report (rule 8)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KEYS = frozenset({"timezone", "dir", "task_url", "hour_usd", "channels", "severity"})
SEVERITIES = ("info", "warning", "critical")


@dataclass(frozen=True, slots=True)
class ReportSettings:
    """``timezone``: where a day starts and ends. ``dir``: where ``kelpie report`` writes
    ``kelpie-report-<date>.md`` (None: stdout only). ``task_url``: a link template with
    ``{id}`` for task ids. ``hour_usd``: what an hour of stuck work costs (e.g. an agent
    slot), to put chaos hours into dollars; None leaves hours as hours. ``channels``: the
    ``[[alerts.channels]]`` that get the summary (None: all), sent at ``severity``."""

    timezone: str = "UTC"
    dir: Path | None = None
    task_url: str | None = None
    hour_usd: float | None = None
    channels: tuple[str, ...] | None = None
    severity: str = "warning"

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
    return ReportSettings(
        timezone=tz.strip(),
        dir=None if out_dir is None else Path(out_dir.strip()).expanduser(),
        task_url=url,
        hour_usd=None if hour is None else float(hour),
        channels=None if channels is None else tuple(c.strip() for c in channels),
        severity=severity,
    )
