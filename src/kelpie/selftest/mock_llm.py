"""A local OpenAI-compatible model for self-tests: it spends no tokens and always asks for the
same tool call, which is what a runaway agent looks like from the outside.

``POST <base>/chat/completions`` with tools answers one ``terminal`` tool call running
``command`` (the same arguments every time); without tools (a title, a summary) it answers a
line of text. ``stream: true`` gets the same answer as server-sent events. ``GET
<base>/models`` lists the one model. Anything else is a 404. It listens on 127.0.0.1 only and
counts the requests it answered, so a self-test can tell that the agent stopped calling it.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "kelpie-selftest"
MAX_BODY = 16 * 1024 * 1024  # a long conversation resends its history every turn
TEXT = "kelpie selftest"


class MockLLM:
    """``with MockLLM("sleep 1") as llm: ... llm.base_url ...``"""

    def __init__(self, command: str, tool: str = "terminal") -> None:
        self.command = command
        self.tool = tool
        self._lock = threading.Lock()
        self._calls = 0
        self._last: float | None = None
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    @property
    def calls(self) -> int:
        """Chat completions answered so far."""
        with self._lock:
            return self._calls

    @property
    def last_call(self) -> float | None:
        """``time.monotonic()`` of the last chat completion answered."""
        with self._lock:
            return self._last

    def __enter__(self) -> MockLLM:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._thread.is_alive():
            self._server.shutdown()
        self._server.server_close()

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """The assistant message for one chat completion request, and count it."""
        with self._lock:
            self._calls += 1
            self._last = time.monotonic()
            n = self._calls
        if not request.get("tools"):
            return {"role": "assistant", "content": TEXT}
        arguments = json.dumps({"command": self.command})
        call = {
            "id": f"call_kelpie_{n}",
            "type": "function",
            "function": {"name": self.tool, "arguments": arguments},
        }
        return {"role": "assistant", "content": None, "tool_calls": [call]}


def _handler(llm: MockLLM) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass  # quiet: the self-test reports what matters

        def do_GET(self) -> None:
            if self.path.split("?")[0].rstrip("/").endswith("/models"):
                self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
            else:
                self._json({"error": {"message": "not found"}}, 404)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if not self.path.split("?")[0].endswith("/chat/completions"):
                self._json({"error": {"message": "not found"}}, 404)
                return
            if length <= 0:
                self._json({"error": {"message": "Content-Length required"}}, 411)
                return
            if length > MAX_BODY:
                self._json({"error": {"message": "request too large"}}, 413)
                return
            try:
                request = json.loads(self.rfile.read(length))
            except ValueError:
                self._json({"error": {"message": "not JSON"}}, 400)
                return
            if not isinstance(request, dict):
                self._json({"error": {"message": "not an object"}}, 400)
                return
            message = llm.answer(request)
            finish = "tool_calls" if message.get("tool_calls") else "stop"
            if request.get("stream"):
                self._stream(message, finish)
                return
            self._json(
                {
                    "id": "chatcmpl-kelpie",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": MODEL,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            )

        def _json(self, body: dict[str, Any], status: int = 200) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _stream(self, message: dict[str, Any], finish: str) -> None:
            delta: dict[str, Any] = {"role": "assistant", "content": message.get("content")}
            if message.get("tool_calls"):
                delta["tool_calls"] = [
                    dict(c, index=i) for i, c in enumerate(message["tool_calls"])
                ]
            chunks = [
                {"index": 0, "delta": delta, "finish_reason": None},
                {"index": 0, "delta": {}, "finish_reason": finish},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            for choice in chunks:
                event = {
                    "id": "chatcmpl-kelpie",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": MODEL,
                    "choices": [choice],
                }
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.close_connection = True

    return Handler
