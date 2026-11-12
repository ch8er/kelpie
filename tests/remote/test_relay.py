"""OTLP through an agent: the loopback relay forwards exports to the hub under the agent's
token; the hub ingests them with every span in the agent's namespace."""

import http.client
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from kelpie.core.model import Span, SpanKind
from kelpie.remote.hub import Hub, HubServer
from kelpie.remote.relay import Relay
from kelpie.traces.ingest import Reply
from tests.remote.test_hub import NOW, OTHER, TOKEN, Clock, hub_settings


@dataclass
class Sink:
    calls: list[tuple[bytes, Callable[[Span], Span] | None, bool]] = field(default_factory=list)
    metric_calls: list[tuple[bytes, Callable[[str], str] | None, bool]] = field(
        default_factory=list
    )

    def handle(
        self, headers: Any, read: Any, *, authenticated: bool = False, rewrite: Any = None
    ) -> Reply:
        body = read(int(headers.get("Content-Length")))
        self.calls.append((body, rewrite, authenticated))
        return Reply(200, b"ok", "application/x-protobuf")

    def handle_metrics(
        self, headers: Any, read: Any, *, authenticated: bool = False, rename: Any = None
    ) -> Reply:
        body = read(int(headers.get("Content-Length")))
        self.metric_calls.append((body, rename, authenticated))
        return Reply(200, b"metrics ok", "application/x-protobuf")


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def hub_server(sink: Sink) -> Iterator[HubServer]:
    hub = Hub(hub_settings(), {"laptop": TOKEN, "vm-1": OTHER}, Clock())
    server = HubServer(hub, sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def send(
    port: int,
    body: bytes,
    token: str | None = None,
    path: str = "/v1/traces",
    **extra: str,
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/x-protobuf", **extra}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST", path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, data


def test_the_hub_namespaces_relayed_spans(hub_server: HubServer, sink: Sink) -> None:
    port = hub_server.server_address[1]
    assert send(port, b"spans")[0] == 401
    assert sink.calls == []
    assert send(port, b"spans", token=OTHER) == (200, b"ok")
    ((body, rewrite, authenticated),) = sink.calls
    assert body == b"spans" and authenticated and rewrite is not None
    span = rewrite(Span("tr", "sp", "x", SpanKind.TOOL, NOW, task_id="t_1", session_id="s"))
    assert (span.trace_id, span.task_id, span.session_id) == ("vm-1:tr", "vm-1:t_1", "vm-1:s")


def test_without_traces_the_hub_refuses_them() -> None:
    hub = Hub(hub_settings(), {"laptop": TOKEN, "vm-1": OTHER}, Clock())
    server = HubServer(hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert send(server.server_address[1], b"x", token=TOKEN)[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_relay_forwards_under_the_agents_token(hub_server: HubServer, sink: Sink) -> None:
    hub_url = f"http://127.0.0.1:{hub_server.server_address[1]}"
    relay = Relay(("127.0.0.1", 0), hub_url, TOKEN, max_body_bytes=1024)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    try:
        port = relay.server_address[1]
        assert send(port, b"export") == (200, b"ok")  # exporters send no token
        ((body, rewrite, _),) = sink.calls
        assert body == b"export"
        assert (
            rewrite is not None
            and rewrite(Span("t", "s", "x", SpanKind.LLM, NOW)).trace_id == "laptop:t"
        )
        assert send(port, b"x" * 2048)[0] == 413
    finally:
        relay.shutdown()
        relay.server_close()


def test_the_relay_answers_503_when_the_hub_is_away() -> None:
    relay = Relay(("127.0.0.1", 0), "http://127.0.0.1:9", TOKEN, max_body_bytes=1024, timeout=2)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    try:
        assert send(relay.server_address[1], b"export")[0] == 503
    finally:
        relay.shutdown()
        relay.server_close()


def test_relayed_spans_are_stored_in_the_agents_namespace(tmp_path: Any) -> None:
    from datetime import UTC, datetime

    from kelpie.core.spans import MemorySpanStore
    from kelpie.traces.ingest import TraceIngest
    from tests.traces.otlp_builders import S, request, sid

    now = datetime.now(UTC)
    store = MemorySpanStore()
    ingest = TraceIngest(store=store, clock=lambda: now)
    hub = Hub(hub_settings(), {"laptop": TOKEN, "vm-1": OTHER}, Clock())
    server = HubServer(hub, ingest)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = Relay(("127.0.0.1", 0), f"http://127.0.0.1:{server.server_address[1]}", TOKEN, 1 << 20)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    try:
        attrs = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "shell",
            "kelpie.task.id": "t_1a",
            "session.id": "s1",
        }
        body = request([S(sid(1), attrs=attrs, start=now)])
        status, _ = send(relay.server_address[1], body)
        assert status == 200
        (span,) = store.spans_since(datetime.min.replace(tzinfo=UTC))
        assert span.trace_id.startswith("laptop:") and span.span_id.startswith("laptop:")
        assert span.session_id in (None, "laptop:s1")
    finally:
        relay.shutdown()
        relay.server_close()
        server.shutdown()
        server.server_close()


def test_a_short_body_is_not_forwarded(hub_server: HubServer, sink: Sink) -> None:
    import socket

    relay = Relay(("127.0.0.1", 0), f"http://127.0.0.1:{hub_server.server_address[1]}", TOKEN, 1024)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    try:
        with socket.create_connection(("127.0.0.1", relay.server_address[1]), timeout=5) as s:
            s.sendall(
                b"POST /v1/traces HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 100\r\n\r\nshort"
            )
            s.shutdown(socket.SHUT_WR)
            assert s.recv(100).startswith(b"HTTP/1.0 400")
        assert sink.calls == []
    finally:
        relay.shutdown()
        relay.server_close()


def test_trace_exports_over_the_cap_are_told_to_retry(hub_server: HubServer) -> None:
    hub = hub_server.hub
    for _ in range(2):
        hub.enter("laptop", "traces")
    conn = http.client.HTTPConnection("127.0.0.1", hub_server.server_address[1], timeout=5)
    conn.request("POST", "/v1/traces", body=b"spans", headers={"Authorization": f"Bearer {TOKEN}"})
    response = conn.getresponse()
    assert (response.status, response.getheader("Retry-After")) == (503, "1")
    conn.close()


# --- Claude Code's metrics (/v1/metrics) ------------------------------------------------

METRICS = "/v1/metrics"


@pytest.fixture
def relay(hub_server: HubServer) -> Iterator[Relay]:
    hub_url = f"http://127.0.0.1:{hub_server.server_address[1]}"
    found = Relay(("127.0.0.1", 0), hub_url, TOKEN, max_body_bytes=1024)
    threading.Thread(target=found.serve_forever, daemon=True).start()
    yield found
    found.shutdown()
    found.server_close()


def test_the_hub_namespaces_relayed_metrics(hub_server: HubServer, sink: Sink) -> None:
    port = hub_server.server_address[1]
    assert send(port, b"points", path=METRICS)[0] == 401
    assert send(port, b"points", token="not-a-token-" * 4, path=METRICS)[0] == 401
    assert sink.metric_calls == [] and sink.calls == []
    assert send(port, b"points", token=OTHER, path=METRICS) == (200, b"metrics ok")
    ((body, rename, authenticated),) = sink.metric_calls
    assert body == b"points" and authenticated and rename is not None
    assert rename("s-1") == "vm-1:s-1"
    assert sink.calls == []  # not taken as spans


def test_without_traces_the_hub_refuses_metrics() -> None:
    hub = Hub(hub_settings(), {"laptop": TOKEN, "vm-1": OTHER}, Clock())
    server = HubServer(hub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        assert send(server.server_address[1], b"x", token=TOKEN, path=METRICS)[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_relay_forwards_metrics_under_the_agents_token(relay: Relay, sink: Sink) -> None:
    port = relay.server_address[1]
    assert send(port, b"points", path=METRICS) == (200, b"metrics ok")
    ((body, rename, _),) = sink.metric_calls
    assert body == b"points" and rename is not None and rename("s") == "laptop:s"
    assert sink.calls == []


def test_the_relay_guards_metrics_like_spans(relay: Relay, sink: Sink) -> None:
    port = relay.server_address[1]
    assert send(port, b"x" * 2048, path=METRICS)[0] == 413
    assert send(port, b"points", path=METRICS, Origin="https://example.org")[0] == 403
    assert send(port, b"points", path=METRICS, Host="attacker.example.org")[0] == 421
    assert send(port, b"points", path="/v1/logs")[0] == 404
    assert send(port, b"points", path="/v1/metrics/../traces")[0] == 404
    assert sink.metric_calls == [] and sink.calls == []


def test_metric_exports_over_the_cap_are_told_to_retry(hub_server: HubServer, sink: Sink) -> None:
    """A flood of metric exports from one agent: two in flight, the rest get 503 with
    Retry-After (OTLP exporters retry), and trace exports keep their own slots."""
    hub = hub_server.hub
    for _ in range(2):
        hub.enter("laptop", "metrics")
    try:
        port = hub_server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", METRICS, body=b"points", headers={"Authorization": f"Bearer {TOKEN}"})
        response = conn.getresponse()
        assert (response.status, response.getheader("Retry-After")) == (503, "1")
        conn.close()
        assert sink.metric_calls == []
        assert send(port, b"spans", token=TOKEN) == (200, b"ok")  # spans are not starved
        assert send(port, b"points", token=OTHER, path=METRICS)[0] == 200  # nor other agents
    finally:
        for _ in range(2):
            hub.leave("laptop", "metrics")


def test_relayed_metrics_are_stored_in_the_agents_namespace(tmp_path: Any) -> None:
    """End to end: Claude Code -> relay -> hub -> ingest -> store. A point naming another
    host's session ("vm-1:s-1") stays in the sending agent's namespace."""
    import gzip
    import json
    from datetime import timedelta

    from kelpie.core.storage import SqliteSpanStore
    from kelpie.traces import metrics as m
    from kelpie.traces.ingest import TraceIngest
    from tests.traces.test_metrics_ingest import NOW as T
    from tests.traces.test_metrics_ingest import body, kv, metric, point

    store = SqliteSpanStore(tmp_path / "kelpie.db")
    ingest = TraceIngest(store=store, clock=lambda: T, max_body_bytes=64 * 1024)
    hub = Hub(hub_settings(), {"laptop": TOKEN, "vm-1": OTHER}, Clock())
    server = HubServer(hub, ingest)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    relay = Relay(("127.0.0.1", 0), f"http://127.0.0.1:{server.server_address[1]}", TOKEN, 1 << 20)
    threading.Thread(target=relay.serve_forever, daemon=True).start()
    try:
        pt = point(100, model="claude-x", type="input")
        export = body(
            metric(
                m.TOKENS, [{**pt, "attributes": [*pt["attributes"], kv("kelpie.task_id", "t_1")]}]
            ),
            metric(m.COST, [point(0.5, session="vm-1:s-1", model="claude-x")]),
        )
        port = relay.server_address[1]
        status, _ = send(port, export, path=METRICS, **{"Content-Type": "application/json"})
        assert status == 200
        # A gzip bomb is refused by ingest's own decompressed-size limit.
        bomb = gzip.compress(b"{" + b" " * (1 << 20) + b"}")
        status, _ = send(
            port,
            bomb,
            path=METRICS,
            **{"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )
        assert status == 413
        # Cumulative points stay refused through the hub, as on a single host.
        cumulative = body(metric(m.TOKENS, [point(1, type="input")], temporality=2))
        status, reply = send(port, cumulative, path=METRICS, **{"Content-Type": "application/json"})
        assert status == 200 and json.loads(reply)["partialSuccess"]["rejectedDataPoints"] in (
            1,
            "1",
        )
        rows = store._db.execute(
            "SELECT session_id, task_id, kind FROM metric_points ORDER BY kind"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("laptop:vm-1:s-1", None, "cost_usd"),
            ("laptop:s-1", "laptop:t_1", "input"),
        ]
        later = T + timedelta(minutes=1)
        [usages] = store.metered_usage_since(later, [timedelta(hours=1)])
        sessions = {u.session_id for u in usages}  # budgets join the namespaced sessions
        assert "laptop:s-1" in sessions and all(x.startswith("laptop:") for x in sessions)
    finally:
        relay.shutdown()
        relay.server_close()
        server.shutdown()
        server.server_close()
        store.close()
