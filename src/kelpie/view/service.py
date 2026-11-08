"""The read-only farm view listener of ``kelpie serve`` (``[view]``, off by default).

- ``GET /``: the page (:mod:`kelpie.view.page`), for a phone or a browser;
- ``GET /v1/farm``: the view of :mod:`kelpie.core.farmview` as JSON (``kelpie status
  --server`` reads it);
- ``GET /manifest.webmanifest``: lets a phone add the page to its home screen.

There is no write endpoint: every other method is refused with 405 and every other path is
404. With a token set (``[view] token_env``) every request must carry it, compared in
constant time: as a bearer token (``Authorization: Bearer <token>``, scripts) or as the
password of HTTP Basic auth (any user name; a browser asks once and remembers it). Off
loopback ``kelpie serve`` refuses to start without one
(:meth:`ViewSettings.exposure_problem`).

The view is built from the control loop's last snapshot (no board is read per request) and
from the database opened read-only, and kept for ``CACHE_SECONDS``, so a client that asks
often costs one build per interval. Today's cost of chaos and spend come from the report,
computed on this listener's own thread every ``TODAY_EVERY``, never on a request (a ``GET``
must be answered within ``[server] header_timeout``). Connections are guarded by
:mod:`kelpie.httpguard` (``[server]``), like every listener of Kelpie.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.core.farmview import LONG_AFTER, FarmView, Today, build_view, to_dict
from kelpie.core.ledger import LedgerStore
from kelpie.core.model import Snapshot
from kelpie.core.states import StateStore
from kelpie.httpguard import GuardedHandler, GuardedServer, Limits
from kelpie.view.config import ViewSettings
from kelpie.view.page import MANIFEST, render_page
from kelpie.view.read import read_stored

log = logging.getLogger(__name__)

PATH = "/v1/farm"
PAGE = "/"
MANIFEST_PATH = "/manifest.webmanifest"
CACHE_SECONDS = 2.0
TODAY_EVERY = 300.0  # seconds between two computations of today's figures
MEMORY_LOOKBACK = timedelta(days=7)  # ledger entries read from an in-memory ledger
_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; manifest-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

TodayFigures = Callable[[Snapshot, datetime], Today]


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
        long_after: timedelta = LONG_AFTER,
        today: TodayFigures | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._clock = clock
        self._db = db
        self._states = states
        self._store = store
        self._cache_seconds = cache_seconds
        self._long_after = long_after
        self._figures = today
        self._today: Today | None = None
        self._today_failed = False
        self._lock = threading.Lock()
        self._cached: tuple[float, FarmView] | None = None

    @property
    def settled(self) -> bool:
        """Whether today's figures were computed, or tried and failed (no quick retry)."""
        return self._today is not None or self._today_failed

    def view(self) -> FarmView:
        with self._lock:
            cached = self._cached
            if cached is not None and time.monotonic() - cached[0] < self._cache_seconds:
                return cached[1]
            view = self._build()
            self._cached = (time.monotonic(), view)
            return view

    def refresh_today(self) -> None:
        """Compute today's figures from the last snapshot (none yet: nothing to do). Never
        raises: a failure is logged and the previous figures are kept."""
        if self._figures is None:
            return
        snapshot = self._snapshot()
        if snapshot is None:
            return
        try:
            today = self._figures(snapshot, self._clock())
        except Exception:  # the page goes on without them
            if not self._today_failed:
                log.exception("today's figures for the farm view failed")
            self._today_failed = True
            return
        self._today_failed = False
        with self._lock:
            self._today = today
            self._cached = None

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
        if self._today_failed:
            problems.append("today's figures could not be computed (see kelpie serve's log)")
        return build_view(
            now,
            snapshot,
            states,
            entries,
            source="server",
            stored=statuses,
            problems=problems,
            long_after=self._long_after,
            today=self._today,
        )


def _presented(header: str | None) -> bytes | None:
    """The token in an ``Authorization`` header: a bearer token, or a Basic password."""
    if header is None:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() == "bearer":
        return value.strip().encode()
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError):
            return None
        return decoded.partition(b":")[2]
    return None


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
        presented = _presented(header)
        return presented is not None and hmac.compare_digest(presented, self.token.encode())


class ViewHandler(GuardedHandler):
    server: ViewServer

    def do_GET(self) -> None:
        self.close_connection = True
        path = self.path.split("?")[0]
        if not self.server.authorized(self.headers.get("Authorization")):
            # A browser asks for the password (the token) once and remembers it.
            challenge = 'Bearer, Basic realm="kelpie view", charset="UTF-8"'
            self._json(401, {"error": "unauthorized"}, {"WWW-Authenticate": challenge})
            return
        if path not in (PATH, PAGE, MANIFEST_PATH):
            self._json(404, {"error": "not found"})
            return
        if path == MANIFEST_PATH:
            self._send(200, MANIFEST.encode(), "application/manifest+json")
            return
        try:
            view = self.server.source.view()
            if path == PAGE:
                page = render_page(view).encode()
        except Exception:  # a bug in the view must not kill the listener or leak a trace
            log.exception("farm view failed")
            self._json(500, {"error": "internal error"})
            return
        if path == PAGE:
            self._send(200, page, "text/html; charset=utf-8", {"Content-Security-Policy": _CSP})
        else:
            self._json(200, to_dict(view))

    def _refuse(self) -> None:
        self.close_connection = True
        self._json(405, {"error": "read-only: only GET"}, {"Allow": "GET"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = _refuse

    def _json(self, code: int, body: Any, headers: dict[str, str] | None = None) -> None:
        data = (json.dumps(body, ensure_ascii=False) + "\n").encode()
        self._send(code, data, "application/json; charset=utf-8", headers)

    def _send(
        self, code: int, data: bytes, content_type: str, headers: dict[str, str] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class ViewService:
    """The listener as a service of ``kelpie serve``: started with the loop, stopped first.
    With today's figures, a second thread computes them every ``today_every`` seconds."""

    def __init__(self, server: ViewServer, today_every: float = TODAY_EVERY) -> None:
        self.server = server
        self.today_every = today_every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._today: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.server.serve_forever, name="kelpie-view", daemon=True
        )
        self._thread.start()
        self._today = threading.Thread(target=self._refresh, name="kelpie-view-today", daemon=True)
        self._today.start()
        log.info(
            "farm view (read-only) on http://%s:%d/%s",
            self.server.settings.host,
            self.server.server_address[1],
            "" if self.server.token is None else " (token required)",
        )

    def _refresh(self) -> None:
        source = self.server.source
        while not self._stop.is_set():
            source.refresh_today()
            # The first tick may come later than the first wait: try again soon until then.
            wait = self.today_every if source.settled else min(self.today_every, 30.0)
            self._stop.wait(wait)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self.server.shutdown()
            self._thread.join(timeout)
        if self._today is not None:
            self._today.join(timeout)
        self.server.server_close()
