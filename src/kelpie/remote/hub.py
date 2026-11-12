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
from collections import Counter, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from typing import Any, Protocol

from kelpie.core.actions import ActionKind
from kelpie.core.model import Span
from kelpie.httpguard import GuardedHandler, GuardedServer, Limits
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
    namespaced,
)

log = logging.getLogger(__name__)

# How long an unanswered action keeps its id (and a late result waits to be claimed).
PENDING_TTL = timedelta(hours=1)
MAX_REPLY_BYTES = 512 * 1024  # of a poll reply (the agent reads at most 1 MiB)
# Authenticated requests in flight per agent, by kind.
# Trace and metric exports share one budget ("otlp"): together they stay at half of ingest's
# decode slots at the default ``max_concurrent`` (4), so one agent cannot hold them all.
IN_FLIGHT = {"report": 1, "poll": 2, "otlp": 2}
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
    allowed: frozenset[str] = frozenset()  # adapter names taken from this agent
    refused: int = 0  # adapter parts refused, since the hub started
    refused_warned: float = -_LOG_EVERY  # monotonic time of the last warning about them
    refused_names: tuple[str, ...] = ()  # in the latest accepted report (at most 16)
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
    refused_adapters: int = 0  # adapter parts refused from its reports (audit M6)
    refused_names: tuple[str, ...] = ()  # those of its latest report


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
        tls: ssl.SSLContext | None = None,
    ) -> None:
        self.settings = settings
        self.tls = tls  # loaded once by whoever checked the config; else when listening
        self._tokens = [(name, token.encode()) for name, token in tokens.items()]
        self._clock = clock
        self._agents = {a.name: _Agent(a.name, a.adapters) for a in settings.agents}
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
        """Count a request of ``kind`` (report, poll, otlp: trace and metric exports
        together) in flight for the agent; Busy over :data:`IN_FLIGHT`. Authenticated
        requests are limited here, per agent, so one token cannot hold more than a few
        threads. Pair with :meth:`leave`."""
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
            state.report, state.report_at = self._allowed(state, report), now

    def _allowed(self, state: _Agent, report: Report) -> Report:
        """``report`` with only the adapters the agent may report (audit M6): Kelpie's own
        adapter names, narrowed by ``[[hub.agents]] adapters``. Others are refused, counted
        and logged at most once a minute, never merged, alerted or given an executor."""
        kept = tuple(p for p in report.adapters if p.name in state.allowed)
        refused = sorted(p.name for p in report.adapters if p.name not in state.allowed)
        state.refused_names = tuple(refused)
        if not refused:
            return report
        state.refused += len(refused)
        if time.monotonic() - state.refused_warned > _LOG_EVERY:
            state.refused_warned = time.monotonic()
            log.warning(
                "hub: agent %s reports adapters it may not (%s); refused, as %d so far "
                "(allowed: %s)",
                state.name,
                ", ".join(refused),
                state.refused,
                ", ".join(sorted(state.allowed)),
            )
        return replace(report, adapters=kept)

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
                AgentView(a.name, a.last_contact, a.report, a.report_at, a.refused, a.refused_names)
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
METRICS_PATH = "/v1/metrics"  # Claude Code's cost and token metrics (kelpie.traces.metrics)
OTLP_KINDS = {TRACES_PATH: "traces", METRICS_PATH: "metrics"}  # for logs and labels


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

    def handle_metrics(
        self,
        headers: Any,
        read: Callable[[int], bytes],
        *,
        authenticated: bool = False,
        rename: Callable[[str], str] | None = None,
    ) -> Any: ...


