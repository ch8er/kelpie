"""DNS rebinding (#406): every loopback listener answers 421 to a request naming another
host; configured names and off-loopback binds keep working; a tokenless ingest refuses a
request from a web page."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from kelpie.app import load_settings
from kelpie.core.spans import MemorySpanStore
from kelpie.httpguard import GuardedServer, Limits, host_allowed, is_loopback, load_server
from kelpie.metrics import Metrics
from kelpie.remote.hub import Hub, HubServer
from kelpie.remote.relay import Relay
from kelpie.server import make_server
from kelpie.traces.ingest import TraceIngest
from tests.remote.test_hub import TOKEN as AGENT_TOKEN
from tests.remote.test_hub import Clock, hub_settings
from tests.traces.otlp_builders import NOW

TOKEN = "t" * 32
EXPORT = json.dumps({"resourceSpans": []}).encode()


def _ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def started(server: GuardedServer | Relay) -> Iterator[GuardedServer | Relay]:
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def request(
    server: GuardedServer | Relay,
    method: str,
    path: str,
    host: str | None,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    address = server.server_address
    with socket.create_connection((address[0], address[1]), timeout=5) as sock:
        lines = [f"{method} {path} HTTP/1.1"]
        if host is not None:
            lines.append(f"Host: {host}")
        for name, value in (headers or {}).items():
            lines.append(f"{name}: {value}")
        if method == "POST":
            lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + body)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, data.partition(b"\r\n\r\n")[2]


def main_port(host: str = "127.0.0.1", limits: Limits | None = None, token: str | None = None):
    store = MemorySpanStore()
    ingest = TraceIngest(store=store, clock=lambda: NOW, token=token)
    return make_server(host, 0, Metrics(), ingest, limits), store


@pytest.fixture
def loopback() -> Iterator[GuardedServer]:
    server, _ = main_port()
    yield from started(server)  # type: ignore[misc]


REBOUND = ["attacker.example:9464", "attacker.example", "127.0.0.1.attacker.example", "10.0.0.5"]
LOOPBACK = ["127.0.0.1:9464", "localhost:9464", "LOCALHOST", "127.0.0.9", "[::1]:9464", None]


@pytest.mark.parametrize("host", REBOUND)
@pytest.mark.parametrize("path", ["/metrics", "/healthz", "/nope"])
def test_main_port_refuses_a_rebound_name(loopback: GuardedServer, host: str, path: str) -> None:
    status, body = request(loopback, "GET", path, host)
    assert status == 421
    assert b"kelpie_" not in body  # nothing of the metrics leaks


@pytest.mark.parametrize("host", LOOPBACK)
def test_main_port_serves_loopback_names(loopback: GuardedServer, host: str | None) -> None:
    assert request(loopback, "GET", "/healthz", host) == (200, b"ok\n")


def test_a_rebound_page_cannot_write_spans_to_a_tokenless_ingest() -> None:
    server, _ = main_port()
    for _ in started(server):
        assert request(server, "POST", "/v1/traces", "attacker.example:9464", EXPORT)[0] == 421
        assert request(server, "POST", "/v1/traces", "127.0.0.1", EXPORT)[0] == 200


def test_a_tokenless_ingest_refuses_a_request_with_an_origin() -> None:
    server, _ = main_port()
    for _ in started(server):
        origin = {"Origin": "http://127.0.0.1:9464"}
        status, body = request(server, "POST", "/v1/traces", "127.0.0.1", EXPORT, origin)
        assert status == 403 and b"web page" in body


def test_with_a_token_an_origin_is_left_to_the_token() -> None:
    server, _ = main_port(token=TOKEN)
    for _ in started(server):
        headers = {"Origin": "http://x.example", "Authorization": f"Bearer {TOKEN}"}
        assert request(server, "POST", "/v1/traces", "127.0.0.1", EXPORT, headers)[0] == 200


@pytest.mark.skipif(not _ipv6(), reason="no IPv6 loopback")
def test_ipv6_loopback_listener_checks_the_host() -> None:
    server, _ = main_port("::1")
    for _ in started(server):
        assert request(server, "GET", "/healthz", "[::1]:9464")[0] == 200
        assert request(server, "GET", "/healthz", "localhost")[0] == 200
        assert request(server, "GET", "/healthz", "attacker.example")[0] == 421


def test_configured_names_are_served() -> None:
    server, _ = main_port(limits=Limits(hosts=("kelpie.internal", "box")))
    for _ in started(server):
        assert request(server, "GET", "/healthz", "kelpie.internal:9464")[0] == 200
        assert request(server, "GET", "/healthz", "BOX.")[0] == 200
        assert request(server, "GET", "/healthz", "kelpie.internal.attacker.example")[0] == 421


def test_a_listener_off_loopback_checks_no_host() -> None:
    server, _ = main_port("0.0.0.0")
    try:
        assert server.check_host is False
        assert server.host_ok("kelpie.example.net:9464") is True
    finally:
        server.server_close()


def test_the_bind_name_is_served() -> None:
    server, _ = main_port("localhost")
    try:
        assert server.check_host is True and server.host_ok("localhost:9464")
    finally:
        server.server_close()


def test_the_hub_on_loopback_checks_the_host() -> None:
    hub = Hub(hub_settings(), {"laptop": AGENT_TOKEN}, Clock())
    server = HubServer(hub, None)
    for _ in started(server):
        auth = {"Authorization": f"Bearer {AGENT_TOKEN}"}
        assert request(server, "POST", "/v1/traces", "attacker.example", b"", auth)[0] == 421
        assert request(server, "POST", "/v1/traces", "127.0.0.1", b"", auth)[0] == 404


def test_the_agent_relay_checks_the_host() -> None:
    relay = Relay(("127.0.0.1", 0), "http://127.0.0.1:9", AGENT_TOKEN, 1024, timeout=1)
    for _ in started(relay):
        assert request(relay, "POST", "/v1/traces", "attacker.example:4318", EXPORT)[0] == 421
        assert request(relay, "POST", "/v1/traces", "localhost:4318", EXPORT)[0] == 503
    named = Relay(("127.0.0.1", 0), "http://127.0.0.1:9", AGENT_TOKEN, 1024, 1, hosts=("otel",))
    for _ in started(named):
        assert request(named, "POST", "/v1/traces", "otel:4318", EXPORT)[0] == 503


@pytest.mark.parametrize(
    ("header", "bind", "names", "ok"),
    [
        ("127.0.0.1:9464", "127.0.0.1", (), True),
        ("[0:0:0:0:0:0:0:1]", "127.0.0.1", (), True),
        ("[::ffff:127.0.0.1]", "127.0.0.1", (), True),
        ("box:9464", "127.0.0.1", ("box",), True),
        ("box.attacker.example", "127.0.0.1", ("box",), False),
        ("", "127.0.0.1", (), False),
        ("localhost:abc", "127.0.0.1", (), False),
        ("[::1]evil.example", "127.0.0.1", (), False),
        ("0.0.0.0", "127.0.0.1", (), False),
    ],
)
def test_host_allowed_table(header: str, bind: str, names: tuple[str, ...], ok: bool) -> None:
    assert host_allowed(header, bind, names) is ok


@pytest.mark.parametrize(
    ("host", "loop"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("10.0.0.5", False),
        ("", False),
    ],
)
def test_is_loopback(host: str, loop: bool) -> None:
    assert is_loopback(host) is loop


def test_server_hosts_is_read_and_checked(tmp_path: Path) -> None:
    assert load_server({"hosts": ["Box.", "kelpie.internal", "box", "::1"]}).hosts == (
        "box",
        "kelpie.internal",
        "::1",
    )
    for bad in ("box", ["http://box"], ["box:9464"], [1], ["a b"], ["x"] * 33, [""]):
        with pytest.raises(ValueError, match=r"\[server\] hosts"):
            load_server({"hosts": bad})
    config = tmp_path / "kelpie.toml"
    config.write_text('[server]\nhosts = ["kelpie.internal"]\n')
    assert load_settings(config).server.hosts == ("kelpie.internal",)
