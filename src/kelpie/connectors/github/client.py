"""Read-only GitHub REST client: one host, one token, no redirects, rate-limit aware.

Security:

- The token comes from the caller (an env var named in the config) and is kept out of
  ``repr``, log lines and exception messages. Errors carry a short kind and the HTTP status,
  never header values, and are raised ``from None`` so no urllib internals ride along.
- Requests go only to the configured API origin: every URL is the API base plus a path the
  client checks (no ``..``, no ``//``, no ``@``, no scheme), and the resulting origin must
  equal the base origin before the ``Authorization`` header is added.
- Redirects are never followed: a 3xx would carry the credentials to whatever host the
  ``Location`` names. They count as errors.
- A plain-http API URL is only accepted on a loopback address (tests, a local mirror), and
  then no proxy is used, so the token never crosses the network in clear.

Load: responses are cached for ``cache_ttl``, so one tick asks GitHub once per PR however
many blocks name it; after that the cached ETag makes a conditional request (a 304 does not
count against the GitHub rate limit). Error answers (404, 401, 403, other 4xx) are
remembered for the same time. ``X-RateLimit-Remaining: 0`` and a 403/429 with
``Retry-After`` start a back-off until the reset; 401, 403 (at least a minute), network
errors and 5xx a shorter one. :meth:`GitHubClient.begin_tick` caps the requests and the
wall-clock time of one tick, and every request has a deadline for the whole exchange, not
just for each socket read.
During a back-off nothing is sent: callers get the last cached answer, with the time it was
fetched, or a :class:`GitHubError`.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from kelpie import __version__

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_TIMEOUT = timedelta(seconds=10)
DEFAULT_CACHE_TTL = timedelta(seconds=60)
ERROR_BACKOFF = timedelta(seconds=60)  # after a network error, a 5xx or a bare 429
MAX_BACKOFF = timedelta(hours=1)  # a reset or Retry-After further out is capped
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_CACHE_ENTRIES = 1024
MIN_CACHE_TTL = timedelta(seconds=30)
API_VERSION = "2022-11-28"

# A token goes into a header: printable ASCII without spaces, so it can neither split the
# request (CR/LF) nor make http.client echo it in an error message.
_TOKEN = re.compile(r"[\x21-\x7e]{1,1024}")
_PATH = re.compile(r"/[A-Za-z0-9._\-/]*")

SECONDARY_WAIT = timedelta(seconds=60)  # a 403 without Retry-After: GitHub asks for >= 1 min
READ_CHUNK = 64 * 1024

# Errors after which a cached answer, if any, is still worth more than nothing. The others
# (404, 401, 403, redirects, other 4xx) are answers too: they are cached for ``cache_ttl``.
_TRANSIENT = frozenset(
    {"backoff", "budget", "rate_limited", "network", "server", "bad_response", "invalid_request"}
)


@dataclass(frozen=True, slots=True)
class _Failure:
    """The cached body of a request GitHub answered with an error."""

    kind: str
    message: str
    status: int | None


def valid_token(token: str) -> bool:
    return _TOKEN.fullmatch(token) is not None


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_api_url(url: str) -> str:
    """Validate the API base URL and return it without a trailing slash.

    https, or http on a loopback address; no credentials, query or fragment.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("api_url must be a non-empty string")
    parts = urllib.parse.urlsplit(url.strip())
    if parts.scheme not in {"https", "http"} or not parts.hostname:
        raise ValueError("api_url must be an http(s) URL with a host")
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise ValueError("api_url must use https (plain http only on a loopback address)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("api_url must not contain credentials, a query or a fragment")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class GitHubObserver(Protocol):
    def github_request(self, outcome: str) -> None: ...

    def github_rate_limit(self, remaining: int, limit: int) -> None: ...


class NullObserver:
    def github_request(self, outcome: str) -> None:
        pass

    def github_rate_limit(self, remaining: int, limit: int) -> None:
        pass


class GitHubError(Exception):
    """A request that gave no usable answer. ``kind`` is a metric label; the message never
    holds header values."""

    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True, slots=True)
class Response:
    body: Any  # decoded JSON
    fetched_at: datetime  # when GitHub last confirmed this answer
    stale: bool = False  # served from the cache because GitHub could not be asked


@dataclass(slots=True)
class _Entry:
    body: Any
    etag: str | None
    fetched_at: datetime


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # urllib then raises HTTPError with the 3xx code


def _utc_now() -> datetime:
    return datetime.now(UTC)


