"""The Slack channel: an incoming webhook.

The webhook URL is a secret (whoever has it can post to the channel), so it is handled like
the Telegram bot token: read from the environment variable named in config, validated at
startup (the error names the variable, never the value), kept out of ``repr``, logs, metrics,
``kelpie status`` and errors. Errors come from :mod:`kelpie.net`, which never quotes the
path, and the little of Slack's own error text that is logged is first checked against a
short ``[a-z_]`` whitelist (``invalid_token``, ``no_service``) and redacted.

The URL must be https on ``hooks.slack.com``, or on the host named in ``allow_host`` (plain
http only for a loopback ``allow_host``, for tests and local relays). Redirects are refused,
so the URL is never sent anywhere else.

Messages have the layout of the Telegram ones: plain text in the ``text`` field. Slack
formatting is switched off (``mrkdwn: false``, no link unfurling) and ``&``, ``<`` and ``>``
are escaped as Slack requires, so untrusted task text cannot become a mention or a link.
The caps are Telegram's (the text stays far below Slack's limit of about 40000 characters,
even with every character escaped).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from kelpie import net
from kelpie.alerts.delivery import PermanentError, TransientError
from kelpie.alerts.telegram import (
    DEFAULT_TIMEOUT,
    DID,
    MAX_BODY,
    MAX_HEAD,
    MAX_RESPONSE,
    MAX_TIMEOUT,
    clean,
    lead,
    loopback,
    redact,
    truncate,
)
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertMessage
from kelpie.core.serde import parse_duration

TYPE = "slack"
SLACK_HOST = "hooks.slack.com"
MAX_RETRY_AFTER = 86400.0  # a longer wait is passed on capped; delivery gives up on it
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,300}")
_ERROR = re.compile(r"[a-z][a-z_]{0,39}")  # Slack's short error codes, e.g. no_service
_KEYS = {"type", "name", "webhook_env", "timeout", "min_severity", "allow_host"}


def escape(text: str) -> str:
    """Slack's required escaping; ``&`` first so the entities are not escaped again."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(message: AlertMessage) -> str:
    """The text of a message, laid out as in Telegram, then escaped for Slack."""
    if message.text is not None:
        return escape(truncate(clean(message.text)))
    head = f"[{message.severity.value.upper()}] "
    head += f"{truncate(clean(message.rule), MAX_HEAD)}: " if message.rule else ""
    # Each part has its own cap, so the last line (what Kelpie did) always survives.
    body = truncate(clean(lead(message) + message.body.strip()), MAX_BODY)
    lines = [head + truncate(clean(message.subject), MAX_HEAD), body, DID[message.did]]
    return escape(truncate("\n".join(lines)))


@dataclass(frozen=True, slots=True)
class SlackSettings:
    webhook_env: str
    name: str = TYPE
    timeout: timedelta = DEFAULT_TIMEOUT
    min_severity: Severity = Severity.WARNING
    allow_host: str = ""

    @property
    def type(self) -> str:
        return TYPE

    def describe(self) -> str:
        return f"slack (from {self.min_severity.value}, webhook from ${self.webhook_env})"

    def webhook(self, environ: Mapping[str, str] = os.environ) -> tuple[str, str]:
        """The webhook as (origin, path). A ``ValueError`` names the variable, never its
        value."""
        value = environ.get(self.webhook_env, "").strip()
        where = f"[alerts] channel {self.name}: ${self.webhook_env}"
        if not value:
            raise ValueError(f"{where} is not set")
        try:
            return _split(value, self.allow_host)
        except ValueError as exc:
            raise ValueError(f"{where} is not a usable Slack webhook URL: {exc}") from None

    def check(self, environ: Mapping[str, str] = os.environ) -> None:
        self.webhook(environ)

    def build(self, environ: Mapping[str, str] = os.environ) -> SlackChannel:
        return SlackChannel(self, *self.webhook(environ))


