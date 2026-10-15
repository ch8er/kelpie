"""HTTP server for ``kelpie serve``: ``/metrics``, ``/healthz`` and OTLP/HTTP trace ingest
(``POST /v1/traces``); runs the control loop.

Requests are served on their own threads, so a large trace export never delays the control
loop, which runs on its own thread too.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

from kelpie.app import App
from kelpie.metrics import Metrics
from kelpie.traces.ingest import PATH as TRACES_PATH
from kelpie.traces.ingest import TraceIngest

log = logging.getLogger(__name__)


class KelpieHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], metrics: Metrics, ingest: TraceIngest | None = None
    ) -> None:
        super().__init__(address, Handler)
        self.metrics = metrics
        self.ingest = ingest


class Handler(BaseHTTPRequestHandler):
    server: KelpieHTTPServer
    # Socket timeout per read: a client that stalls (slowloris) loses its connection
    # instead of holding a thread.
    timeout = 30
    # Whole-body deadline: a client dripping a byte just under ``timeout`` must not keep an
    # ingest slot forever.
    body_deadline = 60.0

    def do_GET(self) -> None:
        match self.path:
            case "/metrics":
                body = self.server.metrics.render()
                self._reply(200, body, "text/plain; version=0.0.4; charset=utf-8")
            case "/healthz":
                self._reply(200, "ok\n", "text/plain; charset=utf-8")
            case _:
                self._reply(404, "not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        ingest = self.server.ingest
        if self.path.split("?")[0] != TRACES_PATH or ingest is None:
            self.close_connection = True
            self._reply(404, "not found\n", "text/plain; charset=utf-8")
            return
        try:
            reply = ingest.handle(self.headers, self._read_body)
        except OSError as exc:  # the client stalled past the timeout or went away
            log.info("trace export from %s aborted: %s", self.client_address[0], exc)
            self.server.metrics.traces_request(408)
            self.close_connection = True
            return
        except Exception:  # a bug in ingest must not look like a client error or kill Kelpie
            log.exception("trace ingest failed")
            self.server.metrics.traces_request(500)
            self.close_connection = True
            self._reply(500, "internal error\n", "text/plain; charset=utf-8")
            return
        if reply.close:
            self.close_connection = True
        self._send(reply.status, reply.body, reply.content_type, reply.headers)

    def _read_body(self, length: int) -> bytes:
        """Read ``length`` bytes within ``body_deadline``; ``TimeoutError`` past it."""
        deadline = time.monotonic() + self.body_deadline
        chunks: list[bytes] = []
        left = length
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("request body not received within the deadline")
                self.connection.settimeout(min(self.timeout, remaining))
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    break  # the client went away; the decoder rejects a short body
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            self.connection.settimeout(self.timeout)
        return b"".join(chunks)

    def _reply(self, code: int, body: str, content_type: str) -> None:
        self._send(code, body.encode(), content_type)

    def _send(
        self, code: int, data: bytes, content_type: str, headers: Mapping[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


def make_server(
    host: str, port: int, metrics: Metrics | None = None, ingest: TraceIngest | None = None
) -> KelpieHTTPServer:
    return KelpieHTTPServer((host, port), metrics or Metrics(), ingest)


def run(app: App, server: KelpieHTTPServer) -> None:
    """Serve until ``server.shutdown()``; then stop the loop and close the ledger."""
    app.loop.start()
    try:
        server.serve_forever()
    finally:
        app.close()
        server.server_close()
        log.info("kelpie stopped")


def serve(app: App, host: str = "0.0.0.0", port: int = 9464) -> None:
    server = make_server(host, port, app.metrics, app.ingest)

    def on_signal(signum: int, frame: FrameType | None) -> None:
        log.info("received %s, shutting down", signal.Signals(signum).name)
        # shutdown() blocks until serve_forever returns, so it cannot run in this thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    # In a container Kelpie is PID 1, where SIGTERM has no default action.
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    log.info(
        "kelpie listening on http://%s:%d (%d rules, dry-run %s)",
        host,
        port,
        len(app.loaded.rules),
        "on" if app.settings.engine.dry_run else "off",
    )
    run(app, server)
