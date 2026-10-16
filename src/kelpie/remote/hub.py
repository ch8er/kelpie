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

import contextlib
import hmac
import ipaddress
import json
import logging
import socket
import ssl
import sys
import threading
import time
import uuid
import zlib
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from kelpie.core.actions import ActionKind
from kelpie.core.model import Span
from kelpie.remote.config import HubSettings
from kelpie.remote.protocol import (
    ACTIONS_PATH,
    MAX_ACTIONS,
    MAX_TTL,
    REPORT_PATH,
    Outcome,
    ProtocolError,
    Report,
    Result,
    WireAction,
    decode_poll,
    decode_report,
    depth_ok,
    encode_action,
    namespace_span,
)

log = logging.getLogger(__name__)

# How long an unanswered action keeps its id (and a late result waits to be claimed).
PENDING_TTL = timedelta(hours=1)
MAX_REPLY_BYTES = 512 * 1024  # of a poll reply (the agent reads at most 1 MiB)
PREAUTH_DEADLINE = 3.0  # seconds from accept to a checked token; then the socket is shut
MAX_PER_PEER = 16  # connections from one address (an IPv6 /64) not yet authenticated
# Authenticated requests in flight per agent, by kind.
IN_FLIGHT = {"report": 1, "poll": 2, "traces": 2}
MAX_POLL_BYTES = 2 * 1024 * 1024  # a poll carries results only
_LOG_EVERY = 60.0  # seconds between refusal warnings per peer


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
    waiting: int = 0  # send() calls waiting for the result
    result: Result | None = None
    result_at: datetime | None = None


