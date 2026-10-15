"""A narrow HTTP GET for integrations that send a secret (a bearer token or an API key).

Shared by the Prometheus reader and the LLM quota connector. The rules it enforces:

- Only the configured origin is contacted: redirects are never followed (a 3xx is an
  error) and proxies from the environment are never used, so a secret goes nowhere else.
- One deadline for the whole request after the TCP connection is open, however slowly
  the server drips bytes: a watchdog shuts the socket down at the deadline, and every read
  re-applies the time that is left. Connecting is bounded by the timeout per address; DNS
  resolution is not bounded (the stdlib cannot do it cheaply).
- The body is read in small chunks up to a size cap.
- Header values are checked before sending: ``http.client`` would put an invalid value
  (a secret with a line break) into its exception message.
- Errors carry a short ``cause`` (a metric label) and a ``detail`` that is safe to log: never
  the headers, the URL's query or the response body.
"""

from __future__ import annotations

import contextlib
import http.client
import re
import socket
import threading
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field

READ_CHUNK = 4096
DEFAULT_MAX_BYTES = 1024 * 1024

_BAD_HEADER_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


def valid_header_value(value: str) -> bool:
    """A header value (a token, a key) must be non-empty and free of line breaks and other
    control characters."""
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
    read only for 2xx answers; it is empty otherwise."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


def get(
    url: str,
    path: str,
    headers: Mapping[str, str],
    timeout: float,
    max_bytes: int = DEFAULT_MAX_BYTES,
    read_body: bool = True,
) -> Response:
    """GET ``path`` (with its query, if any) on the origin of ``url``; raises
    :class:`RequestError` on redirects, timeouts, connection errors, bodies over
    ``max_bytes`` and header values that are not valid. Other statuses are returned.
    With ``read_body`` false the body is never read (only the status and headers count)."""
    for name, value in headers.items():
        if not valid_header_name(name) or not valid_header_value(value):
            raise RequestError("invalid_header", f"header {name!r} has an invalid name or value")
    parts = urllib.parse.urlsplit(url)
    connection_class = (
        http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    )
    deadline = time.monotonic() + timeout
    expired = threading.Event()
    conn = connection_class(parts.hostname or "", parts.port, timeout=timeout)
    watchdog: threading.Timer | None = None
    try:
        conn.request("GET", path, headers=dict(headers))
        sock = conn.sock  # http.client drops conn.sock once a response will close it
        assert sock is not None
        watchdog = threading.Timer(max(0.0, deadline - time.monotonic()), _expire, (sock, expired))
        watchdog.daemon = True
        watchdog.start()
        _arm(sock, deadline)
        response = conn.getresponse()
        status = response.status
        if 300 <= status < 400:
            raise RequestError("redirect", f"HTTP {status} redirect (not followed)")
        names: dict[str, str] = {}
        for name, value in response.getheaders():
            names.setdefault(name.lower(), value)
        body = (
            _read_limited(response, sock, deadline, max_bytes)
            if read_body and status < 300
            else b""
        )
        if expired.is_set():
            raise TimeoutError
    except RequestError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # ValueError: http.client rejected something it was given; its message may quote it.
        if expired.is_set() or isinstance(exc, TimeoutError):
            raise RequestError("timeout", "timed out") from None
        raise RequestError("connection", f"connection failed ({type(exc).__name__})") from None
    finally:
        if watchdog is not None:
            watchdog.cancel()
        conn.close()
    return Response(status, names, body)


def _expire(sock: socket.socket, expired: threading.Event) -> None:
    """Watchdog: end a request that outlived its deadline by shutting its socket down."""
    expired.set()
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


def _arm(sock: socket.socket, deadline: float) -> None:
    """Give the socket only the time that is left."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    sock.settimeout(remaining)


def _read_limited(
    response: http.client.HTTPResponse, sock: socket.socket, deadline: float, max_bytes: int
) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while not response.isclosed():  # the socket is gone once the last byte is read
        _arm(sock, deadline)
        chunk = response.read(READ_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise RequestError("too_large", f"response over {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
