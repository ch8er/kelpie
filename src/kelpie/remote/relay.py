"""The agent's OTLP relay: exporters on an agent host send trace exports to
``127.0.0.1:4318/v1/traces`` (the usual OTLP/HTTP port; in single-host mode they go to
Kelpie's own 9464 instead), and Claude Code its cost and token metrics to ``/v1/metrics``
on the same port; the relay forwards each export unchanged to the same path on the hub
under the agent's token, and hands the hub's answer back. The hub puts the spans, and the
metric points' sessions and tasks, in the agent's namespace.

It listens on loopback only and asks nothing of local senders, like the hooks' spool:
anything on the host that can reach it can add spans to this host's traces, which is what
the host's own users can do anyway. A web page in a browser on the host is not such a
sender: a request naming any host but a loopback one (or a ``[server] hosts`` name) gets
421, as on every listener of ``kelpie serve`` (DNS rebinding, #406).
"""

from __future__ import annotations

import logging
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kelpie import net
from kelpie.httpguard import host_allowed

log = logging.getLogger(__name__)

PATH = "/v1/traces"
METRICS_PATH = "/v1/metrics"
PATHS = frozenset({PATH, METRICS_PATH})  # forwarded to the same path on the hub
MAX_REPLY_BYTES = 64 * 1024
MAX_CONNECTIONS = 16


class Relay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        server: str,
        token: str,
        max_body_bytes: int,
        timeout: float = 30.0,
        context: ssl.SSLContext | None = None,
        post: Callable[..., net.Response] = net.post,
        hosts: tuple[str, ...] = (),
    ) -> None:
        self.target = server
        self.hosts = hosts  # [server] hosts: further names exporters reach the relay by
        self.warned_at = -60.0  # monotonic time of the last refused-host warning
        self.token = token
        self.max_body_bytes = max_body_bytes
        self.timeout_seconds = timeout
        self.context = context
        self.post = post
        self.connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, RelayHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # the exporter went away
            log.debug("relay: connection ended: %s", exc)
        else:
            log.exception("relay: request failed")


class RelayHandler(BaseHTTPRequestHandler):
    server: Relay
    timeout = 30
    body_deadline = 60.0

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        host = self.headers.get("Host")
        if host is None or host_allowed(host, self.server.server_address[0], self.server.hosts):
            return True
        self.close_connection = True
        now = time.monotonic()
        if now - self.server.warned_at >= 60.0:  # a page retrying must not flood the log
            self.server.warned_at = now
            log.warning(
                "relay: refused a request for host %r (DNS rebinding?); a name exporters "
                "should reach the relay by goes in [server] hosts",
                host[:100],
            )
        self._reply(421, b"unexpected host: use the address the relay is on\n", "text/plain")
        return False

    def do_POST(self) -> None:
        self.close_connection = True
        path = self.path.split("?")[0]
        if path not in PATHS:
            self._reply(404, b"not found\n", "text/plain")
            return
        if self.headers.get("Origin") is not None:
            # No exporter sends an Origin; a browser does on every cross-site POST, and the
            # relay adds the agent's token to whatever it forwards (#406).
            self._reply(403, b"requests from a web page are refused\n", "text/plain")
            return
        raw = self.headers.get("Content-Length")
        try:
            length = int(raw) if raw is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._reply(411, b"Content-Length required\n", "text/plain")
            return
        if length > self.server.max_body_bytes:
            self._reply(413, b"body too large\n", "text/plain")
            return
        try:
            body = self._read(length)
        except OSError:
            return
        if len(body) != length:
            self._reply(400, b"body shorter than Content-Length\n", "text/plain")
            return
        headers = {"Authorization": f"Bearer {self.server.token}"}
        for name in ("Content-Type", "Content-Encoding"):
            value = self.headers.get(name)
            if value and net.valid_header_value(value):
                headers[name] = value
        try:
            response = self.server.post(
                self.server.target,
                path,
                headers,
                body,
                self.server.timeout_seconds,
                max_bytes=MAX_REPLY_BYTES,
                context=self.server.context,
            )
        except net.RequestError as exc:
            what = "metrics" if path == METRICS_PATH else "spans"
            log.warning("relay: forwarding %s to the hub failed: %s", what, exc.detail)
            self._reply(503, b"hub unreachable, retry later\n", "text/plain", {"Retry-After": "5"})
            return
        content_type = response.headers.get("content-type", "application/octet-stream")
        extra = {}
        if retry := response.headers.get("retry-after"):
            extra["Retry-After"] = retry
        self._reply(response.status, response.body, content_type, extra)

    def _read(self, length: int) -> bytes:
        deadline = time.monotonic() + self.body_deadline
        chunks: list[bytes] = []
        left = length
        while left > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request body not received within the deadline")
            self.connection.settimeout(min(self.timeout, remaining))
            chunk = self.rfile.read1(min(left, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    def _reply(
        self, code: int, body: bytes, content_type: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)