class GitHubClient:
    """GET JSON from the GitHub REST API. Not thread-safe: the control loop owns it."""

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        token: str | None = None,
        *,
        timeout: timedelta = DEFAULT_TIMEOUT,
        cache_ttl: timedelta = DEFAULT_CACHE_TTL,
        clock: Callable[[], datetime] = _utc_now,
        observer: GitHubObserver | None = None,
        error_backoff: timedelta = ERROR_BACKOFF,
        max_cache_entries: int = MAX_CACHE_ENTRIES,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_url = check_api_url(api_url)
        if token is not None and not valid_token(token):
            raise ValueError("GitHub token contains characters not allowed in a header")
        self.__token = token
        base = urllib.parse.urlsplit(self.api_url)
        self._origin = (base.scheme, base.netloc.lower())
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.clock = clock
        self.observer: GitHubObserver = observer or NullObserver()
        self.error_backoff = error_backoff
        self.max_cache_entries = self._base_cache_entries = max_cache_entries
        self.monotonic = monotonic
        self._requests_left: int | None = None  # per-tick budget, see begin_tick
        self._tick_deadline: float | None = None
        self.backoff_until: datetime | None = None
        self.rate_remaining: int | None = None
        self._cache: OrderedDict[str, _Entry] = OrderedDict()
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if base.scheme == "http":
            handlers.append(urllib.request.ProxyHandler({}))  # loopback: never via a proxy
        self._opener = urllib.request.build_opener(*handlers)

    def __repr__(self) -> str:
        auth = "token" if self.__token else "anonymous"
        return f"GitHubClient(api_url={self.api_url!r}, auth={auth})"

    @property
    def authenticated(self) -> bool:
        return self.__token is not None

    def begin_tick(self, max_requests: int, budget: timedelta, min_cache_entries: int = 0) -> None:
        """Start a tick: at most ``max_requests`` requests and ``budget`` of wall-clock time
        from now; beyond them callers get cached answers or a ``budget`` error. The cache
        grows to ``min_cache_entries`` so that nothing fetched this tick is evicted."""
        self._requests_left = max_requests
        self._tick_deadline = self.monotonic() + budget.total_seconds()
        self.max_cache_entries = max(self._base_cache_entries, min_cache_entries)

    def get(self, path: str, query: Mapping[str, str] | None = None) -> Response:
        """GET ``path`` (relative to the API base). Raises :class:`GitHubError`."""
        url = self._url(path, query)
        now = self.clock()
        entry = self._cache.get(url)
        if entry is not None and now - entry.fetched_at < self.cache_ttl:
            self.observer.github_request("cached")
            if isinstance(entry.body, _Failure):  # errors are answers too: asked once per TTL
                failure = entry.body
                raise GitHubError(failure.kind, f"{failure.message} (cached)", failure.status)
            return Response(entry.body, entry.fetched_at)
        if entry is not None and isinstance(entry.body, _Failure):
            entry = None
        try:
            if self.backoff_until is not None and now < self.backoff_until:
                raise GitHubError("backoff", f"GET {path}: backing off after errors")
            deadline = self._spend(path)
            return self._fetch(url, path, entry, now, deadline)
        except GitHubError as exc:
            self.observer.github_request(exc.kind)
            if exc.kind not in _TRANSIENT:
                self._store(url, _Entry(_Failure(exc.kind, str(exc), exc.status), None, now))
            if entry is not None and exc.kind in _TRANSIENT:
                return Response(entry.body, entry.fetched_at, stale=True)
            raise

    def _spend(self, path: str) -> float:
        """Take one request from the tick's budget; the deadline for the whole request."""
        start = self.monotonic()
        deadline = start + self.timeout.total_seconds()
        if self._tick_deadline is not None:
            if start >= self._tick_deadline:
                raise GitHubError("budget", f"GET {path}: tick time budget spent")
            deadline = min(deadline, self._tick_deadline)
        if self._requests_left is not None:
            if self._requests_left <= 0:
                raise GitHubError("budget", f"GET {path}: tick request budget spent")
            self._requests_left -= 1
        return deadline

    def _url(self, path: str, query: Mapping[str, str] | None) -> str:
        if not _PATH.fullmatch(path) or "//" in path or "/../" in f"{path}/":
            raise GitHubError("invalid_request", "invalid API path")
        url = self.api_url + path
        if query:
            url += "?" + urllib.parse.urlencode(sorted(query.items()))
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme, parts.netloc.lower()) != self._origin:  # defence in depth
            raise GitHubError("invalid_request", "URL outside the API origin")
        return url

    def _request(self, url: str, etag: str | None) -> urllib.request.Request:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", f"kelpie/{__version__}")
        if self.__token is not None:
            request.add_header("Authorization", f"Bearer {self.__token}")
        if etag is not None and valid_token(etag):
            request.add_header("If-None-Match", etag)
        return request

    def _fetch(
        self, url: str, path: str, entry: _Entry | None, now: datetime, deadline: float
    ) -> Response:
        request = self._request(url, entry.etag if entry else None)
        timeout = max(0.001, deadline - self.monotonic())
        try:
            with self._opener.open(request, timeout=timeout) as response:
                self._note_rate_limit(response.headers, now)
                raw = self._read(response, deadline)
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            self._note_rate_limit(exc.headers, now)
            if exc.code == 304 and entry is not None:
                entry.fetched_at = now
                self._cache.move_to_end(url)
                self.observer.github_request("not_modified")
                return Response(entry.body, now)
            raise self._http_error(exc, path, now) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            self._back_off(now + self.error_backoff, now)
            raise GitHubError(
                "network", f"GET {path}: connection failed ({type(reason).__name__})"
            ) from None
        if len(raw) > MAX_BODY_BYTES:
            raise GitHubError("bad_response", f"GET {path}: response too large")
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):  # not JSON, or nested too deep to decode
            raise GitHubError("bad_response", f"GET {path}: response is not JSON") from None
        self._store(url, _Entry(body, etag, now))
        self.observer.github_request("ok")
        return Response(body, now)

    def _read(self, response: Any, deadline: float) -> bytes:
        """Read the body in chunks; the socket timeout bounds each read, this the whole."""
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_BODY_BYTES:
            if self.monotonic() > deadline:
                raise TimeoutError("request deadline passed")
            chunk = response.read(min(READ_CHUNK, MAX_BODY_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)

    def _store(self, url: str, entry: _Entry) -> None:
        self._cache[url] = entry
        self._cache.move_to_end(url)
        while len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)

    def _http_error(self, exc: urllib.error.HTTPError, path: str, now: datetime) -> GitHubError:
        code = exc.code
        where = f"GET {path}: HTTP {code}"
        if 300 <= code < 400:
            return GitHubError("redirect", f"{where} redirect (not followed)", code)
        if code == 401:  # a revoked token fails every request: stop asking for a while
            self._back_off(now + self.error_backoff, now)
            return GitHubError("unauthorized", f"{where} (token missing, invalid or expired)", code)
        if code == 404:
            return GitHubError("not_found", f"{where} (no such object, or no access)", code)
        retry_after = _seconds(exc.headers.get("Retry-After") if exc.headers else None)
        if code == 429 or (code == 403 and (retry_after is not None or self.rate_remaining == 0)):
            if retry_after is not None:
                self._back_off(now + timedelta(seconds=retry_after), now)
            elif self.rate_remaining != 0:
                self._back_off(now + self.error_backoff, now)
            return GitHubError("rate_limited", f"{where} (rate limited)", code)
        if code == 403:
            # No access, or a secondary rate limit without Retry-After (GitHub then asks to
            # wait at least a minute): either way, asking again right away will not help.
            self._back_off(now + max(self.error_backoff, SECONDARY_WAIT), now)
            return GitHubError("forbidden", f"{where} (no access, or a secondary rate limit)", code)
        if code >= 500:
            self._back_off(now + self.error_backoff, now)
            return GitHubError("server", where, code)
        return GitHubError("http_error", where, code)

    def _note_rate_limit(self, headers: Any, now: datetime) -> None:
        if headers is None:
            return
        remaining = _int(headers.get("X-RateLimit-Remaining"))
        if remaining is None:
            return
        self.rate_remaining = remaining
        limit = _int(headers.get("X-RateLimit-Limit"))
        self.observer.github_rate_limit(remaining, limit if limit is not None else -1)
        if remaining <= 0:
            reset = _int(headers.get("X-RateLimit-Reset"))
            until = (
                datetime.fromtimestamp(reset, UTC)
                if reset is not None
                else now + self.error_backoff
            )
            self._back_off(max(until, now + timedelta(seconds=1)), now)

    def _back_off(self, until: datetime, now: datetime) -> None:
        until = min(until, now + MAX_BACKOFF)
        if self.backoff_until is None or until > self.backoff_until:
            self.backoff_until = until


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _seconds(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # an HTTP date: fall back to the rate-limit reset or our own back-off
