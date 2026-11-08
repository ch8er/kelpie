"""The read-only farm view listener of ``kelpie serve`` (``[view]``, off by default).

``GET /v1/farm`` serves the view of :mod:`kelpie.core.farmview` as JSON (``kelpie status
--server`` reads it). There is no write endpoint: every other method is refused with 405
and every other path is 404. With a token set (``[view] token_env``) every request must
carry ``Authorization: Bearer <token>``, compared in constant time; off loopback ``kelpie
serve`` refuses to start without one (:meth:`ViewSettings.exposure_problem`).

The view is built from the control loop's last snapshot (no board is read per request) and
from the database opened read-only, and kept for ``CACHE_SECONDS``, so a client that asks
often costs one build per interval. Connections are guarded by :mod:`kelpie.httpguard`
(``[server]``), like every listener of Kelpie.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.core.farmview import FarmView, build_view, to_dict
from kelpie.core.ledger import LedgerStore
from kelpie.core.model import Snapshot
from kelpie.core.states import StateStore
from kelpie.httpguard import GuardedHandler, GuardedServer, Limits
from kelpie.view.config import ViewSettings
from kelpie.view.read import read_stored

log = logging.getLogger(__name__)

PATH = "/v1/farm"
CACHE_SECONDS = 2.0
MEMORY_LOOKBACK = timedelta(days=7)  # ledger entries read from an in-memory ledger


class ViewSource:
    """Builds the view on demand from the loop's last snapshot and the stored history."""

    def __init__(
        self,
        snapshot: Callable[[], Snapshot | None],
        clock: Callable[[], datetime],
        db: Path | None,
        states: StateStore | None = None,
        store: LedgerStore | None = None,
        cache_seconds: float = CACHE_SECONDS,
    ) -> None:
        self._snapshot = snapshot
        self._clock = clock
        self._db = db
        self._states = states
        self._store = store
        self._cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: tuple[float, FarmView] | None = None

    def view(self) -> FarmView:
        with self._lock:
            if (
                self._cached is not None
                and time.monotonic() - self._cached[0] < self._cache_seconds
            ):
                return self._cached[1]
            view = self._build()
            self._cached = (time.monotonic(), view)
            return view

    def _build(self) -> FarmView:
        now = self._clock()
        snapshot = self._snapshot()
        problems: list[str] = []
        if snapshot is None:
            problems.append("kelpie serve has not finished a tick yet")
        if self._db is not None:  # its own read-only connection, as kelpie status reads it
            stored = read_stored(self._db, statuses=snapshot is None)
            states, entries, statuses = stored.states, stored.entries, stored.statuses
            problems += stored.problems
        else:  # an in-memory ledger: read across threads, best effort (a stale entry at most)
            states = list(self._states.open_states()) if self._states is not None else []
            entries = list(self._store.since(now - MEMORY_LOOKBACK)) if self._store else []
            statuses = None
        return build_view(
            now,
            snapshot,
            states,
            entries,
            source="server",
            stored=statuses,
            problems=problems,
        )


class ViewServer(GuardedServer):
    name = "view"

    def __init__(
        self,
        settings: ViewSettings,
        source: ViewSource,
        token: str | None,
        limits: Limits | None = None,
    ) -> None:
        self.settings = settings
        self.source = source
        self.token = token
        super().__init__((settings.host, settings.port), ViewHandler, limits)

    def authorized(self, header: str | None) -> bool:
        if self.token is None:
            return True
        if header is None or not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].strip().encode(), self.token.encode())


class ViewHandler(GuardedHandler):
    server: ViewServer

    def do_GET(self) -> None:
        self.close_connection = True
        if not self.server.authorized(self.headers.get("Authorization")):
            self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            return
        if self.path.split("?")[0] != PATH:
            self._json(404, {"error": "not found"})
            return
        try:
            body = to_dict(self.server.source.view())
        except Exception:  # a bug in the view must not kill the listener or leak a trace
            log.exception("farm view failed")
            self._json(500, {"error": "internal error"})
            return
        self._json(200, body)

    def _refuse(self) -> None:
        self.close_connection = True
        self._json(405, {"error": "read-only: only GET"}, {"Allow": "GET"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = _refuse

    def _json(self, code: int, body: Any, headers: dict[str, str] | None = None) -> None:
        data = (json.dumps(body, ensure_ascii=False) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class ViewService:
    """The listener as a service of ``kelpie serve``: started with the loop, stopped first."""

    def __init__(self, server: ViewServer) -> None:
        self.server = server
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.server.serve_forever, name="kelpie-view", daemon=True
        )
        self._thread.start()
        log.info(
            "farm view (read-only) on http://%s:%d%s%s",
            self.server.settings.host,
            self.server.server_address[1],
            PATH,
            "" if self.server.token is None else " (token required)",
        )

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self.server.shutdown()
            self._thread.join(timeout)
        self.server.server_close()
