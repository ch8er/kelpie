"""The hub of ``kelpie serve``: where ``kelpie agent`` processes report and pick up actions.

:class:`Hub` keeps each agent's latest report and its queue of actions; :class:`HubServer`
is its HTTP(S) listener (``[hub] listen``). An agent is identified by the token it sends,
never by anything in the body. Requests are limited in size (decompressed size included),
time and number; see docs/multi-host.md.

:meth:`Hub.send` hands one action to an agent and waits for its result. While a result is
outstanding the same action (same adapter, kind, target and params) keeps its id, and the
agent remembers results by id, so a retry after a slow confirmation is not carried out
twice; a result that arrives late answers the retry.
"""

from __future__ import annotations

import hmac
import json
import logging
import ssl
import threading
import time
import uuid
import zlib
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kelpie.remote.config import HubSettings
from kelpie.remote.protocol import (
    ACTIONS_PATH,
    MAX_ACTIONS,
    MAX_TTL,
    REPORT_PATH,
    ProtocolError,
    Report,
    Result,
    WireAction,
    decode_poll,
    decode_report,
    encode_action,
)

log = logging.getLogger(__name__)

# How long an unanswered action keeps its id (and a late result waits to be claimed).
PENDING_TTL = timedelta(hours=1)


class AgentUnavailable(Exception):
    """The agent cannot take the action now (silent, busy, or it did not confirm in
    time). ``public_reason`` is what a human reads."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.public_reason = reason


class Busy(Exception):
    """The agent already has a request of this kind in flight (HTTP 409)."""


class OutOfOrder(Exception):
    """A report not newer than the last one accepted from the agent (HTTP 409)."""


@dataclass(slots=True)
class _Pending:
    action: WireAction
    created: datetime
    deadline: float = 0.0  # monotonic: the waiting send gives up then
    queued: bool = False  # waiting in the queue, not yet delivered
    delivered: bool = False
    waiting: bool = False  # a send() is waiting for the result
    result: Result | None = None
    result_at: datetime | None = None


@dataclass(slots=True)
class _Agent:
    name: str
    last_contact: datetime | None = None  # any authenticated request
    report: Report | None = None
    report_at: datetime | None = None
    polling: bool = False
    reporting: bool = False
    queue: deque[str] = field(default_factory=deque)  # action ids to deliver
    pending: dict[str, _Pending] = field(default_factory=dict)  # by action id
    by_key: dict[str, str] = field(default_factory=dict)  # action key -> id


@dataclass(frozen=True, slots=True)
class AgentView:
    """What the hub knows about one agent, for the merge and for metrics."""

    name: str
    last_contact: datetime | None
    report: Report | None
    report_at: datetime | None  # when the hub accepted ``report``


@dataclass(frozen=True, slots=True)
class LateResult:
    """A result that arrived after its action's wait gave up, and that no retry claimed."""

    agent: str
    action: WireAction
    result: Result


