"""Telegram and Slack endpoints that collect what Kelpie sends (loopback only).

Built on the fakes the channel unit tests use (:mod:`tests.alerts.fake_telegram`,
:mod:`tests.alerts.fake_slack`); this module only adds the settings a farm config needs and
reads the messages back as text: what a person sees (Telegram HTML without its tags and
entities; Slack's rich_text blocks as the text of their elements, else the plain text with
Slack's escaping undone). Nothing else is changed: what Kelpie sent is read as it is.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from typing import Any

from tests.alerts.fake_slack import FakeSlack, fake_slack
from tests.alerts.fake_telegram import FakeTelegram, fake_telegram

TELEGRAM_TOKEN = "123456:SimulatedFarmBotToken_abcdefghij"
TELEGRAM_TOKEN_ENV = "KELPIE_SIM_TELEGRAM_TOKEN"
SLACK_WEBHOOK_ENV = "KELPIE_SIM_SLACK_WEBHOOK"
SLACK_PATH = "/services/T000/B000/simulated"
CHAT_ID = "-1001234567890"
_TAG = re.compile(r"<[^>]*>")


def _shown(body: dict[str, Any]) -> str:
    """A Telegram message as it shows: HTML tags dropped and entities decoded."""
    text = str(body.get("text", ""))
    return html.unescape(_TAG.sub("", text)) if body.get("parse_mode") == "HTML" else text


def _slack_shown(body: dict[str, Any]) -> str:
    """A Slack message as it shows: the text elements of its rich_text blocks, one block per
    line group; a message without blocks (free text) is its text, Slack's escaping undone."""
    blocks = body.get("blocks")
    if not isinstance(blocks, list):
        text = str(body.get("text", ""))
        return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return "\n".join(
        "".join(str(e.get("text", "")) for section in b["elements"] for e in section["elements"])
        for b in blocks
    )


@dataclass
class Channels:
    telegram: FakeTelegram
    slack: FakeSlack

    def telegram_texts(self) -> list[str]:
        return [_shown(body) for _, _, body in list(self.telegram.requests)]

    def slack_texts(self) -> list[str]:
        return [_slack_shown(body) for _, _, body in list(self.slack.requests)]

    def slack_bodies(self) -> list[dict[str, Any]]:
        """What Kelpie posted to Slack, as it is."""
        return [body for _, _, body in list(self.slack.requests)]

    def texts(self) -> list[str]:
        return self.telegram_texts() + self.slack_texts()

    def env(self) -> dict[str, str]:
        """The secrets Kelpie reads from its environment."""
        return {
            TELEGRAM_TOKEN_ENV: TELEGRAM_TOKEN,
            SLACK_WEBHOOK_ENV: self.slack.origin + SLACK_PATH,
        }

    def config(self, telegram_min: str = "info", slack_min: str = "warning") -> dict[str, Any]:
        """The ``[alerts]`` section for both fakes."""
        return {
            "dedup_window": "10m",
            "channels": [
                {
                    "type": "telegram",
                    "token_env": TELEGRAM_TOKEN_ENV,
                    "chat_id": CHAT_ID,
                    "api_url": self.telegram.url,
                    "min_severity": telegram_min,
                    "timeout": "5s",
                },
                {
                    "type": "slack",
                    "webhook_env": SLACK_WEBHOOK_ENV,
                    "allow_host": "127.0.0.1",
                    "min_severity": slack_min,
                    "timeout": "5s",
                },
            ],
        }


@contextmanager
def serve() -> Iterator[Channels]:
    with ExitStack() as stack:
        telegram = stack.enter_context(fake_telegram())
        slack = stack.enter_context(fake_slack())
        yield Channels(telegram, slack)
