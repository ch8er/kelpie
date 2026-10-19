"""``[alerts]``: where real-time alerts go, as config data.

::

    [alerts]
    dedup_window = "10m"        # identical alerts within this window are sent once per channel
    queue_size = 50             # per channel; an alert that does not fit is dropped and counted
    retry_horizon = "15m"       # transient failures (429, 5xx, network) are retried this long
    language = "en"             # en | ru: Kelpie's own words in alerts (agents' text as is)
    task_url = "https://board.example.org/tasks/{id}"  # "Open task" links (default: [report])

    [[alerts.channels]]
    type = "telegram"
    token_env = "KELPIE_TELEGRAM_TOKEN"   # the bot token is read from this variable
    chat_id = "-1001234567890"
    timeout = "10s"
    min_severity = "warning"              # info | warning | critical
    format = "html"                       # html | plain

    [[alerts.channels]]
    type = "slack"
    webhook_env = "KELPIE_SLACK_WEBHOOK"  # the incoming-webhook URL is read from this variable
    min_severity = "critical"

An alert goes to every channel whose ``min_severity`` it reaches. ``task_url`` must be an
http(s) URL containing ``{id}``; unset, ``[report] task_url`` is used when it is one.
Secrets are read from the environment when the channels are built; a missing or malformed
one stops Kelpie at startup.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, timedelta, tzinfo
from typing import Any

from kelpie.alerts import i18n, slack, telegram
from kelpie.alerts.delivery import (
    DEFAULT_QUEUE_SIZE,
    DEFAULT_RETRY_HORIZON,
    DEFAULT_WINDOW,
    AlertObserver,
    Delivery,
)
from kelpie.alerts.render import DEFAULT_RENDERING, RenderSettings, check_task_url
from kelpie.alerts.slack import SlackSettings
from kelpie.alerts.telegram import TelegramSettings
from kelpie.core.serde import parse_duration

log = logging.getLogger(__name__)

SECTION = "[alerts]"
MAX_QUEUE = 1000
MAX_RETRY_HORIZON = timedelta(hours=24)

ChannelSettings = TelegramSettings | SlackSettings


@dataclass(frozen=True, slots=True)
class AlertSettings:
    channels: tuple[ChannelSettings, ...] = ()
    dedup_window: timedelta = DEFAULT_WINDOW
    queue_size: int = DEFAULT_QUEUE_SIZE
    retry_horizon: timedelta = DEFAULT_RETRY_HORIZON
    rendering: RenderSettings = DEFAULT_RENDERING  # language and task_url
    links_note: str | None = None  # why alerts carry no task links, when config says one

    def build(
        self, observer: AlertObserver | None = None, environ: Mapping[str, str] = os.environ
    ) -> Delivery | None:
        """The delivery layer, or None without channels. ``ValueError`` (naming the
        variable, never its value) when a secret is missing or malformed."""
        if not self.channels:
            return None
        built = [c.build(environ, self.rendering) for c in self.channels]
        return Delivery(
            built, observer, self.dedup_window, self.queue_size, retry_horizon=self.retry_horizon
        )

    def problems(self, environ: Mapping[str, str] = os.environ) -> list[str]:
        out = []
        for c in self.channels:
            try:
                c.check(environ)
            except ValueError as exc:
                out.append(str(exc))
        return out


_KEYS = {"dedup_window", "queue_size", "retry_horizon", "channels", "language", "task_url"}


def load_alerts(
    section: Any, report_task_url: str | None = None, tz: tzinfo = UTC
) -> AlertSettings:
    """``[alerts]``; ``report_task_url`` is ``[report] task_url``, the default link
    template (used only when it is an http(s) URL: the report may link elsewhere); ``tz``
    is ``[report] timezone``, where alerts show when a snooze ends."""
    if section is None:
        url, note = _inherited(report_task_url)
        return AlertSettings(rendering=RenderSettings(task_url=url, tz=tz), links_note=note)
    if not isinstance(section, Mapping):
        raise ValueError(f"{SECTION} must be a table")
    if extra := set(section) - _KEYS:
        raise ValueError(f"{SECTION}: unknown keys {sorted(extra)}")
    language = i18n.check_language(section.get("language", i18n.DEFAULT), f"{SECTION} language")
    note = None
    if "task_url" in section:
        task_url: str | None = check_task_url(section["task_url"], f"{SECTION} task_url")
    else:
        task_url, note = _inherited(report_task_url)
    try:
        window = parse_duration(section.get("dedup_window", DEFAULT_WINDOW))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{SECTION} dedup_window: {exc}") from exc
    if window < timedelta(0):
        raise ValueError(f"{SECTION} dedup_window must not be negative")
    size = section.get("queue_size", DEFAULT_QUEUE_SIZE)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_QUEUE:
        raise ValueError(f"{SECTION} queue_size must be between 1 and {MAX_QUEUE}")
    try:
        horizon = parse_duration(section.get("retry_horizon", DEFAULT_RETRY_HORIZON))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{SECTION} retry_horizon: {exc}") from exc
    if not timedelta(0) <= horizon <= MAX_RETRY_HORIZON:
        raise ValueError(f"{SECTION} retry_horizon must be between 0 and {MAX_RETRY_HORIZON}")
    rendering = RenderSettings(language, task_url, tz)
    channels = _channels(section.get("channels", []))
    return AlertSettings(channels, window, size, horizon, rendering, note)


def _inherited(url: str | None) -> tuple[str | None, str | None]:
    """``[report] task_url`` for alerts, or why not (it is not an http(s) link)."""
    if url is None:
        return None, None
    try:
        return check_task_url(url), None
    except ValueError:
        note = "[report] task_url is not an http(s) URL; set [alerts] task_url"
        log.warning("alerts carry no task links: %s", note)
        return None, note


def _channels(entries: Any) -> tuple[ChannelSettings, ...]:
    if not isinstance(entries, Sequence) or isinstance(entries, str):
        raise ValueError(f"{SECTION} channels must be an array of tables ([[alerts.channels]])")
    channels: list[ChannelSettings] = []
    for i, entry in enumerate(entries, 1):
        where = f"[[alerts.channels]] #{i}"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where} must be a table")
        kind = entry.get("type")
        if kind == telegram.TYPE:
            channels.append(telegram.load_telegram(entry, where))
        elif kind == slack.TYPE:
            channels.append(slack.load_slack(entry, where))
        else:
            raise ValueError(f"{where}: type must be one of ['{telegram.TYPE}', '{slack.TYPE}']")
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        raise ValueError(f"{SECTION} channel names must be unique (set name = ...)")
    return tuple(channels)
