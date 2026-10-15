"""``[alerts]``: where real-time alerts go, as config data.

::

    [alerts]
    dedup_window = "10m"        # identical alerts within this window are sent once per channel
    queue_size = 50             # per channel; an alert that does not fit is dropped and counted

    [[alerts.channels]]
    type = "telegram"
    token_env = "KELPIE_TELEGRAM_TOKEN"   # the bot token is read from this variable
    chat_id = "-1001234567890"
    timeout = "10s"
    min_severity = "warning"              # info | warning | critical

An alert goes to every channel whose ``min_severity`` it reaches. Secrets are read from the
environment when the channels are built; a missing or malformed one stops Kelpie at startup.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.alerts import telegram
from kelpie.alerts.delivery import (
    DEFAULT_QUEUE_SIZE,
    DEFAULT_WINDOW,
    AlertObserver,
    Delivery,
)
from kelpie.alerts.telegram import TelegramSettings
from kelpie.core.serde import parse_duration

SECTION = "[alerts]"
MAX_QUEUE = 1000

ChannelSettings = TelegramSettings  # a union as more channel types arrive (slack, ...)


@dataclass(frozen=True, slots=True)
class AlertSettings:
    channels: tuple[ChannelSettings, ...] = ()
    dedup_window: timedelta = DEFAULT_WINDOW
    queue_size: int = DEFAULT_QUEUE_SIZE

    def build(
        self, observer: AlertObserver | None = None, environ: Mapping[str, str] = os.environ
    ) -> Delivery | None:
        """The delivery layer, or None without channels. ``ValueError`` (naming the
        variable, never its value) when a secret is missing or malformed."""
        if not self.channels:
            return None
        built = [c.build(environ) for c in self.channels]
        return Delivery(built, observer, self.dedup_window, self.queue_size)

    def problems(self, environ: Mapping[str, str] = os.environ) -> list[str]:
        out = []
        for c in self.channels:
            try:
                c.check(environ)
            except ValueError as exc:
                out.append(str(exc))
        return out


def load_alerts(section: Any) -> AlertSettings:
    if section is None:
        return AlertSettings()
    if not isinstance(section, Mapping):
        raise ValueError(f"{SECTION} must be a table")
    if extra := set(section) - {"dedup_window", "queue_size", "channels"}:
        raise ValueError(f"{SECTION}: unknown keys {sorted(extra)}")
    try:
        window = parse_duration(section.get("dedup_window", DEFAULT_WINDOW))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{SECTION} dedup_window: {exc}") from exc
    if window < timedelta(0):
        raise ValueError(f"{SECTION} dedup_window must not be negative")
    size = section.get("queue_size", DEFAULT_QUEUE_SIZE)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_QUEUE:
        raise ValueError(f"{SECTION} queue_size must be between 1 and {MAX_QUEUE}")
    return AlertSettings(_channels(section.get("channels", [])), window, size)


def _channels(entries: Any) -> tuple[ChannelSettings, ...]:
    if not isinstance(entries, Sequence) or isinstance(entries, str):
        raise ValueError(f"{SECTION} channels must be an array of tables ([[alerts.channels]])")
    channels: list[ChannelSettings] = []
    for i, entry in enumerate(entries, 1):
        where = f"[[alerts.channels]] #{i}"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where} must be a table")
        kind = entry.get("type")
        if kind != telegram.TYPE:
            raise ValueError(f"{where}: type must be one of ['{telegram.TYPE}']")
        channels.append(telegram.load_telegram(entry, where))
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        raise ValueError(f"{SECTION} channel names must be unique (set name = ...)")
    return tuple(channels)
