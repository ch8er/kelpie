"""Alert sinks (stub). Slack incoming webhook first."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SlackAlerter:
    webhook_url: str

    def send(self, title: str, body: str, severity: str) -> None:
        raise NotImplementedError
