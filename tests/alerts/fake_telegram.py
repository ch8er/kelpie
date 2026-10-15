"""A local fake of the Telegram Bot API for tests (loopback only, no real network)."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeTelegram:
    """Answers each POST from ``replies`` (status, body) in turn, then ``default``."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, dict[str, Any]]] = []
        self.default: tuple[int, dict[str, Any]] = (200, {"ok": True, "result": {}})
        self.redirect_to: str | None = None
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []  # path, headers, json
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
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((self.path, dict(self.headers), json.loads(raw or b"{}")))
                if outer.redirect_to:
                    self.send_response(307)
                    self.send_header("Location", outer.redirect_to + self.path)
                    self.end_headers()
                    return
                status, body = outer.replies.pop(0) if outer.replies else outer.default
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        return Handler


@contextmanager
def fake_telegram() -> Iterator[FakeTelegram]:
    fake = FakeTelegram()
    fake.thread.start()
    try:
        yield fake
    finally:
        fake.server.shutdown()
        fake.server.server_close()