@dataclass(slots=True)
class _Agent:
    name: str
    last_contact: datetime | None = None  # any authenticated request
    report: Report | None = None
    report_at: datetime | None = None
    polling: int = 0  # generation of the poll being held; 0: none
    poll_gen: int = 0
    warned: float = -_LOG_EVERY  # monotonic time of the last "polls again" warning
    in_flight: Counter[str] = field(default_factory=Counter)  # requests by kind
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

    def enter(self, agent: str, kind: str) -> None:
        """Count a request of ``kind`` (report, poll, traces) in flight for the agent; Busy
        over :data:`IN_FLIGHT`. Authenticated requests are limited here, per agent, so one
        token cannot hold more than a few threads. Pair with :meth:`leave`."""
        with self._cond:
            state = self._agents[agent]
            if state.in_flight[kind] >= IN_FLIGHT[kind]:
                raise Busy(kind)
            state.in_flight[kind] += 1

    def leave(self, agent: str, kind: str) -> None:
        with self._cond:
            self._agents[agent].in_flight[kind] -= 1

    def begin_poll(self, agent: str) -> int:
        """Hold the agent's poll slot: a new poll (its body already read) replaces a held
        one, whose connection may be dead; the held one then returns at once with nothing.
        Returns the generation for :meth:`poll` and :meth:`end_poll`."""
        with self._cond:
            state = self._agents[agent]
            if state.polling and time.monotonic() - state.warned > _LOG_EVERY:
                state.warned = time.monotonic()
                log.warning(
                    "hub: agent %s polls again while a poll is held; the new one replaces it "
                    "(if this repeats, two processes use the agent's token)",
                    agent,
                )
            state.poll_gen += 1
            state.polling = state.poll_gen
            self._cond.notify_all()
            return state.poll_gen

    def end_poll(self, agent: str, generation: int) -> None:
        with self._cond:
            state = self._agents[agent]
            if state.polling == generation:
                state.polling = 0

    def undeliver(self, agent: str, actions: list[WireAction]) -> None:
        """Put actions whose reply could not be written back at the front of the queue."""
        with self._cond:
            state = self._agents[agent]
            for action in reversed(actions):
                pending = state.pending.get(action.id)
                if pending is not None and pending.result is None and not pending.queued:
                    pending.queued, pending.delivered = True, False
                    state.queue.appendleft(action.id)
            self._cond.notify_all()

    def receive_report(self, agent: str, report: Report) -> None:
        """Keep ``report`` as the agent's latest; OutOfOrder when the same agent process
        (``boot``) already sent a later one. Ordering never uses the agent's clock."""
        now = self._clock()
        with self._cond:
            state = self._agents[agent]
            state.last_contact = now
            last = state.report
            if last is not None and report.boot == last.boot and report.seq <= last.seq:
                raise OutOfOrder(f"{report.boot}/{report.seq}")
            if (
                last is not None
                and report.boot != last.boot
                and state.report_at is not None
                and now - state.report_at < 2 * last.interval
                and time.monotonic() - state.warned > _LOG_EVERY
            ):
                state.warned = time.monotonic()
                log.warning(
                    "hub: agent %s reports from a new process while the old one reported %ss "
                    "ago (if this repeats, two processes use the agent's token)",
                    agent,
                    int((now - state.report_at).total_seconds()),
                )
            state.report, state.report_at = report, now

    def poll(
        self, agent: str, results: list[Result], wait: float, generation: int = 0
    ) -> list[WireAction]:
        """Store ``results``, then return the agent's queued actions, waiting up to ``wait``
        seconds for one. Each goes out with the time left to start it (``ttl``); one whose
        wait already gave up is not sent. A poll replaced by a newer one (``generation``)
        returns nothing at once. Replies stay under :data:`MAX_REPLY_BYTES`."""
        deadline = time.monotonic() + wait
        with self._cond:
            state = self._agents[agent]
            now = self._clock()
            state.last_contact = now
            for result in results:
                self._store(state, result, now)
            if results:
                self._cond.notify_all()
            out: list[WireAction] = []
            size = 0
            while not self._closed and (not generation or state.polling == generation):
                while state.queue and len(out) < MAX_ACTIONS:
                    pending = state.pending.get(state.queue[0])
                    if pending is None or not pending.queued or pending.result is not None:
                        state.queue.popleft()
                        if pending is not None:
                            pending.queued = False
                        continue
                    ttl = pending.deadline - time.monotonic()
                    if ttl < 0.01:
                        state.queue.popleft()  # its wait gave up: withdrawn, not delivered
                        pending.queued = False
                        continue
                    action = replace(pending.action, ttl=min(ttl, MAX_TTL))
                    cost = len(json.dumps(encode_action(action)))
                    if out and size + cost > MAX_REPLY_BYTES:
                        break  # the rest goes with the next poll
                    state.queue.popleft()
                    pending.queued, pending.delivered = False, True
                    out.append(action)
                    size += cost
                left = deadline - time.monotonic()
                if out or left <= 0:
                    break
                self._cond.wait(left)
            state.last_contact = max(state.last_contact or now, self._clock())
            return out

    def _store(self, state: _Agent, result: Result, now: datetime) -> None:
        pending = state.pending.get(result.id)
        if pending is None or pending.result is not None:
            log.info("agent %s: result for an unknown action %s", state.name, result.id)
            return
        if result.outcome is Outcome.EXPIRED:
            # Not carried out: a waiting send gives up as for an action never picked up,
            # and a retry starts afresh instead of being answered with this.
            pending.delivered = False
            if not pending.waiting:
                self._forget(state, result.id)
            return
        pending.result, pending.result_at = result, now

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
            pending.waiting += 1
            try:
                while pending.result is None and not self._closed:
                    left = pending.deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._cond.wait(left)
            finally:
                pending.waiting -= 1
            if pending.result is None:
                if not pending.delivered:
                    pending.queued = False  # withdrawn
                    if action_id in state.queue:
                        state.queue.remove(action_id)
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
    """What makes two actions the same: rule, adapter, kind, target and params. The reason
    only for a comment, where it is what gets posted; elsewhere its wording may change
    between ticks for the same effect."""
    text = action.reason if action.kind is ActionKind.COMMENT else None
    return json.dumps(
        [action.rule, action.adapter, str(action.kind), action.task_id, action.params, text],
        sort_keys=True,
        default=str,
    )


# --- HTTP -------------------------------------------------------------------------------


TRACES_PATH = "/v1/traces"


class TraceSink(Protocol):
    """Trace ingest (``kelpie.traces.ingest.TraceIngest``) as the hub uses it."""

    def handle(
        self,
        headers: Any,
        read: Callable[[int], bytes],
        *,
        authenticated: bool = False,
        rewrite: Callable[[Span], Span] | None = None,
    ) -> Any: ...


class HubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, hub: Hub, traces: TraceSink | None = None) -> None:
        settings = hub.settings
        self.hub = hub
        self.traces = traces  # OTLP exports agents relay; None: refused (404)
        self.tls = _server_context(settings) if settings.tls else None
        if ":" in settings.host:
            self.address_family = socket.AF_INET6
        # Connections not yet authenticated: at most max_connections (when full, the oldest
        # is cut to make room: an agent sends its token at once, a stranger holding slots
        # does not), MAX_PER_PEER from one address. A connection gives its slot back once
        # its token checks out; agents' own requests are then limited per agent.
        self.max_connections = settings.max_connections
        self._slots_lock = threading.Lock()
        self._unauthenticated: dict[int, tuple[str, Any]] = {}  # id(socket) -> peer, socket
        self._per_peer: Counter[str] = Counter()
        self._warned: dict[str, float] = {}
        super().__init__((settings.host, settings.port), HubHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Over the per-peer cap a connection is closed before a thread is started for it.
        peer = _peer_key(str(client_address[0]))
        evicted = None
        with self._slots_lock:
            if self._per_peer[peer] >= MAX_PER_PEER:
                self.shutdown_request(request)
                return
            if len(self._unauthenticated) >= self.max_connections:
                oldest = next(iter(self._unauthenticated))
                old_peer, evicted = self._unauthenticated.pop(oldest)
                self._drop_peer(old_peer)
            self._unauthenticated[id(request)] = (peer, request)
            self._per_peer[peer] += 1
        if evicted is not None:
            _shut(evicted)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.release(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.release(request)

    def release(self, request: Any) -> None:
        """Give back the connection's unauthenticated slot (once)."""
        with self._slots_lock:
            entry = self._unauthenticated.pop(id(request), None)
            if entry is not None:
                self._drop_peer(entry[0])

    def _drop_peer(self, peer: str) -> None:
        self._per_peer[peer] -= 1
        if not self._per_peer[peer]:
            del self._per_peer[peer]

    def unauthenticated(self) -> int:
        with self._slots_lock:
            return len(self._unauthenticated)

    def warn_once(self, peer: str, message: str, *args: object) -> None:
        """Log a refusal at most once a minute per peer (a scanner must not flood the log)."""
        now = time.monotonic()
        with self._slots_lock:
            if now - self._warned.get(peer, -_LOG_EVERY) < _LOG_EVERY:
                return
            self._warned[peer] = now
            if len(self._warned) > 10_000:
                self._warned.clear()
        log.warning(message, *args)

    def get_request(self) -> tuple[Any, Any]:
        sock, address = self.socket.accept()
        if self.tls is not None:
            # The handshake runs in the request's thread (HubHandler.setup), under its socket
            # timeout: a client that stalls it cannot hold up accept().
            sock = self.tls.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)
        return sock, address

    def handle_error(self, request: Any, client_address: Any) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):  # the peer went away, a TLS handshake failed, a timeout
            log.debug("hub: connection from %s ended: %s", client_address[0], exc)
        else:
            log.exception("hub: request from %s failed", client_address[0])

    def shutdown(self) -> None:
        self.hub.close()  # release held polls first
        super().shutdown()


def _shut(sock: Any) -> None:
    """Shut a connection down under its handler, on the plain socket: an SSL socket's own
    shutdown would leave the handler mid-handshake with a broken object (not an OSError)."""
    with contextlib.suppress(OSError):
        socket.socket.shutdown(sock, socket.SHUT_RDWR)


