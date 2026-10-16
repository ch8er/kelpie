"""The agent's OTLP relay: exporters on an agent host send trace exports to
``127.0.0.1:4318/v1/traces`` (the usual OTLP/HTTP port; in single-host mode they go to
Kelpie's own 9464 instead); the relay forwards
each export unchanged to the hub's ``/v1/traces`` under the agent's token, and hands the
hub's answer back. The hub puts the spans in the agent's namespace.

It listens on loopback only and asks nothing of local senders, like the hooks' spool:
anything on the host that can reach it can add spans to this host's traces, which is what
the host's own users can do anyway.
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

log = logging.getLogger(__name__)

PATH = "/v1/traces"
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
    ) -> None:
        self.target = server
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

    def do_POST(self) -> None:
        self.close_connection = True
        if self.path.split("?")[0] != PATH:
            self._reply(404, b"not found\n", "text/plain")
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
                PATH,
                headers,
                body,
                self.server.timeout_seconds,
                max_bytes=MAX_REPLY_BYTES,
                context=self.server.context,
            )
        except net.RequestError as exc:
            log.warning("relay: forwarding spans to the hub failed: %s", exc.detail)
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