class Hub:
    def __init__(
        self,
        settings: HubSettings,
        tokens: Mapping[str, str],
        clock: Callable[[], datetime],
    ) -> None:
        self.settings = settings
        self._tokens = [(name, token.encode()) for name, token in tokens.items()]
        self._clock = clock
        self._agents = {a.name: _Agent(a.name) for a in settings.agents}
        self._cond = threading.Condition()
        self._closed = False

    # --- identity -------------------------------------------------------------------------

    def identify(self, authorization: str | None) -> str | None:
        """The agent whose token ``authorization`` (``Bearer <token>``) carries, or None.
        Every token is compared, in constant time."""
        if not authorization:
            return None
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() != "bearer":
            return None
        given = credentials.strip().encode()
        found = None
        for name, token in self._tokens:
            if hmac.compare_digest(given, token):
                found = name
        return found

    # --- agent side -----------------------------------------------------------------------

    def begin(self, agent: str, path: str) -> None:
        """Take the agent's slot for ``path`` (one report and one poll at a time); Busy
        when it is taken. Release it with :meth:`end`."""
        with self._cond:
            state = self._agents[agent]
            attr = "reporting" if path == REPORT_PATH else "polling"
            if getattr(state, attr):
                raise Busy(attr)
            setattr(state, attr, True)

    def end(self, agent: str, path: str) -> None:
        with self._cond:
            setattr(self._agents[agent], "reporting" if path == REPORT_PATH else "polling", False)

    def receive_report(self, agent: str, report: Report) -> None:
        """Keep ``report`` as the agent's latest; OutOfOrder unless it is newer."""
        now = self._clock()
        with self._cond:
            state = self._agents[agent]
            state.last_contact = now
            if state.report is not None and report.sent_at <= state.report.sent_at:
                raise OutOfOrder(report.sent_at.isoformat())
            state.report, state.report_at = report, now

    def poll(self, agent: str, results: list[Result], wait: float) -> list[WireAction]:
        """Store ``results``, then return the agent's queued actions, waiting up to ``wait``
        seconds for one. Each goes out with the time left to start it (``ttl``); one whose
        wait already gave up is not sent."""
        deadline = time.monotonic() + wait
        with self._cond:
            state = self._agents[agent]
            now = self._clock()
            state.last_contact = now
            for result in results:
                pending = state.pending.get(result.id)
                if pending is None or pending.result is not None:
                    log.info("agent %s: result for an unknown action %s", agent, result.id)
                    continue
                pending.result, pending.result_at = result, now
            if results:
                self._cond.notify_all()
            while not state.queue and not self._closed:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            out: list[WireAction] = []
            while state.queue and len(out) < MAX_ACTIONS:
                pending = state.pending.get(state.queue.popleft())
                if pending is None or not pending.queued:
                    continue
                pending.queued = False
                ttl = pending.deadline - time.monotonic()
                if ttl <= 0:
                    continue  # its wait gave up: withdrawn, never delivered
                pending.delivered = True
                out.append(replace(pending.action, ttl=min(ttl, MAX_TTL)))
            state.last_contact = max(state.last_contact or now, self._clock())
            return out

    # --- server side ----------------------------------------------------------------------

    def agents(self) -> list[AgentView]:
        with self._cond:
            return [
                AgentView(a.name, a.last_contact, a.report, a.report_at)
                for a in self._agents.values()
            ]

    def send(self, agent: str, action: WireAction, timeout: float) -> Result:
        """Queue ``action`` (its ``id`` is assigned here) and wait up to ``timeout`` seconds
        for the agent's result.

        Raises :class:`AgentUnavailable` when the result does not come in time. An action
        not yet delivered then is withdrawn: it was not carried out. One delivered stays
        pending under its id: the same action sent again reuses the id (the agent answers
        from memory instead of running it twice), and a late result answers that retry, or
        :meth:`late_results` hands it over."""
        now = self._clock()
        key = _key(action)
        with self._cond:
            state = self._agents[agent]
            self._expire(state, now)
            action_id = state.by_key.get(key)
            pending = state.pending.get(action_id) if action_id else None
            if pending is None:
                action_id = uuid.uuid4().hex
                pending = _Pending(replace(action, id=action_id), now)
                state.pending[action_id] = pending
                state.by_key[key] = action_id
            assert action_id is not None
            pending.deadline = time.monotonic() + timeout
            if pending.result is None and not pending.queued:
                pending.queued = True
                state.queue.append(action_id)
                self._cond.notify_all()
            pending.waiting = True
            try:
                while pending.result is None and not self._closed:
                    left = pending.deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._cond.wait(left)
            finally:
                pending.waiting = False
            if pending.result is None:
                if not pending.delivered:
                    pending.queued = False  # withdrawn: poll skips it
                    self._forget(state, action_id)
                    raise AgentUnavailable(
                        f"agent {agent} did not pick it up within {timeout:g}s; not carried out"
                    )
                raise AgentUnavailable(
                    f"agent {agent} did not confirm within {timeout:g}s; it may still carry it out"
                )
            self._forget(state, action_id)
            return pending.result

    def late_results(self, older_than: timedelta) -> list[LateResult]:
        """Results that arrived with no send() waiting and that no retry claimed within
        ``older_than``; each is returned once."""
        now = self._clock()
        out: list[LateResult] = []
        with self._cond:
            for state in self._agents.values():
                for action_id, p in list(state.pending.items()):
                    if p.result is None or p.waiting or p.result_at is None:
                        continue
                    if now - p.result_at >= older_than:
                        self._forget(state, action_id)
                        out.append(LateResult(state.name, p.action, p.result))
        return out

    def _forget(self, state: _Agent, action_id: str) -> None:
        pending = state.pending.pop(action_id, None)
        if pending is not None and state.by_key.get(_key(pending.action)) == action_id:
            del state.by_key[_key(pending.action)]

    def _expire(self, state: _Agent, now: datetime) -> None:
        for action_id, p in list(state.pending.items()):
            if now - p.created > PENDING_TTL and not p.waiting:
                self._forget(state, action_id)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()


def _key(action: WireAction) -> str:
    """What makes two actions the same: adapter, kind, target and params (not the reason,
    whose wording may change between ticks)."""
    return json.dumps(
        [action.adapter, str(action.kind), action.task_id, action.params],
        sort_keys=True,
        default=str,
    )


# --- HTTP -------------------------------------------------------------------------------


class HubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, hub: Hub) -> None:
        settings = hub.settings
        self.hub = hub
        self.tls = _server_context(settings) if settings.tls else None
        self.connections = threading.BoundedSemaphore(settings.max_connections)
        super().__init__((settings.host, settings.port), HubHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Over the cap a connection is closed before a thread is started for it.
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def get_request(self) -> tuple[Any, Any]:
        sock, address = self.socket.accept()
        if self.tls is not None:
            # The handshake runs in the request's thread (HubHandler.setup), under its socket
            # timeout: a client that stalls it cannot hold up accept().
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.info("hub: connection from %s failed", client_address[0], exc_info=True)

    def shutdown(self) -> None:
        self.hub.close()  # release held polls first
        super().shutdown()


def _server_context(settings: HubSettings) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(settings.tls_cert), str(settings.tls_key))
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"[hub] cannot load tls_cert/tls_key: {exc}") from None
    return context


class HubHandler(BaseHTTPRequestHandler):
    server: HubServer
    timeout = 30  # per socket read, the TLS handshake included
    body_deadline = 60.0

    def setup(self) -> None:
        self.request.settimeout(self.timeout)
        if isinstance(self.request, ssl.SSLSocket):
            self.request.do_handshake()
        super().setup()

    def do_GET(self) -> None:
        self.close_connection = True
        self._json(405, {"error": "use POST"})

    def do_POST(self) -> None:
        self.close_connection = True  # one request per connection
        path = self.path.split("?")[0]
        if path not in (REPORT_PATH, ACTIONS_PATH):
            self._json(404, {"error": "not found"})
            return
        hub = self.server.hub
        agent = hub.identify(self.headers.get("Authorization"))  # before reading anything
        if agent is None:
            log.warning("hub: refused a request without a valid token from %s", self._peer())
            self._json(401, {"error": "missing or invalid bearer token"})
            return
        try:
            hub.begin(agent, path)
        except Busy:
            log.warning(
                "hub: agent %s sent a second %s while one is in flight (two processes with "
                "one token?)",
                agent,
                path,
            )
            self._json(409, {"error": "a request of this kind is already in flight"})
            return
        try:
            self._handle(hub, agent, path)
        finally:
            hub.end(agent, path)

    def _handle(self, hub: Hub, agent: str, path: str) -> None:
        body = self._body(hub.settings.max_body_bytes)
        if body is None:
            return
        try:
            if path == REPORT_PATH:
                hub.receive_report(agent, decode_report(body))
                self._json(200, {})
            else:
                results, wait = decode_poll(body)
                actions = hub.poll(agent, results, wait)
                self._json(200, {"actions": [encode_action(a) for a in actions]})
        except ProtocolError as exc:
            log.warning("hub: agent %s sent an invalid %s: %s", agent, path, exc)
            self._json(400, {"error": str(exc)})
        except OutOfOrder:
            log.info("hub: agent %s sent a report older than the last one", agent)
            self._json(409, {"error": "report not newer than the last one accepted"})

    def _body(self, limit: int) -> Any:
        """The JSON body within the limits, or None after replying with the error."""
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self._json(411, {"error": "Content-Length required"})
            return None
        try:
            length = int(raw_length)
        except ValueError:
            length = -1
        if length < 0:
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length > limit:
            self._json(413, {"error": f"body over {limit} bytes"})
            return None
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        encoding = (self.headers.get("Content-Encoding") or "identity").strip().lower()
        if content_type != "application/json" or encoding not in ("identity", "gzip"):
            self._json(415, {"error": "expected application/json, identity or gzip"})
            return None
        try:
            data = self._read(length)
        except OSError as exc:
            log.info("hub: request from %s aborted: %s", self._peer(), exc)
            return None
        if len(data) != length:
            self._json(400, {"error": "body shorter than Content-Length"})
            return None
        if encoding == "gzip":
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            try:
                data = inflater.decompress(data, limit + 1)
            except zlib.error:
                self._json(400, {"error": "invalid gzip body"})
                return None
            if len(data) > limit or inflater.unconsumed_tail:
                self._json(413, {"error": f"body over {limit} bytes once decompressed"})
                return None
            if not inflater.eof:
                self._json(400, {"error": "truncated gzip body"})
                return None
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            self._json(400, {"error": "body is not valid JSON"})
            return None

    def _read(self, length: int) -> bytes:
        deadline = time.monotonic() + self.body_deadline
        chunks: list[bytes] = []
        left = length
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("request body not received within the deadline")
                self.connection.settimeout(min(self.timeout, remaining))
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                left -= len(chunk)
        finally:
            self.connection.settimeout(self.timeout)
        return b"".join(chunks)

    def _json(self, code: int, body: Mapping[str, Any]) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _peer(self) -> str:
        return str(self.client_address[0]) if self.client_address else "?"

    def log_message(self, format: str, *args: object) -> None:
        log.debug(format, *args)
