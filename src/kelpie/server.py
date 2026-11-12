"""HTTP server for ``kelpie serve``: ``/metrics``, ``/healthz`` and OTLP/HTTP ingest
(``POST /v1/traces``, and ``POST /v1/metrics`` for Claude Code's cost and token metrics,
behind the same token, Host and body guards); runs the control loop.

Requests are served on their own threads, so a large trace export never delays the control
loop, which runs on its own thread too. Connections are guarded by :mod:`kelpie.httpguard`
(``[server]``), like the hub's: a connection is pending until an OTLP export's token checks
out; a ``GET`` stays pending, so its reply too must be written within the header deadline.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Mapping
from types import FrameType

from kelpie.app import App
from kelpie.httpguard import GuardedHandler, GuardedServer, Limits
from kelpie.metrics import Metrics
from kelpie.traces.ingest import PATH as TRACES_PATH
from kelpie.traces.ingest import TraceIngest
from kelpie.traces.metrics import PATH as METRICS_PATH

log = logging.getLogger(__name__)


class KelpieHTTPServer(GuardedServer):
    name = "serve"

    def __init__(
        self,
        address: tuple[str, int],
        metrics: Metrics,
        ingest: TraceIngest | None = None,
        limits: Limits | None = None,
    ) -> None:
        self.metrics = metrics
        self.ingest = ingest
        super().__init__(address, Handler, limits)


class Handler(GuardedHandler):
    server: KelpieHTTPServer

    def do_GET(self) -> None:
        # Never admitted: rendered at once, the reply must be written within the header
        # deadline, so a client that asks and does not read loses its slot.
        self.close_connection = True
        match self.path:
            case "/metrics":
                body = self.server.metrics.render()
                self._reply(200, body, "text/plain; version=0.0.4; charset=utf-8")
            case "/healthz":
                self._reply(200, "ok\n", "text/plain; charset=utf-8")
            case _:
                self._reply(404, "not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        self.close_connection = True  # one request per connection, like the hub
        ingest = self.server.ingest
        path = self.path.split("?")[0]
        if path not in (TRACES_PATH, METRICS_PATH) or ingest is None:
            self._reply(404, "not found\n", "text/plain; charset=utf-8")
            return
        metrics = path == METRICS_PATH  # Claude Code's cost and token metrics: same guards
        count = (
            self.server.metrics.metrics_request if metrics else self.server.metrics.traces_request
        )
        try:
            if metrics:
                reply = ingest.handle_metrics(self.headers, self._read_body)
            else:
                reply = ingest.handle(self.headers, self._read_body)
        except OSError as exc:  # the client stalled past the timeout or went away
            log.info("OTLP export from %s aborted: %s", self.client_address[0], exc)
            count(408)
            return
        except Exception:  # a bug in ingest must not look like a client error or kill Kelpie
            log.exception("OTLP ingest failed")
            count(500)
            self._reply(500, "internal error\n", "text/plain; charset=utf-8")
            return
        # ``reply.close`` (body left unread) needs nothing more: every connection closes.
        self._send(reply.status, reply.body, reply.content_type, reply.headers)

    def _read_body(self, length: int) -> bytes:
        """Ingest calls this only once the export is authorized: it gives its pending slot
        back (ingest's own ``max_concurrent_requests`` limits bodies in flight)."""
        self.admit()
        return self.read_body(length)

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


def make_server(
    host: str,
    port: int,
    metrics: Metrics | None = None,
    ingest: TraceIngest | None = None,
    limits: Limits | None = None,
) -> KelpieHTTPServer:
    return KelpieHTTPServer((host, port), metrics or Metrics(), ingest, limits)


def run(app: App, server: KelpieHTTPServer) -> None:
    """Serve until ``server.shutdown()``; then stop the loop and close the ledger."""
    app.loop.start()
    try:
        for service in app.services:
            service.start()
        server.serve_forever()
    finally:
        app.close()
        server.server_close()
        log.info("kelpie stopped")


def serve(app: App, host: str = "0.0.0.0", port: int = 9464) -> None:
    server = make_server(host, port, app.metrics, app.ingest, app.settings.server)

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
