"""HTTP server for ``kelpie serve`` (stub): ``/metrics`` and ``/healthz``.

The control loop and OTLP ingest will be mounted here.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from kelpie import metrics

log = logging.getLogger(__name__)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        match self.path:
            case "/metrics":
                self._reply(200, metrics.render(), "text/plain; version=0.0.4; charset=utf-8")
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


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "0.0.0.0", port: int = 9464) -> None:
    server = make_server(host, port)
    log.info("kelpie listening on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
