"""A narrow HTTP client for integrations that send a secret (a bearer token or an API key).

Shared by the Prometheus reader, the LLM quota connector and the alert channels (``get`` and
``post``; a secret may even be in the path, so no error ever quotes the path).
The rules it enforces:

- Only the configured origin gets the request: redirects are never followed (a 3xx is an
  error). Plain http never goes through a proxy. For https, with ``proxy=True``, an
  ``https_proxy`` from the environment (honouring ``no_proxy``) is used as a CONNECT
  tunnel: the proxy sees the target host and port, never the request or the secret, and
  TLS is verified against the target host. Proxy credentials come only from the proxy URL.
  With ``proxy=False`` (the default) environment proxies are ignored.
- One deadline for the whole request: a watchdog shuts the connection down at the deadline,
  whether it is in the proxy CONNECT exchange, the TLS handshake, the status line, the
  headers or the body, however slowly the other side drips bytes. Opening the TCP
  connection is bounded by the time left, per address tried; DNS resolution is not bounded
  (the stdlib cannot do it cheaply).
- The body is read in small chunks up to a size cap.
- Header names and values are checked before sending (values must be printable ASCII):
  ``http.client`` would put an invalid value (a secret with a line break) into its
  exception message.
- Errors carry a short ``cause`` (a metric label) and a ``detail`` that is safe to log:
  never the headers, the URL's query or the response body.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import logging
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

READ_CHUNK = 4096
DEFAULT_MAX_BYTES = 1024 * 1024

_BAD_HEADER_VALUE = re.compile(r"[^\x20-\x7e]")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_BAD_PATH = re.compile(r"[^\x21-\x7e]|#")
# Headers http.client sets from the URL and the request itself; a caller may not send them.
_RESERVED = frozenset({"host", "content-length", "transfer-encoding", "connection"})


def valid_header_value(value: str) -> bool:
    """A header value (a token, a key) must be non-empty printable ASCII: no line breaks,
    no other control characters, nothing http.client would encode differently."""
    return bool(value) and _BAD_HEADER_VALUE.search(value) is None


def valid_header_name(name: str) -> bool:
    return _HEADER_NAME.fullmatch(name) is not None


class RequestError(Exception):
    """A failed request. ``cause`` is a short category; ``detail`` is safe to log."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Response:
    """``headers`` have lower-case names (the first of repeated headers wins). ``body`` is
    read only for 2xx answers (and for others when asked to); it is empty otherwise."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


class _Guard:
    """Holds a duplicate of every socket a request opens, so the watchdog can shut the
    connection down at any stage (the TLS layer replaces the socket object it wraps)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.expired = False
        self.socks: list[socket.socket] = []

    def track(self, sock: socket.socket) -> None:
        with self.lock:
            if self.expired:
                sock.close()
                raise TimeoutError
            self.socks.append(sock.dup())

    def expire(self) -> None:
        with self.lock:
            self.expired = True
            for sock in self.socks:
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)

    def close(self) -> None:
        with self.lock:
            for sock in self.socks:
                with contextlib.suppress(OSError):
                    sock.close()
            self.socks.clear()


def https_proxy(host: str) -> urllib.parse.SplitResult | None:
    """The https proxy from the environment for ``host``, if any and usable (an http://
    proxy URL with a host); None when ``no_proxy`` covers the host."""
    proxy = urllib.request.getproxies().get("https")
    if not proxy or urllib.request.proxy_bypass(host):
        return None
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    try:
        usable = parts.scheme == "http" and bool(parts.hostname) and parts.port != 0
    except ValueError:
        usable = False
    if not usable:
        # The scheme only: the proxy URL may hold credentials.
        what = parts.scheme if parts.scheme != "http" else "http (no host or port 0)"
        if what not in _warned_proxies:
            _warned_proxies.add(what)
            log.warning(
                "the https_proxy from the environment (%s) is not supported (only http:// "
                "proxies are); connecting directly",
                what,
            )
        return None
    return parts


_warned_proxies: set[str] = set()


