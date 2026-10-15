"""kelpie.net: the token-safe GET, against raw loopback servers (no real network)."""

import base64
import contextlib
import socket
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from kelpie import net

SECRET = "sk-net-test-SECRET"
Handler = Callable[[socket.socket, list[bytes]], None]


@contextmanager
def raw_server(handler: Handler, family: int = socket.AF_INET) -> Iterator[tuple[int, list[bytes]]]:
    """A loopback TCP server that hands each connection to ``handler``; yields its port and
    the bytes it received (one entry per connection)."""
    host = "::1" if family == socket.AF_INET6 else "127.0.0.1"
    srv = socket.socket(family, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, 0))
    srv.listen(8)
    seen: list[bytes] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handler, args=(conn, seen), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield srv.getsockname()[1], seen
    finally:
        srv.close()


def read_head(conn: socket.socket) -> bytes:
    data = b""
    conn.settimeout(5)
    with contextlib.suppress(OSError):
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
    return data


def answer(body: bytes = b"ok", head: bytes = b"HTTP/1.1 200 OK\r\n") -> Handler:
    def handle(conn: socket.socket, seen: list[bytes]) -> None:
        seen.append(read_head(conn))
        with contextlib.suppress(OSError):
            conn.sendall(head + b"Content-Length: %d\r\n\r\n" % len(body) + body)
        conn.close()

    return handle


def drip(first_read: bool = True) -> Handler:
    """Sends one byte every 0.2s, forever: every single read is "in time"."""

    def handle(conn: socket.socket, seen: list[bytes]) -> None:
        with contextlib.suppress(OSError):
            seen.append(conn.recv(4096) if first_read else b"")
            for _ in range(100):
                conn.sendall(b"\x16")
                time.sleep(0.2)
        conn.close()

    return handle


@pytest.fixture
def no_env_proxy(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("no_proxy", "NO_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def set_proxy(monkeypatch: pytest.MonkeyPatch, url: str, no_proxy: str = "") -> None:
    for name in ("https_proxy", "HTTPS_PROXY"):
        monkeypatch.setenv(name, url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, no_proxy)


# --- header checks ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("Bearer abc-123_./+=", True),
        ("", False),
        ("a\nb", False),
        ("a\tb", False),
        ("café", False),
        ("a\x7f", False),
        ("a b", True),
    ],
)
def test_header_values_must_be_printable_ascii(value: str, ok: bool) -> None:
    assert net.valid_header_value(value) is ok


def test_invalid_headers_are_refused_before_connecting() -> None:
    with raw_server(answer()) as (port, seen):
        for headers in (
            {"Authorization": f"Bearer {SECRET}\r\nX: 1"},
            {"Bad Name": "x"},
            {"X-Key": f"{SECRET}é"},
        ):
            with pytest.raises(net.RequestError) as caught:
                net.get(f"http://127.0.0.1:{port}", "/", headers, 1.0)
            assert caught.value.cause == "invalid_header"
            assert SECRET not in str(caught.value)
        time.sleep(0.05)
        assert seen == []


@pytest.mark.parametrize(
    "headers",
    [{"Host": "evil.example"}, {"User-Agent": "a", "user-agent": "b"}, {"Content-Length": "5"}],
)
def test_reserved_or_repeated_headers_are_refused(headers: dict[str, str]) -> None:
    with pytest.raises(net.RequestError) as caught:
        net.get("http://127.0.0.1:9", "/", headers, 1.0)
    assert caught.value.cause == "invalid_header"


@pytest.mark.parametrize("path", ["", "x", "//evil.example/x", "/a b", "/a#b", "/a\nb", "/\u00e9"])
def test_paths_must_stay_on_the_origin(path: str) -> None:
    with pytest.raises(net.RequestError) as caught:
        net.get("http://127.0.0.1:9", path, {}, 1.0)
    assert caught.value.cause == "invalid_request"


def test_an_invalid_port_is_a_request_error() -> None:
    with pytest.raises(net.RequestError) as caught:
        net.get("http://127.0.0.1:99999", "/", {}, 1.0)
    assert caught.value.cause == "invalid_request"


# --- plain requests ---------------------------------------------------------------------


def test_status_headers_and_body(no_env_proxy: pytest.MonkeyPatch) -> None:
    head = b"HTTP/1.1 200 OK\r\nX-A: 1\r\nx-a: 2\r\n"
    with raw_server(answer(b"hello", head)) as (port, seen):
        response = net.get(f"http://127.0.0.1:{port}", "/p?q=1", {"X-Key": SECRET}, 2.0)
    assert (response.status, response.body) == (200, b"hello")
    assert response.headers["x-a"] == "1"  # the first of repeated headers wins
    assert seen[0].startswith(b"GET /p?q=1 HTTP/1.1\r\n")


def test_the_body_is_skipped_on_request_and_for_errors() -> None:
    with raw_server(answer(b"hello")) as (port, _):
        assert net.get(f"http://127.0.0.1:{port}", "/", {}, 2.0, read_body=False).body == b""
    with raw_server(answer(b"nope", b"HTTP/1.1 500 Oops\r\n")) as (port, _):
        response = net.get(f"http://127.0.0.1:{port}", "/", {}, 2.0)
    assert (response.status, response.body) == (500, b"")


