"""A local fake of a Slack incoming webhook for tests (loopback only, no real network)."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = tuple[int, str, dict[str, str]]  # status, plain-text body, extra headers


class FakeSlack:
    """Answers each POST from ``replies`` in turn, then ``default`` (Slack says ``ok``)."""

    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.default: Reply = (200, "ok", {})
        self.redirect_to: str | None = None
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []  # path, headers, json
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def origin(self) -> str:
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
                status, text, headers = outer.replies.pop(0) if outer.replies else outer.default
                data = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        return Handler


@contextmanager
def fake_slack() -> Iterator[FakeSlack]:
    fake = FakeSlack()
    fake.thread.start()
    try:
        yield fake
    finally:
        fake.server.shutdown()
        fake.server.server_close()