def _connection(
    parts: urllib.parse.SplitResult, timeout: float, proxy: bool
) -> http.client.HTTPConnection:
    host = parts.hostname or ""
    if parts.scheme != "https":  # never through a proxy
        return http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
    port = parts.port or 443
    via = https_proxy(host) if proxy else None
    if via is None:
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    conn = http.client.HTTPSConnection(via.hostname or "", via.port or 80, timeout=timeout)
    tunnel_headers = {}
    if via.username:
        pair = f"{urllib.parse.unquote(via.username)}:{urllib.parse.unquote(via.password or '')}"
        tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode(
            "ascii"
        )
    conn.set_tunnel(host, port, headers=tunnel_headers)
    return conn


def get(
    url: str,
    path: str,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    read_body: bool = True,
    proxy: bool = False,
) -> Response:
    """GET ``path`` on the origin of ``url`` (see :func:`request`)."""
    return request("GET", url, path, headers, timeout, max_bytes, read_body, proxy)


def post(
    url: str,
    path: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    proxy: bool = False,
) -> Response:
    """POST ``body`` to ``path`` (see :func:`request`). The body of a non-2xx answer is
    read too (up to ``max_bytes``): APIs say there how long to wait."""
    return request(
        "POST", url, path, headers, timeout, max_bytes, True, proxy, body=body, body_on_error=True
    )


def request(
    method: str,
    url: str,
    path: str,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    read_body: bool = True,
    proxy: bool = False,
    body: bytes | None = None,
    body_on_error: bool = False,
) -> Response:
    """Send ``method`` ``path`` (with its query, if any) on the origin of ``url``; raises
    :class:`RequestError` on redirects, timeouts, connection errors, bodies over
    ``max_bytes`` and header values that are not valid. Other statuses are returned.
    With ``read_body`` false the body is never read (only the status and headers count);
    otherwise it is read for 2xx answers, and for others only with ``body_on_error``.
    ``proxy`` allows an environment https proxy as a CONNECT tunnel (https only)."""
    seen: set[str] = set()
    for name, value in headers.items():
        if not valid_header_name(name) or not valid_header_value(value):
            raise RequestError("invalid_header", f"header {name!r} has an invalid name or value")
        lowered = name.lower()
        if lowered in _RESERVED or lowered in seen:
            raise RequestError("invalid_header", f"header {name!r} is reserved or repeated")
        seen.add(lowered)
    if not path.startswith("/") or path.startswith("//") or _BAD_PATH.search(path):
        raise RequestError("invalid_request", "the path must be absolute, on the same origin")
    parts = urllib.parse.urlsplit(url)
    try:
        parts.port  # noqa: B018 - validates the port
    except ValueError:
        raise RequestError("invalid_request", "invalid port") from None
    deadline = time.monotonic() + timeout
    guard = _Guard()
    conn = _connection(parts, timeout, proxy)
    conn._create_connection = _tracked(guard, deadline)  # type: ignore[attr-defined]
    watchdog = threading.Timer(timeout, guard.expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        conn.request(method, path, body=body, headers=dict(headers))
        response = conn.getresponse()
        status = response.status
        if 300 <= status < 400:
            raise RequestError("redirect", f"HTTP {status} redirect (not followed)")
        names: dict[str, str] = {}
        for name, value in response.getheaders():
            names.setdefault(name.lower(), value)
        wanted = read_body and (status < 300 or body_on_error)
        data = _read_limited(response, deadline, max_bytes) if wanted else b""
        if guard.expired:
            raise TimeoutError
    except RequestError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # ValueError: http.client rejected something it was given; its message may quote it.
        if guard.expired or isinstance(exc, TimeoutError):
            raise RequestError("timeout", "timed out") from None
        raise RequestError("connection", f"connection failed ({type(exc).__name__})") from None
    finally:
        watchdog.cancel()
        conn.close()
        guard.close()
    return Response(status, names, data)


def _tracked(guard: _Guard, deadline: float) -> Callable[..., socket.socket]:
    """``socket.create_connection`` with the time left, registered with the watchdog."""

    def create(address: tuple[str, int], *args: Any, **kwargs: Any) -> socket.socket:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        sock = socket.create_connection(address, left)
        guard.track(sock)
        return sock

    return create


def _read_limited(response: http.client.HTTPResponse, deadline: float, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while not response.isclosed():  # the socket is gone once the last byte is read
        if time.monotonic() >= deadline:
            raise TimeoutError
        chunk = response.read(READ_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise RequestError("too_large", f"response over {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
