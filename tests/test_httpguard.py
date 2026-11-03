"""The connection guards of every ``kelpie serve`` listener (#304; audit #275 M5): slow headers,
slow bodies, too many connections, oversized bodies, and the ``[server]`` settings."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import ConfigError, load_settings
from kelpie.core.spans import MemorySpanStore
from kelpie.httpguard import GuardedServer, Limits, load_server
from kelpie.metrics import Metrics
from kelpie.server import KelpieHTTPServer, make_server
from kelpie.traces.ingest import TraceIngest
from tests.traces.otlp_builders import NOW

TOKEN = "t" * 32
FAST = Limits(max_connections=3, max_per_address=8, header_timeout=0.5, read_timeout=2.0)
# For the caps: no deadline cuts a connection during the test, only the caps do.
SLOW = Limits(max_connections=3, max_per_address=8, header_timeout=30.0)


def _aliases() -> bool:
    """127.0.0.2 and up are usable (Linux; macOS has only 127.0.0.1 without an alias)."""
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.2", 0))
    except OSError:
        return False
    return True


needs_aliases = pytest.mark.skipif(not _aliases(), reason="no 127.0.0.2 loopback alias")


def start(server: GuardedServer) -> Iterator[GuardedServer]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def serve(request: pytest.FixtureRequest) -> Iterator[KelpieHTTPServer]:
    limits = getattr(request, "param", FAST)
    ingest = TraceIngest(store=MemorySpanStore(), clock=lambda: NOW, token=TOKEN)
    ingest.max_body_bytes = 1024
    server = make_server("127.0.0.1", 0, Metrics(), ingest, limits)
    yield from start(server)  # type: ignore[misc]


def connect(server: GuardedServer, source: str = "127.0.0.1") -> socket.socket:
    sock = socket.create_connection(
        ("127.0.0.1", server.server_address[1]), timeout=5, source_address=(source, 0)
    )
    return sock


def wait_pending(server: GuardedServer, n: int, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while server.pending() != n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.pending() == n


def closed(sock: socket.socket, within: float | None = None) -> bool:
    """The server closed the connection (within ``within`` seconds, when given)."""
    if within is not None:
        sock.settimeout(within)
    try:
        return sock.recv(100) == b""
    except ConnectionResetError:
        return True
    except TimeoutError:
        return False


def head(length: int, token: bool = True) -> bytes:
    """An OTLP export's request line and headers."""
    auth = f"Authorization: Bearer {TOKEN}\r\n" if token else ""
    return (
        "POST /v1/traces HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
        f"{auth}Content-Length: {length}\r\n\r\n"
    ).encode()


def get(server: GuardedServer, path: str = "/healthz", source: str = "127.0.0.1") -> bytes:
    with connect(server, source) as sock:
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
        return data


def test_slow_headers_are_cut_at_the_header_deadline(serve: KelpieHTTPServer) -> None:
    with connect(serve) as sock:
        sock.sendall(b"GET /metrics HTTP/1.1\r\n")
        started = time.monotonic()
        for _ in range(30):  # a header byte every 0.1s: each read is quick, the whole is not
            try:
                sock.sendall(b"X")
            except OSError:
                break
            time.sleep(0.1)
        assert closed(sock)
        assert time.monotonic() - started < 2.5
    wait_pending(serve, 0)


def test_a_slow_request_line_holds_no_slot_past_the_deadline(serve: KelpieHTTPServer) -> None:
    with connect(serve) as sock:  # connects and sends nothing
        wait_pending(serve, 1)
        sock.settimeout(3)
        assert closed(sock)
    wait_pending(serve, 0)


def test_slow_body_is_cut_at_the_body_deadline(serve: KelpieHTTPServer) -> None:
    serve.limits = Limits(header_timeout=0.5, read_timeout=0.5, body_timeout=0.8)
    with connect(serve) as sock:
        sock.sendall(head(500))
        started = time.monotonic()
        for _ in range(30):  # a byte every 0.2s, under the read timeout
            time.sleep(0.2)
            try:
                sock.sendall(b" ")
            except OSError:
                break
        assert closed(sock)
        assert time.monotonic() - started < 3
    wait_pending(serve, 0)


def test_an_authorized_export_gives_its_slot_back(serve: KelpieHTTPServer) -> None:
    with connect(serve) as sock:
        sock.sendall(head(100) + b"{")
        wait_pending(serve, 0)  # its body is limited by ingest's own slots and deadline
        time.sleep(0.7)  # past the header deadline: an admitted export is not cut
        sock.sendall(b" " * 98 + b"}")
        assert b" 200 " in sock.recv(200)  # the whole body was read


def test_a_stranger_without_the_token_keeps_its_slot_until_answered(
    serve: KelpieHTTPServer,
) -> None:
    with connect(serve) as sock:
        sock.sendall(head(100, token=False))
        assert b" 401 " in sock.recv(200)


@needs_aliases
@pytest.mark.parametrize("serve", [SLOW], indirect=True)
def test_too_many_connections_cut_the_oldest_stranger(serve: KelpieHTTPServer) -> None:
    # Idle strangers from several addresses fill every pending slot...
    idle = [connect(serve, f"127.0.0.{i + 2}") for i in range(3)]
    wait_pending(serve, 3)
    # ...a scrape still gets in: the oldest stranger is cut to make room, the others stay.
    assert get(serve, "/metrics").startswith(b"HTTP/1.0 200")
    assert closed(idle[0], within=1.0)
    assert not closed(idle[1], within=0.2)
    assert not closed(idle[2], within=0.2)
    wait_pending(serve, 2)
    for sock in idle:
        sock.close()
    wait_pending(serve, 0)


