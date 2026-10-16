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

    def handle(
        self, headers: Any, read: Any, *, authenticated: bool = False, rewrite: Any = None
    ) -> Reply:
        body = read(int(headers.get("Content-Length")))
        self.calls.append((body, rewrite, authenticated))
        return Reply(200, b"ok", "application/x-protobuf")


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


def send(port: int, body: bytes, token: str | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/x-protobuf"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST", "/v1/traces", body=body, headers=headers)
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
            s.sendall(b"POST /v1/traces HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nshort")
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
    status, _ = send(hub_server.server_address[1], b"spans", token=TOKEN)
    assert status == 503