def test_redirects_are_errors() -> None:
    head = b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/\r\n"
    with raw_server(answer(b"", head)) as (port, _), pytest.raises(net.RequestError) as caught:
        net.get(f"http://127.0.0.1:{port}", "/", {}, 2.0)
    assert caught.value.cause == "redirect"


def test_the_body_cap() -> None:
    with (
        raw_server(answer(b"x" * 10_000)) as (port, _),
        pytest.raises(net.RequestError) as caught,
    ):
        net.get(f"http://127.0.0.1:{port}", "/", {}, 2.0, max_bytes=5000)
    assert caught.value.cause == "too_large"


def test_an_ipv6_literal_keeps_its_port() -> None:
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    try:
        cm = raw_server(answer(b"six"), socket.AF_INET6)
        port, _ = cm.__enter__()
    except OSError:
        pytest.skip("no IPv6 loopback")
    try:
        assert net.get(f"http://[::1]:{port}", "/", {}, 2.0).body == b"six"
    finally:
        cm.__exit__(None, None, None)


# --- the deadline -----------------------------------------------------------------------


def test_a_dripped_tls_handshake_is_cut_at_the_deadline(no_env_proxy: pytest.MonkeyPatch) -> None:
    with raw_server(drip()) as (port, _):
        started = time.monotonic()
        with pytest.raises(net.RequestError) as caught:
            net.get(f"https://127.0.0.1:{port}", "/", {"X-Key": SECRET}, 0.6)
        elapsed = time.monotonic() - started
    assert caught.value.cause == "timeout"
    assert elapsed < 2.0


def test_a_dripped_proxy_answer_is_cut_at_the_deadline(no_env_proxy: pytest.MonkeyPatch) -> None:
    with raw_server(drip()) as (port, _):
        set_proxy(no_env_proxy, f"http://127.0.0.1:{port}")
        started = time.monotonic()
        with pytest.raises(net.RequestError) as caught:
            net.get("https://provider.example.test", "/", {}, 0.6, proxy=True)
        elapsed = time.monotonic() - started
    assert caught.value.cause == "timeout"
    assert elapsed < 2.0


# --- proxies ----------------------------------------------------------------------------


def test_https_goes_through_the_environment_proxy_as_a_tunnel(
    no_env_proxy: pytest.MonkeyPatch,
) -> None:
    refuse = answer(b"", b"HTTP/1.1 407 Proxy Authentication Required\r\n")
    with raw_server(refuse) as (port, seen):
        set_proxy(no_env_proxy, f"http://user:p%40ss@127.0.0.1:{port}")
        with pytest.raises(net.RequestError) as caught:
            net.get(
                "https://provider.example.test:8443",
                "/v1/models",
                {"Authorization": f"Bearer {SECRET}"},
                2.0,
                proxy=True,
            )
    assert caught.value.cause == "connection"
    (request,) = seen
    assert request.startswith(b"CONNECT provider.example.test:8443 HTTP/1.1\r\n")
    creds = base64.b64encode(b"user:p@ss")
    assert b"Proxy-Authorization: Basic " + creds in request
    assert SECRET.encode() not in request and b"/v1/models" not in request


def test_the_default_port_and_no_credentials(no_env_proxy: pytest.MonkeyPatch) -> None:
    refuse = answer(b"", b"HTTP/1.1 403 Forbidden\r\n")
    with raw_server(refuse) as (port, seen):
        set_proxy(no_env_proxy, f"127.0.0.1:{port}")  # no scheme: http
        with pytest.raises(net.RequestError):
            net.get("https://provider.example.test", "/", {}, 2.0, proxy=True)
    assert seen[0].startswith(b"CONNECT provider.example.test:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization" not in seen[0]


@pytest.mark.parametrize(
    ("url", "proxy", "no_proxy"),
    [
        ("http://127.0.0.1:9", True, ""),  # plain http: never proxied
        ("https://127.0.0.1:9", False, ""),  # proxy not allowed by the caller
        ("https://127.0.0.1:9", True, "127.0.0.1"),  # no_proxy
    ],
)
def test_when_the_proxy_is_not_used(
    no_env_proxy: pytest.MonkeyPatch, url: str, proxy: bool, no_proxy: str
) -> None:
    with raw_server(answer()) as (port, seen):
        set_proxy(no_env_proxy, f"http://127.0.0.1:{port}", no_proxy)
        with pytest.raises(net.RequestError) as caught:
            net.get(url, "/", {"Authorization": f"Bearer {SECRET}"}, 1.0, proxy=proxy)
        time.sleep(0.05)
        assert seen == []
    assert caught.value.cause == "connection"


@pytest.mark.parametrize("proxy_url", ["https://127.0.0.1:3128", "http://127.0.0.1:0", "http://"])
def test_unusable_proxies_are_ignored(no_env_proxy: pytest.MonkeyPatch, proxy_url: str) -> None:
    set_proxy(no_env_proxy, proxy_url)
    assert net.https_proxy("provider.example.test") is None


@pytest.mark.parametrize(
    ("url", "port"),
    [("http://[::1]:8080", 8080), ("http://[::1]", 80), ("https://[::1]", 443)],
)
def test_ipv6_literals_get_an_explicit_port(
    no_env_proxy: pytest.MonkeyPatch, url: str, port: int
) -> None:
    conn = net._connection(urllib.parse.urlsplit(url), 1.0, proxy=True)
    assert (conn.host, conn.port) == ("::1", port)
