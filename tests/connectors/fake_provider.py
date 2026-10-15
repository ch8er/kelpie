"""A local fake of an LLM provider API for tests (loopback only, no real network)."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeProvider:
    """Answers every GET with ``status``, ``headers`` and ``body``; records requests."""

    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body = b"{}"
        self.redirect_to: str | None = None
        self.drip = 0.0  # seconds between bytes of the body (or of the headers)
        self.drip_headers = False
        self.requests: list[tuple[str, dict[str, str]]] = []  # path, headers
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
                outer.requests.append((self.path, dict(self.headers)))
                if outer.redirect_to:
                    self.send_response(307)
                    self.send_header("Location", outer.redirect_to + self.path)
                    self.end_headers()
                    return
                if outer.drip and outer.drip_headers:
                    self._drip(b"HTTP/1.1 200 OK\r\nRateLimit-Remaining: 5\r\n")
                    return
                self.send_response(outer.status)
                for name, value in outer.headers.items():
                    self.send_header(name, value)
                if outer.drip:
                    self.send_header("Content-Length", str(len(outer.body) + 1000))
                    self.end_headers()
                    self._drip(outer.body)
                    return
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                with contextlib.suppress(OSError):  # the client may have given up
                    self.wfile.write(outer.body)

            def _drip(self, data: bytes) -> None:
                with contextlib.suppress(OSError):
                    for i in range(len(data)):
                        self.wfile.write(data[i : i + 1])
                        self.wfile.flush()
                        time.sleep(outer.drip)

            def log_message(self, *args: object) -> None:
                pass

        return Handler

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@contextmanager
def fake_provider() -> Iterator[FakeProvider]:
    server = FakeProvider()
    server.thread.start()
    try:
        yield server
    finally:
        server.close()
