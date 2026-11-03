"""Connection guards shared by every listener of ``kelpie serve``: the main port (OTLP
ingest, ``/metrics``, ``/healthz``) and the hub (``[hub] listen``).

A connection is *pending* from accept until it is admitted: the hub admits it once its
agent token checks out, the main port once an OTLP export's token checks out (before its
body is read). A ``GET`` (``/metrics``, ``/healthz``) is never admitted: its reply must be
written within the header deadline too, so a client that asks and never reads cannot keep
its slot. While pending:

- it has ``header_timeout`` in all (TLS handshake, request line and headers included,
  however slowly they come); then its socket is shut down;
- at most ``max_connections`` connections are pending at once; when all are taken the
  oldest is cut to make room (a real client is through in milliseconds, a stranger holding
  slots is not);
- at most ``max_per_address`` of them come from one address (an IPv6 /64), checked at
  accept, before a thread is started.

Every connection, admitted or not, has ``read_timeout`` per socket read or write and
``body_timeout`` for a whole request body (:meth:`GuardedHandler.read_body`). Body sizes are
the endpoint's own limit (``[traces] max_body_bytes``, ``[hub] max_body_bytes``); a body is
never read before the request is authorized.

``[server]`` configures the guards; see docs/configuration.md and docs/security.md. This
module builds on the standard library and :mod:`kelpie.core.serde` only.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import ssl
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kelpie.core.serde import parse_duration

log = logging.getLogger(__name__)

SERVER_KEYS = frozenset(
    {
        "max_connections",
        "max_connections_per_address",
        "header_timeout",
        "read_timeout",
        "body_timeout",
    }
)
DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_MAX_PER_ADDRESS = 16
DEFAULT_HEADER_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_BODY_TIMEOUT = 60.0
_LOG_EVERY = 60.0  # seconds between refusal warnings per peer


@dataclass(frozen=True, slots=True)
class Limits:
    """The guards of one listener (``[server]``; the hub takes ``[hub] max_connections``)."""

    max_connections: int = DEFAULT_MAX_CONNECTIONS  # pending at once
    max_per_address: int = DEFAULT_MAX_PER_ADDRESS  # pending from one address (IPv6: /64)
    header_timeout: float = DEFAULT_HEADER_TIMEOUT  # accept -> request line and headers
    read_timeout: float = DEFAULT_READ_TIMEOUT  # per socket read or write
    body_timeout: float = DEFAULT_BODY_TIMEOUT  # a whole request body


def load_server(section: Any) -> Limits:
    """``[server]``; defaults when absent. ValueError names the bad key."""
    if section is None:
        return Limits()
    if not isinstance(section, Mapping):
        raise ValueError("[server] must be a table")
    if extra := set(section) - SERVER_KEYS:
        raise ValueError(f"unknown [server] keys: {sorted(extra)}")
    connections = _int(section, "max_connections", DEFAULT_MAX_CONNECTIONS, 8, 10_000)
    per_address = _int(section, "max_connections_per_address", DEFAULT_MAX_PER_ADDRESS, 1, 10_000)
    header = _seconds(section, "header_timeout", DEFAULT_HEADER_TIMEOUT, 1, 60, "1s and 1m")
    read = _seconds(section, "read_timeout", DEFAULT_READ_TIMEOUT, 1, 300, "1s and 5m")
    body = _seconds(section, "body_timeout", DEFAULT_BODY_TIMEOUT, 1, 600, "1s and 10m")
    if body < read:
        raise ValueError("[server] body_timeout must not be shorter than read_timeout")
    return Limits(connections, per_address, header, read, body)


def _int(section: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"[server] {key} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"[server] {key} must be between {low} and {high}")
    return value


def _seconds(
    section: Mapping[str, Any], key: str, default: float, low: float, high: float, span: str
) -> float:
    raw = section.get(key, timedelta(seconds=default))
    if isinstance(raw, bool):
        raise ValueError(f'[server] {key} must be a duration such as "30s"')
    try:
        value = parse_duration(raw).total_seconds()
    except (TypeError, ValueError):
        raise ValueError(f'[server] {key} must be a duration such as "30s"') from None
    if not low <= value <= high:
        raise ValueError(f"[server] {key} must be between {span}")
    return value


@dataclass(frozen=True, slots=True)
class _Pending:
    peer: str
    sock: Any


class GuardedServer(ThreadingHTTPServer):
    """A threading HTTP server whose connections are guarded as the module says."""

    daemon_threads = True
    name = "http"  # in log lines

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[GuardedHandler],
        limits: Limits | None = None,
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.limits = limits or Limits()
        self.tls = tls
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self._slots_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}  # id(socket) -> pending connection
        self._per_peer: dict[str, int] = {}
        self._warned: dict[str, float] = {}
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Refused connections are closed before a thread is started for them.
        peer = peer_key(str(client_address[0]))
        evicted = None
        with self._slots_lock:
            if self._per_peer.get(peer, 0) >= self.limits.max_per_address:
                refused = "too many connections from this address"
            else:
                refused = ""
                if len(self._pending) >= self.limits.max_connections:
                    evicted = self._pending.pop(next(iter(self._pending)))
                    self._drop_peer(evicted.peer)
            if not refused:
                self._pending[id(request)] = _Pending(peer, request)
                self._per_peer[peer] = self._per_peer.get(peer, 0) + 1
        if refused:
            self.warn_once(peer, "%s: closed a connection from %s: %s", self.name, peer, refused)
            self.shutdown_request(request)
            return
        if evicted is not None:
            self.warn_once(
                evicted.peer,
                "%s: all connection slots taken; cut the oldest pending one, from %s",
                self.name,
                evicted.peer,
            )
            shut(evicted.sock)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.admit(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.admit(request)

    def admit(self, request: Any) -> None:
        """Give back the connection's pending slot (once)."""
        with self._slots_lock:
            entry = self._pending.pop(id(request), None)
            if entry is not None:
                self._drop_peer(entry.peer)

    def _drop_peer(self, peer: str) -> None:
        left = self._per_peer.get(peer, 0) - 1
        if left > 0:
            self._per_peer[peer] = left
        else:
            self._per_peer.pop(peer, None)

    def pending(self) -> int:
        """Connections not yet admitted."""
        with self._slots_lock:
            return len(self._pending)

    def warn_once(self, peer: str, message: str, *args: object) -> None:
        """Log a refusal at most once a minute per peer (a scanner must not flood the log)."""
        now = time.monotonic()
        with self._slots_lock:
            if now - self._warned.get(peer, -_LOG_EVERY) < _LOG_EVERY:
                return
            self._warned[peer] = now
            if len(self._warned) > 10_000:
                self._warned.clear()
        log.warning(message, *args)

    def get_request(self) -> tuple[Any, Any]:
        sock, address = self.socket.accept()
        if self.tls is not None:
            # The handshake runs in the request's thread (GuardedHandler.setup), under the
            # header deadline: a client that stalls it cannot hold up accept().
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # the peer went away, a TLS handshake failed, a timeout
            log.debug("%s: connection from %s ended: %s", self.name, client_address[0], exc)
        else:
            log.exception("%s: request from %s failed", self.name, client_address[0])


