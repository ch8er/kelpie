"""The Telegram channel: ``sendMessage`` of the Bot API.

The bot token is part of the URL path (``/bot<token>/sendMessage``), so it is handled like
any secret: read from the environment variable named in config, validated at startup (the
error names the variable, never the value), kept out of ``repr``, logs, metrics and errors.
Errors come from :mod:`kelpie.net`, which never quotes the path, and the little of Telegram's
own error text that is logged goes through :func:`redact` first.

Messages are plain text (no ``parse_mode``): task text is untrusted and plain text cannot be
turned into markup. Link previews are off. The text is cut to Telegram's 4096-character
limit (counted in UTF-16 units, as Telegram does).
"""

from __future__ import annotations

import ipaddress
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
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertMessage, Did, Transition
from kelpie.core.serde import parse_duration

TYPE = "telegram"
DEFAULT_API_URL = "https://api.telegram.org"
DEFAULT_TIMEOUT = timedelta(seconds=10)
MAX_TIMEOUT = timedelta(minutes=1)
MAX_TEXT = 4096
MAX_BODY = 3500  # leaves room for the heading and the "Kelpie did" line
MAX_HEAD = 200
MAX_RESPONSE = 64 * 1024
_TOKEN = re.compile(r"\d{1,20}:[A-Za-z0-9_-]{20,128}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CHAT = re.compile(r"-?\d{1,20}|@[A-Za-z0-9_]{4,64}")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_KEYS = {"type", "name", "token_env", "chat_id", "timeout", "min_severity", "api_url"}

TRANSITION = {
    Transition.NEW: "",
    Transition.ESCALATED: "Still open. ",
    Transition.CHANGED: "Changed. ",
    Transition.RESOLVED: "Resolved. ",
}
DID = {
    Did.NOTIFIED: "Kelpie did: only sent this alert (no other change).",
    Did.SUGGESTED: "Kelpie did: nothing, mode suggest. A human decides.",
    Did.FAILED: "Kelpie did: tried, and it failed. Needs a human.",
    Did.NOT_EXECUTED: "Kelpie did: nothing, no executor could carry it out. Needs a human.",
}


def redact(text: str, token: str) -> str:
    return text.replace(token, "<redacted>") if token else text


def clean(text: str) -> str:
    """No lone surrogates (they cannot be encoded) and no control characters."""
    return _CONTROL.sub(" ", text.encode("utf-8", "replace").decode("utf-8"))


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


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


def format_message(message: AlertMessage) -> str:
    """Plain text: severity and subject, the transition, the reason, what Kelpie did.
    Free text (``message.text``) is sent as it is."""
    if message.text is not None:
        return truncate(clean(message.text))
    head = f"[{message.severity.value.upper()}] "
    head += f"{truncate(clean(message.rule), MAX_HEAD)}: " if message.rule else ""
    # Each part has its own cap, so the last line (what Kelpie did) always survives.
    body = truncate(clean(TRANSITION[message.transition] + message.body.strip()), MAX_BODY)
    lines = [head + truncate(clean(message.subject), MAX_HEAD), body, DID[message.did]]
    return truncate("\n".join(lines))


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    token_env: str
    chat_id: str
    name: str = TYPE
    timeout: timedelta = DEFAULT_TIMEOUT
    min_severity: Severity = Severity.WARNING
    api_url: str = DEFAULT_API_URL

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

    def build(self, environ: Mapping[str, str] = os.environ) -> TelegramChannel:
        return TelegramChannel(self, self.token(environ))


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
    return TelegramSettings(
        token_env,
        chat,
        name,
        timeout,
        level,
        _api_url(entry.get("api_url", DEFAULT_API_URL), where),
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

    def __init__(self, settings: TelegramSettings, token: str) -> None:
        self.name = settings.name
        self.min_severity = settings.min_severity
        self._settings = settings
        self._token = token

    def __repr__(self) -> str:
        return f"TelegramChannel(name={self.name!r}, chat_id={self._settings.chat_id!r})"

    def send(self, message: AlertMessage) -> None:
        payload = {
            "chat_id": self._settings.chat_id,
            "text": format_message(message),
            "disable_web_page_preview": True,
            "disable_notification": message.severity is Severity.INFO,
        }
        # A timeout after the server got the POST is retried, so a message can arrive twice.
        try:
            response = net.post(
                self._settings.api_url,
                f"/bot{self._token}/sendMessage",
                {"Content-Type": "application/json"},
                json.dumps(payload).encode(),
                self._settings.timeout.total_seconds(),
                max_bytes=MAX_RESPONSE,
                proxy=True,
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
