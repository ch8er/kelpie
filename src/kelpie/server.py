"""HTTP server for ``kelpie serve``: ``/metrics`` and ``/healthz``; runs the control loop.

OTLP ingest will be mounted here.
"""

from __future__ import annotations

import logging
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

from kelpie.app import App
from kelpie.metrics import Metrics

log = logging.getLogger(__name__)


class KelpieHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], metrics: Metrics) -> None:
        super().__init__(address, Handler)
        self.metrics = metrics


class Handler(BaseHTTPRequestHandler):
    server: KelpieHTTPServer

    def do_GET(self) -> None:
        match self.path:
            case "/metrics":
                body = self.server.metrics.render()
                self._reply(200, body, "text/plain; version=0.0.4; charset=utf-8")
            case "/healthz":
                self._reply(200, "ok\n", "text/plain; charset=utf-8")
            case _:
                self._reply(404, "not found\n", "text/plain; charset=utf-8")

    def _reply(self, code: int, body: str, content_type: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)


def make_server(host: str, port: int, metrics: Metrics | None = None) -> KelpieHTTPServer:
    return KelpieHTTPServer((host, port), metrics or Metrics())


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
    server = make_server(host, port, app.metrics)

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
