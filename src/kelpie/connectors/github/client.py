"""GitHub REST client: one host, one token, no redirects, rate-limit aware.

Reads are GETs. The only write is :meth:`GitHubClient.post`, used by the CI connector to
cancel a superseded workflow run; it is never cached, never retried and never sent during
a back-off.

Security:

- The token comes from the caller (an env var named in the config) and is kept out of
  ``repr``, log lines and exception messages. Errors carry a short kind and the HTTP status,
  never header values, and are raised ``from None`` so no library internals ride along.
- Requests go only to the configured API origin: every URL is the API base plus a path the
  client checks (no ``..``, no ``//``, no ``@``, no scheme), and the resulting origin must
  equal the base origin before the ``Authorization`` header is added.
- Redirects are never followed (``http.client`` does not follow them): a 3xx would carry
  the credentials to whatever host the ``Location`` names. They count as errors.
- A plain-http API URL is only accepted on a loopback address (tests, a local mirror), and
  then no proxy is used, so the token never crosses the network in clear. For https, an
  ``https_proxy`` from the environment is used as a CONNECT tunnel: the proxy sees the host
  name, never the request or the token.

Time: ``timeout`` bounds the whole request. The connection is opened with the time that is
left, a watchdog shuts the socket down at the deadline (however slowly the server drips
header or body bytes), and every read gets only the time that is left. DNS resolution is
not bounded (the stdlib cannot do it cheaply).

Load: answers are cached for ``cache_ttl``, so one tick asks GitHub once per PR however
many blocks name it; after that the cached ETag makes a conditional request (a 304 does not
count against the GitHub rate limit). Error answers (404, 401, 403, other 4xx) are
remembered for the same time. ``X-RateLimit-Remaining: 0``, a 403/429 with ``Retry-After``
and a 403 for a secondary rate limit start a back-off for every request (until the reset,
at least a minute for a secondary limit); so do 401, network errors and 5xx, shorter. A
403 for missing access is cached for its URL only. :meth:`GitHubClient.begin_tick` caps the
requests and the wall-clock time of one tick. During a back-off or past the budget nothing
is sent: callers get the last cached answer, with the time it was fetched, or a
:class:`GitHubError`.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import ipaddress
import json
import re
import socket
import threading
import time
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
ERROR_BACKOFF = timedelta(seconds=60)  # after a 401, a network error, a 5xx or a bare 429
SECONDARY_WAIT = timedelta(seconds=60)  # a secondary rate limit: GitHub asks for >= 1 min
MAX_BACKOFF = timedelta(hours=1)  # a reset or Retry-After further out is capped
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_CACHE_ENTRIES = 1024
MIN_CACHE_TTL = timedelta(seconds=30)
API_VERSION = "2022-11-28"
READ_CHUNK = 16 * 1024

# A token goes into a header: printable ASCII without spaces, so it can neither split the
# request (CR/LF) nor make http.client echo it in an error message.
_TOKEN = re.compile(r"[\x21-\x7e]{1,1024}")
_PATH = re.compile(r"/[A-Za-z0-9._\-/]*")
_SECONDARY = re.compile(rb"secondary rate limit|abuse detection", re.IGNORECASE)

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
    _ = parts.port  # raises ValueError for a bad port
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


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    headers: http.client.HTTPMessage
    body: bytes


class _Timeout(Exception):
    """The whole-request deadline passed."""


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
        self._host = base.hostname or ""
        self._port = base.port
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.clock = clock
        self.observer: GitHubObserver = observer or NullObserver()
        self.error_backoff = error_backoff
        self.max_cache_entries = self._base_cache_entries = max_cache_entries
        self.monotonic = monotonic
        self._requests_left: int | None = None  # per-tick budget, see begin_tick
        self._tick_deadline: float | None = None
        self.budget_exhausted = False  # this tick asked for more than its budget
        self.backoff_until: datetime | None = None
        self.rate_remaining: int | None = None
        self._cache: OrderedDict[str, _Entry] = OrderedDict()

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
        self.budget_exhausted = False

    def cache_age(self, path: str, query: Mapping[str, str] | None = None) -> timedelta | None:
        """How old the cached answer for ``path`` is; None if there is none."""
        try:
            entry = self._cache.get(self._url(path, query))
        except GitHubError:
            return None
        return None if entry is None else self.clock() - entry.fetched_at

    def get(
        self, path: str, query: Mapping[str, str] | None = None, *, fresh: bool = False
    ) -> Response:
        """GET ``path`` (relative to the API base). Raises :class:`GitHubError`.

        ``fresh`` skips the cache TTL and the tick budget, for a re-check right before a
        write: GitHub is asked again (conditionally, with the cached ETag). If it cannot be
        asked, the answer is the cached one marked ``stale`` or an error, as for any read."""
        url = self._url(path, query)
        now = self.clock()
        entry = self._cache.get(url)
        if not fresh and entry is not None and now - entry.fetched_at < self.cache_ttl:
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
            if fresh:  # a re-check before a write: outside the tick budget
                deadline = self.monotonic() + self.timeout.total_seconds()
            else:
                deadline = self._spend(path)
            return self._fetch(url, path, entry, now, deadline)
        except GitHubError as exc:
            self.observer.github_request(exc.kind)
            if exc.kind not in _TRANSIENT:
                self._store(url, _Entry(_Failure(exc.kind, str(exc), exc.status), None, now))
            if entry is not None and exc.kind in _TRANSIENT:
                return Response(entry.body, entry.fetched_at, stale=True)
            raise

    def post(self, path: str) -> int:
        """POST ``path`` with an empty body; the HTTP status of a 2xx or 409 answer.

        Not cached and not part of the tick budget (the engine's action ledger limits how
        often an action is carried out). Refused during a back-off. Other answers raise
        :class:`GitHubError`, as for reads; a redirect is never followed."""
        url = self._url(path, None)
        now = self.clock()
        if self.backoff_until is not None and now < self.backoff_until:
            self.observer.github_request("backoff")
            raise GitHubError("backoff", f"POST {path}: backing off after errors")
        deadline = self.monotonic() + self.timeout.total_seconds()
        try:
            reply = self._exchange(url, None, deadline, method="POST")
        except _TooLarge:  # pragma: no cover - error bodies are truncated, not refused
            raise GitHubError("bad_response", f"POST {path}: response too large") from None
        except _Timeout:
            self._back_off(now + self.error_backoff, now)
            self.observer.github_request("network")
            raise GitHubError("network", f"POST {path}: timed out") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self._back_off(now + self.error_backoff, now)
            self.observer.github_request("network")
            raise GitHubError(
                "network", f"POST {path}: connection failed ({type(exc).__name__})"
            ) from None
        self._note_rate_limit(reply.headers, now)
        if 200 <= reply.status < 300 or reply.status == 409:
            self.observer.github_request("ok")
            return reply.status
        error = self._http_error(reply, f"POST {path}", now)
        self.observer.github_request(error.kind)
        raise error

    def _spend(self, path: str) -> float:
        """Take one request from the tick's budget; the deadline for the whole request."""
        start = self.monotonic()
        deadline = start + self.timeout.total_seconds()
        if self._tick_deadline is not None:
            if start >= self._tick_deadline:
                self.budget_exhausted = True
                raise GitHubError("budget", f"GET {path}: tick time budget spent")
            deadline = min(deadline, self._tick_deadline)
        if self._requests_left is not None:
            if self._requests_left <= 0:
                self.budget_exhausted = True
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

    def _headers(self, etag: str | None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": f"kelpie/{__version__}",
        }
        if self.__token is not None:
            headers["Authorization"] = f"Bearer {self.__token}"
        if etag is not None and valid_token(etag):
            headers["If-None-Match"] = etag
        return headers

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self._origin[0] == "http":  # loopback only (check_api_url): never via a proxy
            return http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        proxy = _https_proxy(self._host)
        if proxy is None:
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout)
        conn = http.client.HTTPSConnection(proxy.hostname or "", proxy.port or 80, timeout=timeout)
        tunnel_headers = {}
        if proxy.username:
            pair = f"{urllib.parse.unquote(proxy.username)}:"
            pair += urllib.parse.unquote(proxy.password or "")
            tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(
                pair.encode()
            ).decode("ascii")
        conn.set_tunnel(self._host, self._port, headers=tunnel_headers)
        return conn

    def _exchange(self, url: str, etag: str | None, deadline: float, method: str = "GET") -> _Reply:
        """One request with a deadline for all of it: connect, headers and body."""
        target = urllib.parse.urlsplit(url)
        path = target.path + (f"?{target.query}" if target.query else "")
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise _Timeout
        conn = self._connection(remaining)
        expired = threading.Event()
        held: list[socket.socket] = []
        watchdog = threading.Timer(remaining, _expire, (conn, held, expired))
        watchdog.daemon = True
        watchdog.start()
        try:
            headers = self._headers(etag)
            if method != "GET":
                headers["Content-Length"] = "0"
            conn.request(method, path, headers=headers)
            sock = conn.sock  # http.client drops conn.sock once a response will close it
            if sock is not None:
                held.append(sock)
            self._arm(held, deadline)
            response = conn.getresponse()
            limit = MAX_BODY_BYTES if response.status in (200, 304) else MAX_ERROR_BODY_BYTES
            body = self._read(response, held, deadline, limit)
            if expired.is_set():
                raise _Timeout
            return _Reply(response.status, response.headers, body)
        except (OSError, http.client.HTTPException):
            if expired.is_set():
                raise _Timeout from None
            raise
        finally:
            watchdog.cancel()
            conn.close()

    def _arm(self, held: list[socket.socket], deadline: float) -> None:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise _Timeout
        for sock in held:
            sock.settimeout(remaining)

    def _read(
        self,
        response: http.client.HTTPResponse,
        held: list[socket.socket],
        deadline: float,
        limit: int,
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while not response.isclosed():  # the socket is gone once the last byte is read
            self._arm(held, deadline)
            chunk = response.read1(READ_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                if limit == MAX_ERROR_BODY_BYTES:
                    break  # an error page: its start is enough
                raise _TooLarge
            chunks.append(chunk)
        return b"".join(chunks)

    def _fetch(
        self, url: str, path: str, entry: _Entry | None, now: datetime, deadline: float
    ) -> Response:
        try:
            reply = self._exchange(url, entry.etag if entry else None, deadline)
        except _TooLarge:
            raise GitHubError("bad_response", f"GET {path}: response too large") from None
        except _Timeout:
            self._back_off(now + self.error_backoff, now)
            raise GitHubError("network", f"GET {path}: timed out") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self._back_off(now + self.error_backoff, now)
            raise GitHubError(
                "network", f"GET {path}: connection failed ({type(exc).__name__})"
            ) from None
        self._note_rate_limit(reply.headers, now)
        if reply.status == 304 and entry is not None:
            entry.fetched_at = now
            self._cache.move_to_end(url)
            self.observer.github_request("not_modified")
            return Response(entry.body, now)
        if reply.status != 200:
            raise self._http_error(reply, f"GET {path}", now)
        try:
            body = json.loads(reply.body)
        except (ValueError, RecursionError):  # not JSON, or nested too deep to decode
            raise GitHubError("bad_response", f"GET {path}: response is not JSON") from None
        self._store(url, _Entry(body, reply.headers.get("ETag"), now))
        self.observer.github_request("ok")
        return Response(body, now)

    def _store(self, url: str, entry: _Entry) -> None:
        self._cache[url] = entry
        self._cache.move_to_end(url)
        while len(self._cache) > self.max_cache_entries:
            self._cache.popitem(last=False)

    def _http_error(self, reply: _Reply, request: str, now: datetime) -> GitHubError:
        code = reply.status
        where = f"{request}: HTTP {code}"
        if 300 <= code < 400:
            return GitHubError("redirect", f"{where} redirect (not followed)", code)
        if code == 401:  # a revoked token fails every request: stop asking for a while
            self._back_off(now + self.error_backoff, now)
            return GitHubError("unauthorized", f"{where} (token missing, invalid or expired)", code)
        if code == 404:
            return GitHubError("not_found", f"{where} (no such object, or no access)", code)
        retry_after = _seconds(reply.headers.get("Retry-After"))
        secondary = code == 403 and _SECONDARY.search(reply.body) is not None
        if code == 429 or (
            code == 403 and (retry_after is not None or self.rate_remaining == 0 or secondary)
        ):
            if retry_after is not None:
                self._back_off(now + timedelta(seconds=retry_after), now)
            elif secondary:
                self._back_off(now + max(self.error_backoff, SECONDARY_WAIT), now)
            elif self.rate_remaining != 0:
                self._back_off(now + self.error_backoff, now)
            return GitHubError("rate_limited", f"{where} (rate limited)", code)
        if code == 403:  # missing access to this resource: cached for this URL only
            return GitHubError("forbidden", f"{where} (no access)", code)
        if code >= 500:
            self._back_off(now + self.error_backoff, now)
            return GitHubError("server", where, code)
        return GitHubError("http_error", where, code)

    def _note_rate_limit(self, headers: Any, now: datetime) -> None:
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


class _TooLarge(Exception):
    pass


def _expire(
    conn: http.client.HTTPConnection, held: list[socket.socket], expired: threading.Event
) -> None:
    """Watchdog: end a request that outlived its deadline by shutting its socket down."""
    expired.set()
    for sock in [*held, conn.sock]:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


def _https_proxy(host: str) -> urllib.parse.SplitResult | None:
    """The https proxy from the environment for ``host``, if any and usable."""
    proxy = urllib.request.getproxies().get("https")
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    try:
        usable = parts.scheme == "http" and parts.hostname is not None and parts.port != 0
    except ValueError:
        usable = False
    return parts if usable else None


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
