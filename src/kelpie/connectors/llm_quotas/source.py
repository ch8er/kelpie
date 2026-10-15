"""Probes of LLM provider APIs: quota left, when it refills, whether the key still works.

Each provider is probed with one GET of a configured ``path`` on its ``api_url``, at most
once per ``interval``; between probes the last status is reused. Two generic shapes:

- ``headers``: the answer's rate-limit headers are read (names configurable; by default
  the IETF ``RateLimit-Remaining``/``-Limit``/``-Reset`` fields). The body is never read.
- ``status_url``: the answer is a JSON object and configured dotted paths point at the
  remaining/used/limit amounts, the reset time and the key's expiry.

Probes must not cost tokens: point ``path`` at an endpoint that lists models, describes the
key or reports usage, never at one that generates text. Kelpie only ever sends a GET without
a body, so it cannot start a generation, but whether a GET is free is the provider's rule.

401/403 mean the key was rejected (``auth_ok`` False). 429 means the limit is reached now: a
:class:`~kelpie.core.model.LimitHit` is recorded (once per limit period) with the reset time
from the headers or ``Retry-After``. So is a probe that reads ``remaining`` 0. Anything else
(unreachable, timeout, 5xx, a redirect, an oversized or malformed answer) gives a status
with ``ok`` False: the quota is unknown, never the last known value. One warning is logged
per provider and cause until the provider answers again; failures are counted in metrics.

The key goes only to ``api_url``'s origin (:mod:`kelpie.net`: no redirects, no proxies,
one deadline). It is never logged and is not part of any repr or error message.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from kelpie import __version__, net
from kelpie.core.model import LimitHit, ProviderStatus

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_INTERVAL = timedelta(minutes=5)
MAX_RESPONSE_BYTES = 256 * 1024
# Limit hits are kept this long (and at most this many per provider) for budget forecasts.
HIT_HISTORY = timedelta(days=8)
MAX_HITS = 500
# Dates further out than this are taken as nonsense (a unit mix-up), not as a reading.
MAX_FUTURE = timedelta(days=400)
FIELDS = ("remaining", "used", "limit", "resets_at", "expires_at")
DEFAULT_HEADER_FIELDS: Mapping[str, str] = {
    "remaining": "ratelimit-remaining",
    "limit": "ratelimit-limit",
    "resets_at": "ratelimit-reset",
}


class ProbeKind(StrEnum):
    HEADERS = "headers"
    STATUS_URL = "status_url"


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """One provider, ready to probe. ``fields`` map reading names (:data:`FIELDS`) to
    header names (``headers``) or dotted JSON paths (``status_url``)."""

    name: str
    kind: ProbeKind
    api_url: str
    path: str
    token: str = field(repr=False)
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"  # "" sends the key as the whole header value
    headers: Mapping[str, str] = field(default_factory=dict)  # static, not secret
    fields: Mapping[str, str] = field(default_factory=dict)
    unit: str = ""
    interval: timedelta = DEFAULT_INTERVAL
    timeout: float = DEFAULT_TIMEOUT

    def request_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": f"kelpie/{__version__}",
            "Accept": "application/json",
            **self.headers,
        }
        headers[self.auth_header] = (
            f"{self.auth_scheme} {self.token}" if self.auth_scheme else self.token
        )
        return headers

    def request_path(self) -> str:
        return urlsplit(self.api_url).path.rstrip("/") + self.path


class ProbeObserver(Protocol):
    def provider_probed(self, status: ProviderStatus) -> None: ...

    def provider_failed(self, provider: str, cause: str) -> None: ...

    def provider_limit_hit(self, provider: str) -> None: ...


HttpGet = Callable[..., net.Response]


class LlmQuotaSource:
    """Implements :class:`kelpie.core.ports.ProviderReader`. Used from one thread."""

    def __init__(
        self,
        providers: Sequence[ProviderConfig],
        observer: ProbeObserver | None = None,
        http_get: HttpGet = net.get,
    ) -> None:
        self.providers = tuple(providers)
        self.observer = observer
        self._get = http_get
        self._status: dict[str, ProviderStatus] = {}
        self._next_at: dict[str, datetime] = {}
        self._hits: dict[str, list[LimitHit]] = {}
        self._limited: dict[str, bool] = {}
        self._warned: dict[str, set[str]] = {}

    def read(self, now: datetime) -> tuple[tuple[ProviderStatus, ...], tuple[LimitHit, ...]]:
        for provider in self.providers:
            due = self._next_at.get(provider.name)
            if due is not None and now < due:
                continue
            self._next_at[provider.name] = now + provider.interval
            try:
                status = self._probe(provider, now)
            except Exception as exc:  # never crash a tick
                status = _failed(provider.name, now, "unexpected", type(exc).__name__)
            self._record(provider, status, now)
        hits: list[LimitHit] = []
        for kept in self._hits.values():
            kept[:] = [h for h in kept if now - h.at <= HIT_HISTORY][-MAX_HITS:]
            hits.extend(kept)
        statuses = tuple(self._status[p.name] for p in self.providers if p.name in self._status)
        return statuses, tuple(sorted(hits, key=lambda h: (h.at, h.limit)))

    # --- one probe ------------------------------------------------------------------

    def _probe(self, provider: ProviderConfig, now: datetime) -> ProviderStatus:
        try:
            response = self._get(
                provider.api_url,
                provider.request_path(),
                provider.request_headers(),
                provider.timeout,
                MAX_RESPONSE_BYTES,
                read_body=provider.kind is ProbeKind.STATUS_URL,
            )
        except net.RequestError as exc:
            return _failed(provider.name, now, exc.cause, exc.detail)
        status = response.status
        if status in (401, 403):
            return ProviderStatus(
                provider.name,
                ok=False,
                auth_ok=False,
                unit=provider.unit,
                checked_at=now,
                error_kind="auth",
            )
        if status == 429:
            reading = _from_headers(provider, response.headers, now)
            resets_at = reading.get("resets_at") or _retry_after(response.headers, now)
            return ProviderStatus(
                provider.name,
                ok=True,
                auth_ok=True,
                remaining=0.0,
                limit=reading.get("limit"),
                unit=provider.unit,
                resets_at=resets_at,
                expires_at=reading.get("expires_at"),
                checked_at=now,
                error_kind="rate_limited",
            )
        if not 200 <= status < 300:
            return _failed(provider.name, now, "http_error", f"HTTP {status}")
        if provider.kind is ProbeKind.HEADERS:
            reading = _from_headers(provider, response.headers, now)
        else:
            try:
                reading = _from_json(provider, response.body, now)
            except ValueError as exc:
                return _failed(provider.name, now, "invalid_response", str(exc))
        remaining, limit = reading.get("remaining"), reading.get("limit")
        used = reading.get("used")
        if remaining is None and used is not None and limit is not None:
            remaining = max(limit - used, 0.0)
        return ProviderStatus(
            provider.name,
            ok=True,
            auth_ok=True,
            remaining=remaining,
            limit=limit,
            unit=provider.unit,
            resets_at=reading.get("resets_at"),
            expires_at=reading.get("expires_at"),
            checked_at=now,
        )

    def _record(self, provider: ProviderConfig, status: ProviderStatus, now: datetime) -> None:
        name = provider.name
        self._status[name] = status
        cause = status.error_kind if not status.ok else None
        warned = self._warned.setdefault(name, set())
        if cause is not None:
            if cause not in warned:
                warned.add(cause)
                log.warning(
                    "LLM provider %s: probe failed (%s)%s; its quota is unknown until it answers",
                    name,
                    cause,
                    " - the key was rejected" if cause == "auth" else "",
                )
            if self.observer is not None:
                self.observer.provider_failed(name, cause)
        elif warned:
            log.info("LLM provider %s answers again", name)
            warned.clear()
        limited = status.ok and status.remaining is not None and status.remaining <= 0
        if limited:
            kept = self._hits.setdefault(name, [])
            last = kept[-1] if kept else None
            new_period = last is None or (last.resets_at is not None and last.resets_at <= now)
            if not self._limited.get(name) or new_period:
                kept.append(LimitHit(name, now, status.resets_at))
                if self.observer is not None:
                    self.observer.provider_limit_hit(name)
            elif last is not None and status.resets_at is not None:
                kept[-1] = replace(last, resets_at=status.resets_at)
        self._limited[name] = limited
        if self.observer is not None:
            self.observer.provider_probed(status)


def _failed(name: str, now: datetime, cause: str, detail: str) -> ProviderStatus:
    log.debug("LLM provider %s: %s (%s)", name, cause, detail)
    return ProviderStatus(name, ok=False, checked_at=now, error_kind=cause)


# --- reading values -----------------------------------------------------------------


def _from_headers(
    provider: ProviderConfig, headers: Mapping[str, str], now: datetime
) -> dict[str, Any]:
    names = provider.fields if provider.kind is ProbeKind.HEADERS else {}
    out: dict[str, Any] = {}
    for key, header in names.items():
        raw = headers.get(header.lower())
        if raw is None:
            continue
        value = _time(raw, now) if key in ("resets_at", "expires_at") else _amount(raw)
        if value is not None:
            out[key] = value
    return out


def _from_json(provider: ProviderConfig, body: bytes, now: datetime) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):  # RecursionError: absurdly nested JSON
        raise ValueError("response is not JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("response is not a JSON object")
    out: dict[str, Any] = {}
    for key, path in provider.fields.items():
        raw = _lookup(payload, path)
        if raw is None or isinstance(raw, bool | dict | list):
            continue
        value = _time(raw, now) if key in ("resets_at", "expires_at") else _amount(raw)
        if value is not None:
            out[key] = value
    return out


def _lookup(payload: Any, path: str) -> Any:
    node = payload
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


def _amount(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw.strip()) if isinstance(raw, str) else float(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


_DURATION = re.compile(
    r"(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?(?:(?P<ms>\d+(?:\.\d+)?)ms)?"
)
_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def _time(raw: Any, now: datetime) -> datetime | None:
    """A reset or expiry time, from: seconds from now (small numbers), Unix seconds or
    milliseconds (large numbers), a duration such as ``1m30s``, ISO 8601, or an HTTP date.
    Values that do not parse, or lie absurdly far ahead, give None."""
    at: datetime | None = None
    seconds = _amount(raw)
    text = raw.strip() if isinstance(raw, str) else ""
    try:
        if seconds is not None:
            if seconds >= 1e12:
                at = datetime.fromtimestamp(seconds / 1000, UTC)
            elif seconds >= 1e9:
                at = datetime.fromtimestamp(seconds, UTC)
            else:
                at = now + timedelta(seconds=seconds)
        elif text and (m := _DURATION.fullmatch(text)) and any(m.groupdict().values()):
            total = sum(float(v) * _SECONDS[k] for k, v in m.groupdict().items() if v)
            at = now + timedelta(seconds=total)
        elif text:
            at = _date(text)
    except (OverflowError, OSError, ValueError):
        return None
    if at is None or at - now > MAX_FUTURE:
        return None
    return at


def _date(text: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            at = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _retry_after(headers: Mapping[str, str], now: datetime) -> datetime | None:
    raw = headers.get("retry-after")
    return None if raw is None else _time(raw, now)