class HubServer(GuardedServer):
    """The hub's listener under :mod:`kelpie.httpguard`: a connection is pending until its
    token checks out (an agent sends it at once, a stranger holding slots does not); agents'
    own requests are then limited per agent."""

    name = "hub"

    def __init__(
        self, hub: Hub, traces: TraceSink | None = None, limits: Limits | None = None
    ) -> None:
        settings = hub.settings
        self.hub = hub
        self.traces = traces  # OTLP exports agents relay; None: refused (404)
        limits = limits or Limits()
        limits = replace(
            limits,
            max_connections=settings.max_connections,
            # Never more than the hub's own cap.
            max_per_address=min(limits.max_per_address, settings.max_connections),
        )
        tls = hub.tls or (server_context(settings) if settings.tls else None)
        super().__init__((settings.host, settings.port), HubHandler, limits, tls)

    def shutdown(self) -> None:
        self.hub.close()  # release held polls first
        super().shutdown()


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


def server_context(settings: HubSettings) -> ssl.SSLContext:
    """The hub's TLS context; ValueError when ``tls_cert``/``tls_key`` do not load."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(str(settings.tls_cert), str(settings.tls_key))
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"[hub] cannot load tls_cert/tls_key: {exc}") from None
    return context


class HubHandler(GuardedHandler):
    server: HubServer

    def do_GET(self) -> None:
        self.close_connection = True
        self._json(405, {"error": "use POST"})

    def do_POST(self) -> None:
        self.close_connection = True  # one request per connection
        path = self.path.split("?")[0]
        if path not in (REPORT_PATH, ACTIONS_PATH, TRACES_PATH, METRICS_PATH):
            self._json(404, {"error": "not found"})
            return
        hub = self.server.hub
        agent = hub.identify(self.headers.get("Authorization"))  # before reading anything
        if agent is None:
            peer = self.peer()
            self.server.warn_once(
                peer, "hub: refused a request without a valid token from %s", peer
            )
            self._json(401, {"error": "missing or invalid bearer token"})
            return
        self.admit()
        kinds = {REPORT_PATH: "report", ACTIONS_PATH: "poll", TRACES_PATH: "otlp"}
        kinds[METRICS_PATH] = "otlp"
        kind = kinds[path]
        try:
            hub.enter(agent, kind)
        except Busy:
            if path in OTLP_KINDS:  # exporters retry 503 (OTLP/HTTP), not 409
                self._json(503, {"error": "busy, retry later"}, {"Retry-After": "1"})
                return
            self.server.warn_once(
                agent,
                "hub: agent %s has too many %s requests in flight (two processes with one token?)",
                agent,
                kind,
            )
            self._json(409, {"error": f"too many {kind} requests in flight"})
            return
        try:
            if path in OTLP_KINDS:
                self._otlp(agent, path)
            else:
                self._handle(hub, agent, path)
        finally:
            hub.leave(agent, kind)

    def _otlp(self, agent: str, path: str) -> None:
        """An OTLP export the agent relayed (spans, or Claude Code's metrics): ingest's own
        limits apply, and every span, or every metric point's session and task, goes into
        the agent's namespace before it is stored. An agent can therefore only add to its
        own host's sessions, whatever ids its exporters send."""
        sink = self.server.traces
        kind = OTLP_KINDS[path]
        if sink is None:
            self._json(404, {"error": "OTLP ingest is not enabled on this server"})
            return
        try:
            if path == METRICS_PATH:
                reply = sink.handle_metrics(
                    self.headers,
                    self.read_body,
                    authenticated=True,
                    rename=partial(namespaced, agent),
                )
            else:
                reply = sink.handle(
                    self.headers,
                    self.read_body,
                    authenticated=True,
                    rewrite=partial(namespace_span, agent),
                )
        except OSError as exc:
            log.info("hub: %s export from agent %s aborted: %s", kind, agent, exc)
            return
        except Exception:
            log.exception("hub: %s ingest failed", kind)
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
            data = self.read_body(length)
        except OSError as exc:
            log.info("hub: request from %s aborted: %s", self.peer(), exc)
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

    def _json(
        self, code: int, body: Mapping[str, Any], headers: Mapping[str, str] | None = None
    ) -> None:
        data = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
