"""One connect deadline across all resolved addresses (audit L13): ``kelpie.net.connect`` and
the clients that use it (``kelpie.net`` requests, the GitHub client, trace forwarding, the
doctor's Prometheus check). No real network: fake resolution and sockets, or loopback."""

from __future__ import annotations

import socket
import time
import urllib.request
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest

from kelpie import net

ADDRS = [("192.0.2.1", 443), ("192.0.2.2", 443), ("2001:db8::1", 443)]  # documentation ranges


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def fake_network(
    monkeypatch: pytest.MonkeyPatch,
    clock: FakeClock,
    outcome: Callable[[tuple[Any, ...]], str],
    refuse_after: float = 0.1,
) -> list[tuple[tuple[Any, ...], float]]:
    """Resolve to :data:`ADDRS`; each attempt does what ``outcome(sockaddr)`` says:
    "blackhole" (waits out its timeout), "refuse" (fails fast) or "ok". Returns the
    attempts as (address, timeout)."""
    attempts: list[tuple[tuple[Any, ...], float]] = []

    def getaddrinfo(host: str, port: int, *args: object) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, p))
            for a, p in ADDRS
        ]

    class FakeSocket:
        def __init__(self, *args: object) -> None:
            self.timeout = 0.0
            self.closed = False

        def settimeout(self, value: float) -> None:
            self.timeout = value

        def bind(self, address: object) -> None:
            pass

        def connect(self, sockaddr: tuple[Any, ...]) -> None:
            attempts.append((sockaddr, self.timeout))
            what = outcome(sockaddr)
            if what == "blackhole":
                clock.now += self.timeout
                raise TimeoutError("timed out")
            if what == "refuse":
                clock.now += refuse_after
                raise ConnectionRefusedError(111, "refused")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(net.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(net.socket, "socket", FakeSocket)
    return attempts


def test_blackholed_addresses_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    attempts = fake_network(monkeypatch, clock, lambda a: "blackhole")
    start = clock.now
    with pytest.raises(TimeoutError):
        net.connect(("api.example.test", 443), start + 5.0, clock=clock)
    # socket.create_connection would wait 5 s per address: 15 s here.
    assert clock.now - start == pytest.approx(5.0)
    assert [t for _, t in attempts] == [pytest.approx(5.0)]


def test_each_address_gets_only_the_time_left(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    outcomes = {"192.0.2.1": "refuse", "192.0.2.2": "refuse", "2001:db8::1": "ok"}
    attempts = fake_network(monkeypatch, clock, lambda a: outcomes[a[0]], refuse_after=1.5)
    sock = net.connect(("api.example.test", 443), clock.now + 5.0, clock=clock)
    assert [a[0] for a, _ in attempts] == ["192.0.2.1", "192.0.2.2", "2001:db8::1"]
    assert [t for _, t in attempts] == [pytest.approx(5.0), pytest.approx(3.5), pytest.approx(2.0)]
    assert sock.timeout == pytest.approx(2.0)  # type: ignore[attr-defined]


def test_all_refused_raises_the_last_error(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    attempts = fake_network(monkeypatch, clock, lambda a: "refuse")
    with pytest.raises(ConnectionRefusedError):
        net.connect(("api.example.test", 443), clock.now + 5.0, clock=clock)
    assert len(attempts) == 3  # a fast failure still leaves time for the others


def test_a_passed_deadline_does_not_try_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    attempts = fake_network(monkeypatch, clock, lambda a: "ok")
    with pytest.raises(TimeoutError):
        net.connect(("api.example.test", 443), clock.now, clock=clock)
    assert attempts == []


def test_no_address_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(net.socket, "getaddrinfo", lambda *a: [])
    with pytest.raises(OSError, match="no address"):
        net.connect(("api.example.test", 443), time.monotonic() + 5)


def test_connects_on_loopback() -> None:
    srv = socket.create_server(("127.0.0.1", 0))
    try:
        sock = net.connect(("127.0.0.1", srv.getsockname()[1]), time.monotonic() + 5)
        sock.close()
    finally:
        srv.close()


def test_create_connection_turns_the_timeout_into_a_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, ...]] = []

    def record(address: object, deadline: float, source: object = None) -> socket.socket:
        seen.append((address, deadline - time.monotonic()))
        raise ConnectionRefusedError

    monkeypatch.setattr(net, "connect", record)
    with pytest.raises(ConnectionRefusedError):
        net.create_connection(("h.example.test", 80), 7.0)
    assert seen[0][0] == ("h.example.test", 80)
    assert 6.0 < seen[0][1] <= 7.0  # type: ignore[operator]


def test_create_connection_without_a_timeout_is_the_stdlib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[object, ...]] = []
    monkeypatch.setattr(net.socket, "create_connection", lambda *a, **k: seen.append(a) or "s")
    monkeypatch.setattr(net, "connect", lambda *a, **k: pytest.fail("not with a deadline"))
    assert net.create_connection(("h.example.test", 80), None) == "s"
    assert seen == [(("h.example.test", 80), None, None)]


# --- the clients ---------------------------------------------------------------------------


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the time each connect is given (deadline - now) and refuse it."""
    seen: list[float] = []

    def record(
        address: object,
        deadline: float,
        source: object = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> socket.socket:
        seen.append(deadline - clock())
        raise ConnectionRefusedError

    monkeypatch.setattr(net, "connect", record)
    return seen


def test_net_requests_connect_through_the_deadline(connects: list[float]) -> None:
    with pytest.raises(net.RequestError) as err:
        net.get("http://127.0.0.1:9", "/", {}, timeout=4.0)
    assert err.value.cause == "connection"
    assert len(connects) == 1 and 3.0 < connects[0] <= 4.0


def test_the_github_client_connects_through_the_deadline(connects: list[float]) -> None:
    from kelpie.connectors.github.client import GitHubClient, GitHubError

    gh = GitHubClient("http://127.0.0.1:9", None, timeout=timedelta(seconds=4))
    with pytest.raises(GitHubError) as err:
        gh.get("/repos/example/repo/pulls/1")
    assert err.value.kind == "network"
    assert len(connects) == 1 and 3.0 < connects[0] <= 4.0


def test_trace_forwarding_connects_through_the_deadline(connects: list[float]) -> None:
    from kelpie.traces.forward import Destination, Export, SendError, send_http

    with pytest.raises(SendError):
        send_http(Destination("d", "http://127.0.0.1:9/v1/traces", timeout=4.0), Export(b"", "x"))
    assert len(connects) == 1 and 3.0 < connects[0] <= 4.0


def test_the_handlers_replace_the_defaults_and_open_deadline_connections() -> None:
    opener = urllib.request.build_opener(net.DeadlineHTTPHandler, net.DeadlineHTTPSHandler)
    kinds = {type(h) for h in opener.handlers}
    assert {net.DeadlineHTTPHandler, net.DeadlineHTTPSHandler} <= kinds
    assert urllib.request.HTTPHandler not in kinds and urllib.request.HTTPSHandler not in kinds
    for conn_class in (net.DeadlineHTTPConnection, net.DeadlineHTTPSConnection):
        conn = conn_class("h.example.test", timeout=3)
        assert conn._create_connection is net.create_connection  # type: ignore[attr-defined]


def test_the_doctor_prometheus_check_connects_through_the_deadline(
    connects: list[float],
) -> None:
    from kelpie.doctor import check_prometheus

    check_prometheus("http://127.0.0.1:9", {})
    assert len(connects) == 1 and 2.0 < connects[0] <= 3.0
