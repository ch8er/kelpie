"""The Telegram channel: ``sendMessage`` of the Bot API.

The bot token is part of the URL path (``/bot<token>/sendMessage``), so it is handled like
any secret: read from the environment variable named in config, validated at startup (the
error names the variable, never the value), kept out of ``repr``, logs, metrics and errors.
Errors come from :mod:`kelpie.net`, which never quotes the path, and the little of Telegram's
own error text that is logged goes through :func:`redact` first.

Alerts are sent as HTML (``parse_mode=HTML``, laid out by :mod:`kelpie.alerts.render`,
which escapes every dynamic value), or as plain text with ``format = "plain"``. Should
Telegram still refuse the HTML ("can't parse entities"), the message is sent once more as
plain text. Free text (the daily report) is plain text, as it is. Link previews are off.
The text is cut to Telegram's 4096-character limit (counted in UTF-16 units, as Telegram
does).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from kelpie import net
from kelpie.alerts import render
from kelpie.alerts.delivery import PermanentError, TransientError
from kelpie.alerts.render import (
    AT_SIGN,
    DEFAULT_RENDERING,
    MAX_TEXT,
    RenderSettings,
    clean,
    defang,
    utf16_len,
)
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent
from kelpie.core.serde import parse_duration

__all__ = ["AT_SIGN", "MAX_TEXT", "clean", "defang", "utf16_len"]

log = logging.getLogger(__name__)

TYPE = "telegram"
DEFAULT_API_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT = timedelta(seconds=10)
MAX_TIMEOUT = timedelta(minutes=1)
MAX_RESPONSE = 64 * 1024
FORMATS = ("html", "plain")
_TOKEN = re.compile(r"\d{1,20}:[A-Za-z0-9_-]{20,128}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CHAT = re.compile(r"-?\d{1,20}|@[A-Za-z0-9_]{4,64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_KEYS = {"type", "name", "token_env", "chat_id", "timeout", "min_severity", "api_url", "format"}
_BAD_ENTITIES = "can't parse entities"


def redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    """Cut ``text`` to ``limit`` UTF-16 units, ending in an ellipsis when cut."""
    if utf16_len(text) <= limit:
        return text
    tail = "\n... (cut)"
    kept: list[str] = []
    used = utf16_len(tail)
    for char in text:
        used += utf16_len(char)
        if used > limit:
            break
        kept.append(char)
    return "".join(kept) + tail


def format_message(message: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING) -> str:
    """Plain text (:func:`kelpie.alerts.render.plain`); free text (``message.text``) is
    sent as it is."""
    if message.text is not None:
        return truncate(defang(message.text))
    return render.plain(message, settings)


def payload(
    message: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING, fmt: str = "html"
) -> dict[str, Any]:
    """The message fields of ``sendMessage``: ``text`` and, for HTML, ``parse_mode``."""
    if message.text is not None:
        return {"text": truncate(defang(message.text))}
    return render.telegram_payload(message, settings, html_format=fmt == "html")


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    token_env: str
    chat_id: str
    name: str = TYPE
    timeout: timedelta = DEFAULT_TIMEOUT
    min_severity: Severity = Severity.WARNING
    api_url: str = DEFAULT_API_URL
    format: str = "html"  # html | plain

    @property
    def type(self) -> str:
        return TYPE

    def describe(self) -> str:
        return (
            f"telegram (chat {self.chat_id}, from {self.min_severity.value}, "
            f"token from ${self.token_env})"
        )

    def token(self, environ: Mapping[str, str] = os.environ) -> str:
        """The bot token; a ``ValueError`` names the variable, never its value."""
        value = environ.get(self.token_env, "").strip()
        where = f"[alerts] channel {self.name}: ${self.token_env}"
        if not value:
            raise ValueError(f"{where} is not set")
        if not _TOKEN.fullmatch(value):
            raise ValueError(f"{where} does not look like a Telegram bot token (<id>:<secret>)")
        return value

    def check(self, environ: Mapping[str, str] = os.environ) -> None:
        self.token(environ)

    def build(
        self,
        environ: Mapping[str, str] = os.environ,
        rendering: RenderSettings = DEFAULT_RENDERING,
    ) -> TelegramChannel:
        return TelegramChannel(self, self.token(environ), rendering)


def load_telegram(entry: Mapping[str, Any], where: str) -> TelegramSettings:
    if extra := set(entry) - _KEYS:
        raise ValueError(f"{where}: unknown keys {sorted(extra)}")
    token_env = entry.get("token_env")
    if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
        raise ValueError(f"{where}: token_env must be an environment variable name")
    chat = entry.get("chat_id")
    if isinstance(chat, int) and not isinstance(chat, bool):
        chat = str(chat)
    if not isinstance(chat, str) or not _CHAT.fullmatch(chat):
        raise ValueError(f"{where}: chat_id must be a number (e.g. -1001234567890) or @channel")
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
    fmt = entry.get("format", "html")
    if fmt not in FORMATS:
        raise ValueError(f"{where}: format must be one of {list(FORMATS)}")
    return TelegramSettings(
        token_env,
        chat,
        name,
        timeout,
        level,
        _api_url(entry.get("api_url", DEFAULT_API_URL), where),
        fmt,
    )


def _api_url(value: Any, where: str) -> str:
    """The Bot API origin. https, or plain http to loopback only (a local Bot API server)."""
    if not isinstance(value, str):
        raise ValueError(f"{where}: api_url must be a URL")
    try:
        parts = urlsplit(value)
        host, _ = parts.hostname, parts.port
    except ValueError:
        raise ValueError(f"{where}: api_url is not a valid URL") from None
    if (
        parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ValueError(
            f"{where}: api_url must be an origin only (the token comes from token_env)"
        )
    if not host or parts.scheme not in ("http", "https"):
        raise ValueError(f"{where}: api_url must be an http(s) URL")
    if parts.scheme == "http" and not loopback(host):
        raise ValueError(f"{where}: api_url must be https (plain http only to loopback)")
    return f"{parts.scheme}://{parts.netloc}"


def loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class TelegramChannel:
    """Sends alerts with ``sendMessage``. Holds the token; its ``repr`` does not show it."""

    def __init__(
        self, settings: TelegramSettings, token: str, rendering: RenderSettings = DEFAULT_RENDERING
    ) -> None:
        self.name = settings.name
        self.min_severity = settings.min_severity
        self._settings = settings
        self._token = token
        self._rendering = rendering

    def __repr__(self) -> str:
        return f"TelegramChannel(name={self.name!r}, chat_id={self._settings.chat_id!r})"

    def send(self, message: AlertEvent) -> None:
        fields = payload(message, self._rendering, self._settings.format)
        response = self._post(fields, message)
        if "parse_mode" in fields and self._bad_entities(response):
            # Telegram refused the markup: the same alert once more, as plain text. Should
            # that resend hit a transient error, delivery retries the whole send, HTML first
            # again: two requests per attempt, and the HTML one is refused again (no message
            # arrives twice: a refused message is not shown).
            log.warning(
                "alert channel %s: Telegram could not parse the HTML; sent as plain text",
                self.name,
            )
            response = self._post(payload(message, self._rendering, "plain"), message)
        self._check(response)

    def _post(self, fields: Mapping[str, Any], message: AlertEvent) -> net.Response:
        body = {
            "chat_id": self._settings.chat_id,
            **fields,
            "disable_web_page_preview": True,
            "disable_notification": message.severity is Severity.INFO,
        }
        # A timeout after the server got the POST is retried, so a message can arrive twice.
        try:
            return net.post(
                self._settings.api_url,
                f"/bot{self._token}/sendMessage",
                {"Content-Type": "application/json"},
                json.dumps(body).encode(),
                self._settings.timeout.total_seconds(),
                max_bytes=MAX_RESPONSE,
                proxy=True,
            )
        except net.RequestError as exc:  # its detail never quotes the path
            if exc.cause in ("timeout", "connection"):
                raise TransientError(exc.cause, exc.detail) from None
            raise PermanentError(exc.cause, exc.detail) from None

    def _bad_entities(self, response: net.Response) -> bool:
        return (
            response.status == 400 and _BAD_ENTITIES in self._error_info(response.body)[0].lower()
        )

    def _check(self, response: net.Response) -> None:
        status = response.status
        if 200 <= status < 300:
            return
        info = self._error_info(response.body)
        detail = f"HTTP {status}" + (f": {info[0]}" if info[0] else "")
        if status == 429:
            retry = info[1]
            if retry is None:
                retry = _seconds(response.headers.get("retry-after"))
            raise TransientError("rate_limited", detail, retry)
        if status >= 500:
            raise TransientError("server_error", detail)
        raise PermanentError("rejected", detail)

    def _error_info(self, body: bytes) -> tuple[str, float | None]:
        """Telegram's error description (redacted, short) and ``retry_after``."""
        try:
            data = json.loads(body)
            description = data.get("description", "")
            retry = (data.get("parameters") or {}).get("retry_after")
        except (ValueError, AttributeError):
            return "", None
        text = redact(description, self._token) if isinstance(description, str) else ""
        text = _CONTROL.sub(" ", text)[:120]
        return text, _seconds(retry)


def _seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if 0 <= seconds < 86400 else None
