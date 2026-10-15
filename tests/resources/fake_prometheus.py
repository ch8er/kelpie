"""A local fake of the Prometheus HTTP API for tests (no real network)."""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


def vector(*series: tuple[dict[str, str], str]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [1772449200.0, v]} for m, v in series],
        },
    }


class FakeProm:
    """Answers ``/api/v1/query`` from ``responses`` (PromQL text -> JSON body)."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.status = 200
        self.delay = 0.0
        self.redirect_to: str | None = None
        self.raw: bytes | None = None  # sent instead of the JSON body
        self.requests: list[tuple[str, str, dict[str, str]]] = []  # path, promql, headers
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parts = urlsplit(self.path)
                promql = parse_qs(parts.query).get("query", [""])[0]
                outer.requests.append((parts.path, promql, dict(self.headers)))
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to + self.path)
                    self.end_headers()
                    return
                body = outer.raw
                if body is None:
                    body = json.dumps(outer.responses.get(promql, vector())).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                with contextlib.suppress(OSError):  # the client may have given up (timeout test)
                    self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        return Handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def fake_prometheus() -> Iterator[FakeProm]:
    prom = FakeProm()
    prom.thread.start()
    try:
        yield prom
    finally:
        prom.close()