def _split(value: str, allow_host: str) -> tuple[str, str]:
    """(origin, path) of a webhook URL; the error never repeats the value."""
    try:
        parts = urlsplit(value)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        raise ValueError("not a valid URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError("expected an https URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("no credentials, query or fragment allowed")
    if not _PATH.fullmatch(parts.path) or "//" in parts.path or ".." in parts.path:
        raise ValueError("unexpected path")
    if host == SLACK_HOST:
        if parts.scheme != "https" or port not in (None, 443):
            raise ValueError("expected https on the default port")
    elif not allow_host or host != allow_host:
        raise ValueError(f"host must be {SLACK_HOST} (or the channel's allow_host)")
    elif parts.scheme == "http" and not loopback(host):
        raise ValueError("plain http is allowed to loopback only")
    return f"{parts.scheme}://{parts.netloc}", parts.path


def load_slack(entry: Mapping[str, Any], where: str) -> SlackSettings:
    if extra := set(entry) - _KEYS:
        raise ValueError(f"{where}: unknown keys {sorted(extra)}")
    webhook_env = entry.get("webhook_env")
    if not isinstance(webhook_env, str) or not _ENV_NAME.fullmatch(webhook_env):
        raise ValueError(f"{where}: webhook_env must be an environment variable name")
    name = entry.get("name", TYPE)
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise ValueError(f"{where}: name must be a short lower-case word")
    try:
        timeout = parse_duration(entry.get("timeout", DEFAULT_TIMEOUT))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{where}: timeout: {exc}") from exc
    if not timedelta(0) < timeout <= MAX_TIMEOUT:
        raise ValueError(f"{where}: timeout must be positive and at most {MAX_TIMEOUT}")
    try:
        level = Severity(entry.get("min_severity", Severity.WARNING.value))
    except ValueError:
        raise ValueError(f"{where}: min_severity must be info, warning or critical") from None
    allow_host = entry.get("allow_host", "")
    if not isinstance(allow_host, str) or (allow_host and not _HOST.fullmatch(allow_host)):
        raise ValueError(f"{where}: allow_host must be a host name (no scheme or port)")
    return SlackSettings(webhook_env, name, timeout, level, allow_host.lower())


class SlackChannel:
    """Posts alerts to a webhook. Holds the URL; its ``repr`` does not show it."""

    def __init__(self, settings: SlackSettings, origin: str, path: str) -> None:
        self.name = settings.name
        self.min_severity = settings.min_severity
        self._settings = settings
        self._origin = origin
        self._path = path  # the secret part of the URL

    def __repr__(self) -> str:
        return f"SlackChannel(name={self.name!r})"

    def send(self, message: AlertMessage) -> None:
        payload = {
            "text": format_message(message),
            "mrkdwn": False,
            "unfurl_links": False,
            "unfurl_media": False,
        }
        # A timeout after the server got the POST is retried, so a message can arrive twice.
        try:
            response = net.post(
                self._origin,
                self._path,
                {"Content-Type": "application/json"},
                json.dumps(payload).encode(),
                self._settings.timeout.total_seconds(),
                max_bytes=MAX_RESPONSE,
                proxy=self._origin.startswith("https:"),
            )
        except net.RequestError as exc:  # its detail never quotes the path
            if exc.cause in ("timeout", "connection"):
                raise TransientError(exc.cause, exc.detail) from None
            raise PermanentError(exc.cause, exc.detail) from None
        self._check(response)

    def _check(self, response: net.Response) -> None:
        status = response.status
        if 200 <= status < 300:
            return
        error = self._error_code(response.body)
        detail = f"HTTP {status}" + (f": {error}" if error else "")
        if status == 429:
            raise TransientError("rate_limited", detail, _seconds(response.headers))
        if status >= 500:
            raise TransientError("server_error", detail)
        raise PermanentError("rejected", detail)

    def _error_code(self, body: bytes) -> str:
        """Slack answers with a short code such as ``invalid_token``; anything else (which
        could echo the URL) is not logged."""
        text = redact(body.decode("utf-8", "replace").strip(), self._path)
        return text if _ERROR.fullmatch(text) else ""


def _seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` in seconds, capped; missing, malformed or negative: None (backoff).
    Delivery does not wait longer than its own cap and fails such an alert as rate_limited."""
    try:
        seconds = float(headers.get("retry-after", ""))
    except ValueError:
        return None
    return min(seconds, MAX_RETRY_AFTER) if seconds >= 0 else None
