"""A local fake of the Telegram Bot API for tests (loopback only, no real network)."""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Route = Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]


class FakeTelegram:
    """Answers each POST from ``replies`` (status, body) in turn, then ``default``. A method
    with a ``routes`` entry (``getUpdates``, ...) is answered by it instead; :meth:`bot`
    installs routes that behave like a bot's: sent messages get ids, presses are queued as
    updates, answers and edits are taken."""

    def __init__(self) -> None:
        self.replies: list[tuple[int, dict[str, Any]]] = []
        self.default: tuple[int, dict[str, Any]] = (200, {"ok": True, "result": {}})
        self.redirect_to: str | None = None
        self.requests: list[tuple[str, dict[str, str], dict[str, Any]]] = []  # path, headers, json
        self.routes: dict[str, Route] = {}
        # bot mode
        self.updates: list[dict[str, Any]] = []
        self.webhook = ""
        self.next_message_id = 100
        self.next_update_id = 500
        self.messages: list[tuple[int, dict[str, Any]]] = []  # (message id, body) sent
        self._ready = threading.Condition()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def calls(self, method: str) -> list[dict[str, Any]]:
        """The bodies of the calls of ``method``, in order."""
        return [body for path, _, body in list(self.requests) if path.endswith(f"/{method}")]

    # --- bot mode ------------------------------------------------------------------------

    def bot(self) -> FakeTelegram:
        self.routes.update(
            sendMessage=self._send,
            getUpdates=self._get_updates,
            answerCallbackQuery=lambda body: (200, {"ok": True, "result": True}),
            editMessageText=lambda body: (200, {"ok": True, "result": {"message_id": 1}}),
            editMessageReplyMarkup=lambda body: (200, {"ok": True, "result": {"message_id": 1}}),
            getWebhookInfo=lambda body: (200, {"ok": True, "result": {"url": self.webhook}}),
        )
        return self

    def _send(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self._ready:
            self.next_message_id += 1
            message_id = self.next_message_id
            self.messages.append((message_id, body))
        return 200, {"ok": True, "result": {"message_id": message_id, "text": body.get("text")}}

    def _get_updates(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        offset = body.get("offset", 0)
        with self._ready:
            if not any(u["update_id"] >= offset for u in self.updates):
                self._ready.wait(0.05)  # a short long poll: no busy loop
            found = [u for u in self.updates if u["update_id"] >= offset]
            self.updates = found  # confirmed below the offset, as Telegram forgets them
        return 200, {"ok": True, "result": found[: body.get("limit", 100)]}

    def push(self, update: dict[str, Any]) -> int:
        """Queue an update (``update_id`` is added); returns its id."""
        with self._ready:
            self.next_update_id += 1
            update = {"update_id": self.next_update_id, **update}
            self.updates.append(update)
            self._ready.notify_all()
            return int(update["update_id"])

    def sent(self) -> list[tuple[int, dict[str, Any]]]:
        """(message id, body) of every message sent, in order (bot mode)."""
        with self._ready:
            return list(self.messages)

    def press(
        self,
        message_id: int,
        message: dict[str, Any],
        data: str,
        user_id: int,
        chat_id: int | str,
        **sender: Any,
    ) -> int:
        """A person presses a button carrying ``data`` under message ``message_id``
        (``message``: the sent body, whose text and keyboard Telegram echoes back)."""
        echoed = {
            "message_id": message_id,
            "chat": {"id": int(chat_id), "type": "supergroup"},
            "date": 1,
            "text": message.get("text", ""),
        }
        for key in ("reply_markup", "entities"):
            if key in message:
                echoed[key] = message[key]
        query = {
            "id": f"q{message_id}-{user_id}-{len(self.updates)}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Alice", **sender},
            "message": echoed,
            "chat_instance": "1",
            "data": data,
        }
        return self.push({"callback_query": query})

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw or b"{}")
                outer.requests.append((self.path, dict(self.headers), body))
                if outer.redirect_to:
                    self.send_response(307)
                    self.send_header("Location", outer.redirect_to + self.path)
                    self.end_headers()
                    return
                route = outer.routes.get(self.path.rsplit("/", 1)[-1])
                if route is not None:
                    status, reply = route(body)
                else:
                    status, reply = outer.replies.pop(0) if outer.replies else outer.default
                data = json.dumps(reply).encode()
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