class GuardedHandler(BaseHTTPRequestHandler):
    """A request handler under its server's :class:`Limits`. Subclasses call :meth:`admit`
    once the request is authorized and read bodies with :meth:`read_body`; until then the
    header deadline applies to everything, the reply included."""

    server: GuardedServer

    def setup(self) -> None:
        limits = self.server.limits
        self.timeout = limits.read_timeout
        self._guard = threading.Timer(limits.header_timeout, self._cut)
        self._guard.daemon = True
        self._guard.start()
        try:
            self.request.settimeout(limits.read_timeout)
            if isinstance(self.request, ssl.SSLSocket):
                self.request.do_handshake()
            super().setup()
        except BaseException:
            self._guard.cancel()  # finish() does not run when setup() fails
            raise

    def _cut(self) -> None:
        shut(self.request)

    def admit(self) -> None:
        """The request is authorized: it gives its pending slot back."""
        self._guard.cancel()
        self.server.admit(self.request)

    def finish(self) -> None:
        self._guard.cancel()
        with contextlib.suppress(OSError):
            super().finish()

    def read_body(self, length: int) -> bytes:
        """Read up to ``length`` bytes within ``body_timeout``; ``TimeoutError`` past it.
        Fewer bytes when the client went away (callers check the length)."""
        limits = self.server.limits
        deadline = time.monotonic() + limits.body_timeout
        chunks: list[bytes] = []
        left = length
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("request body not received within the deadline")
                self.connection.settimeout(min(limits.read_timeout, remaining))
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            self.connection.settimeout(limits.read_timeout)
        return b"".join(chunks)

    def peer(self) -> str:
        return str(self.client_address[0]) if self.client_address else "?"

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


def shut(sock: Any) -> None:
    """Shut a connection down under its handler, on the plain socket: an SSL socket's own
    shutdown would leave the handler mid-handshake with a broken object (not an OSError)."""
    with contextlib.suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)


def peer_key(address: str) -> str:
    """Who a connection counts against: the address, or its /64 for IPv6 (one host holds a
    whole /64)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)