@needs_aliases
@pytest.mark.parametrize("serve", [replace(SLOW, max_per_address=2)], indirect=True)
def test_one_address_gets_at_most_its_share(serve: KelpieHTTPServer) -> None:
    idle = [connect(serve) for _ in range(2)]
    wait_pending(serve, 2)
    with connect(serve) as extra:
        assert closed(extra, within=0.5)  # closed at accept, long before any deadline
    wait_pending(serve, 2)
    assert get(serve, source="127.0.0.9").endswith(b"ok\n")  # other addresses still get in
    assert not closed(idle[0], within=0.2)  # one slot was free: nobody was cut
    for sock in idle:
        sock.close()
    wait_pending(serve, 0)


@pytest.mark.parametrize("length", [1025, 10**12])
def test_an_oversized_body_is_refused_unread(serve: KelpieHTTPServer, length: int) -> None:
    with connect(serve) as sock:
        sock.sendall(head(length))
        assert b" 413 " in sock.recv(200)


class BigMetrics(Metrics):
    def render(self) -> str:
        return "#" * (32 * 1024 * 1024)  # more than any send and receive buffer


@needs_aliases
def test_a_reader_that_never_reads_cannot_hold_the_main_port() -> None:
    """GET /metrics, then a tiny receive window: the reply cannot be written, and the
    connection must stay evictable and be cut at the header deadline."""
    server = make_server("127.0.0.1", 0, BigMetrics(), None, replace(SLOW, header_timeout=4.0))
    for running in start(server):
        stalled = []
        for i in range(3):
            sock = socket.socket()
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            sock.bind((f"127.0.0.{i + 2}", 0))
            sock.connect(("127.0.0.1", running.server_address[1]))
            sock.sendall(b"GET /metrics HTTP/1.1\r\nHost: x\r\n\r\n")
            stalled.append(sock)
        wait_pending(running, 3)
        time.sleep(0.3)  # the replies are stuck in the write
        assert running.pending() == 3
        # Every slot is held by a stalled reader; a newcomer still gets in at once...
        started = time.monotonic()
        with connect(running, "127.0.0.9") as sock:
            sock.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            sock.settimeout(2)
            reply = b""
            while chunk := sock.recv(65536):
                reply += chunk
            assert reply.endswith(b"ok\n")
        assert time.monotonic() - started < 2
        # ...and the other readers lose their slots at the header deadline.
        wait_pending(running, 0, within=8.0)
        for sock in stalled:
            sock.close()


def test_the_hub_listener_takes_the_same_guards() -> None:
    from kelpie.remote.config import HubAgent, HubSettings
    from kelpie.remote.hub import Hub, HubServer

    settings = HubSettings(
        agents=(HubAgent("laptop", "KELPIE_AGENT_TOKEN"),),
        host="127.0.0.1",
        port=0,
        allow_plain_http=True,
        max_connections=9,
    )
    hub = Hub(settings, {"laptop": TOKEN}, lambda: NOW)
    server = HubServer(hub, None, Limits(header_timeout=0.5, max_per_address=4))
    for running in start(server):
        assert running.limits.max_connections == 9  # [hub] max_connections
        assert running.limits.max_per_address == 4  # [server]
        with connect(running) as sock:
            sock.settimeout(3)
            started = time.monotonic()
            assert closed(sock)
            assert time.monotonic() - started < 2
    clamped = HubServer(hub, None, Limits(max_connections=64, max_per_address=16))
    try:
        assert clamped.limits.max_per_address == 9  # never more than the hub's cap
    finally:
        clamped.server_close()


def test_server_section_defaults_and_bounds(tmp_path: Path) -> None:
    assert load_server(None) == Limits()
    assert load_server({"header_timeout": "10s", "max_connections": 200}) == Limits(
        max_connections=200, header_timeout=10.0
    )
    config = tmp_path / "kelpie.toml"
    config.write_text('[server]\nbody_timeout = "2m"\nmax_connections_per_address = 4\n')
    settings = load_settings(config)
    assert settings.server == Limits(max_per_address=4, body_timeout=120.0)


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"nope": 1}, "unknown"),
        ({"max_connections": 4}, "between 8 and 10000"),
        ({"max_connections": True}, "integer"),
        ({"max_connections_per_address": 0}, "between 1 and 10000"),
        ({"header_timeout": "0s"}, "between 1s and 1m"),
        ({"header_timeout": "soon"}, "duration"),
        ({"read_timeout": "10m"}, "between 1s and 5m"),
        ({"body_timeout": "1h"}, "between 1s and 10m"),
        ({"read_timeout": "40s", "body_timeout": "30s"}, "not be shorter"),
        ({"max_connections": 8, "max_connections_per_address": 9}, "must not exceed"),
    ],
)
def test_server_section_is_checked(section: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_server(section)


def test_a_bad_server_section_stops_kelpie(tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text("[server]\nmax_connections = 1\n")
    with pytest.raises(ConfigError, match=r"\[server\] max_connections"):
        load_settings(config)