def _peer_key(address: str) -> str:
    """Who a connection counts against: the address, or its /64 for IPv6 (one host holds a
    whole /64)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class HubService:
    """``kelpie serve``'s hub listener as a service: started with the control loop, stopped
    on exit (held polls are released first)."""

    def __init__(self, server: HubServer) -> None:
        self.server = server
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self.server.serve_forever, name="kelpie-hub", daemon=True
        )
        self._thread.start()
        settings = self.server.hub.settings
        log.info(
            "hub listening on %s://%s:%d for agents %s",
            "https" if settings.tls else "http",
            settings.host,
            self.server.server_address[1],
            ", ".join(a.name for a in settings.agents),
        )

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is not None:
            self.server.shutdown()
            self._thread.join(timeout)
        self.server.server_close()


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
        # Until its token checks out a connection has PREAUTH_DEADLINE in all, handshake,
        # request line and headers included, however slowly it sends them.
        self._guard = threading.Timer(PREAUTH_DEADLINE, self._cut)
        self._guard.daemon = True
        self._guard.start()
        try:
            self.request.settimeout(self.timeout)
            if isinstance(self.request, ssl.SSLSocket):
                self.request.do_handshake()
            super().setup()
        except BaseException:
            self._guard.cancel()  # finish() does not run when setup() fails
            raise

    def _cut(self) -> None:
        _shut(self.request)

    def finish(self) -> None:
        self._guard.cancel()
        with contextlib.suppress(OSError):
            super().finish()

    def do_GET(self) -> None:
        self.close_connection = True
        self._json(405, {"error": "use POST"})

    def do_POST(self) -> None:
        self.close_connection = True  # one request per connection
        path = self.path.split("?")[0]
        if path not in (REPORT_PATH, ACTIONS_PATH, TRACES_PATH):
            self._json(404, {"error": "not found"})
            return
        hub = self.server.hub
        agent = hub.identify(self.headers.get("Authorization"))  # before reading anything
        if agent is None:
            peer = self._peer()
            self.server.warn_once(
                peer, "hub: refused a request without a valid token from %s", peer
            )
            self._json(401, {"error": "missing or invalid bearer token"})
            return
        self._guard.cancel()
        self.server.release(self.request)
        kinds = {REPORT_PATH: "report", ACTIONS_PATH: "poll", TRACES_PATH: "traces"}
        kind = kinds[path]
        try:
            hub.enter(agent, kind)
        except Busy:
            self.server.warn_once(
                agent,
                "hub: agent %s has too many %s requests in flight (two processes with one token?)",
                agent,
                kind,
            )
            self._json(409, {"error": f"too many {kind} requests in flight"})
            return
        try:
            if path == TRACES_PATH:
                self._traces(agent)
            else:
                self._handle(hub, agent, path)
        finally:
            hub.leave(agent, kind)

    def _traces(self, agent: str) -> None:
        """An OTLP export the agent relayed: ingest's own limits apply, and every span goes
        into the agent's namespace before it is stored."""
        sink = self.server.traces
        if sink is None:
            self._json(404, {"error": "traces are not enabled on this server"})
            return
        try:
            reply = sink.handle(
                self.headers,
                self._read,
                authenticated=True,
                rewrite=partial(namespace_span, agent),
            )
        except OSError as exc:
            log.info("hub: trace export from agent %s aborted: %s", agent, exc)
            return
        except Exception:
            log.exception("hub: trace ingest failed")
            self._json(500, {"error": "internal error"})
            return
        self.send_response(reply.status)
        self.send_header("Content-Type", reply.content_type)
        self.send_header("Content-Length", str(len(reply.body)))
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(reply.body)

    def _handle(self, hub: Hub, agent: str, path: str) -> None:
        limit = hub.settings.max_body_bytes if path == REPORT_PATH else MAX_POLL_BYTES
        body = self._body(min(limit, hub.settings.max_body_bytes))
        if body is None:
            return
        try:
            if path == REPORT_PATH:
                hub.receive_report(agent, decode_report(body))
                self._json(200, {})
            else:
                results, wait = decode_poll(body)
                generation = hub.begin_poll(agent)  # only now: a stalled body replaces nothing
                try:
                    actions = hub.poll(agent, results, wait, generation)
                finally:
                    hub.end_poll(agent, generation)
                try:
                    self._json(200, {"actions": [encode_action(a) for a in actions]})
                except OSError:
                    hub.undeliver(agent, actions)  # the agent never got them
                    raise
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
            if not inflater.eof or inflater.unused_data:
                self._json(400, {"error": "truncated gzip body, or data after it"})
                return None
        try:
            body = json.loads(data)
        except (ValueError, RecursionError):
            self._json(400, {"error": "body is not valid JSON"})
            return None
        if not depth_ok(body):
            self._json(400, {"error": "body nests too deep"})
            return None
        return body

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
