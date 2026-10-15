"""GitHub client security and load behaviour, against a local fake API."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

import pytest

from kelpie.connectors.github.client import GitHubClient, GitHubError, check_api_url
from tests.connectors.fake_github import TOKEN, Clock, Recorder, serve

PR = "/repos/example/repo/pulls/1"


def client(url: str, clock: Clock, observer: Recorder | None = None, **kw: object) -> GitHubClient:
    return GitHubClient(url, TOKEN, clock=clock, observer=observer, **kw)  # type: ignore[arg-type]


def test_auth_header_sent_to_the_api_host_with_api_headers() -> None:
    with serve() as gh:
        gh.reply(PR, {"number": 1})
        response = client(gh.url, Clock()).get(PR)
    assert response.body == {"number": 1}
    (seen,) = gh.requests
    assert seen.headers["Authorization"] == f"Bearer {TOKEN}"
    assert seen.headers["Accept"] == "application/vnd.github+json"
    assert seen.headers["X-GitHub-Api-Version"]


def test_anonymous_client_sends_no_auth_header() -> None:
    with serve() as gh:
        gh.reply(PR, {"number": 1})
        GitHubClient(gh.url, None, clock=Clock()).get(PR)
    assert "Authorization" not in gh.requests[0].headers


def test_redirect_to_another_host_is_refused_and_never_followed() -> None:
    with serve() as other, serve() as gh:
        other.reply(PR, {"number": 1})
        gh.reply(PR, status=302, Location=f"{other.url}{PR}")
        with pytest.raises(GitHubError) as err:
            client(gh.url, Clock()).get(PR)
    assert err.value.kind == "redirect"
    assert other.requests == []  # the token never reached the other host


@pytest.mark.parametrize(
    "path",
    ["/repos/../x", "//evil.example/x", "/repos/a@evil/x", "repos/x", "/repos/x?y=1", "/a\r\nb"],
)
def test_paths_outside_the_api_are_refused(path: str) -> None:
    with serve() as gh, pytest.raises(GitHubError) as err:
        client(gh.url, Clock()).get(path)
    assert err.value.kind == "invalid_request"
    assert gh.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.test",  # plain http off loopback
        "https://user:pw@api.example.test",
        "https://api.example.test/?x=1",
        "ftp://api.example.test",
        "",
    ],
)
def test_bad_api_urls_are_refused(url: str) -> None:
    with pytest.raises(ValueError):
        check_api_url(url)


def test_api_url_with_path_is_kept() -> None:
    assert check_api_url("https://ghe.example.test/api/v3/") == "https://ghe.example.test/api/v3"
    assert check_api_url("http://localhost:8080") == "http://localhost:8080"


@pytest.mark.parametrize("token", ["bad\r\nX-Injected: 1", "has space", ""])
def test_token_must_be_a_valid_header_value(token: str) -> None:
    with pytest.raises(ValueError) as err:
        GitHubClient("https://api.example.test", token)
    assert token not in str(err.value) or not token


def test_token_absent_from_repr_logs_and_errors(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with serve() as gh:
        gh.reply(PR, {"message": "Bad credentials"}, status=401)
        c = client(gh.url, Clock())
        with pytest.raises(GitHubError) as err:
            c.get(PR)
    assert err.value.kind == "unauthorized"
    for text in (repr(c), str(err.value), repr(err.value), caplog.text):
        assert TOKEN not in text
    exc = err.value
    assert exc.__cause__ is None and (exc.__context__ is None or exc.__suppress_context__)


def test_not_found_and_forbidden() -> None:
    clock = Clock()
    with serve() as gh:
        gh.reply("/repos/example/repo/pulls/2", {"message": "nope"}, status=403)
        c = client(gh.url, clock)
        with pytest.raises(GitHubError) as missing:
            c.get(PR)
        with pytest.raises(GitHubError) as again:
            c.get(PR)  # the 404 is remembered for the cache TTL
        assert gh.paths() == [PR]
        clock.advance(seconds=61)
        gh.reply(PR, {"number": 1})
        assert c.get(PR).body == {"number": 1}
        assert "If-None-Match" not in gh.requests[-1].headers
        with pytest.raises(GitHubError) as forbidden:
            c.get("/repos/example/repo/pulls/2")
    assert missing.value.kind == again.value.kind == "not_found"
    assert missing.value.status == 404
    assert forbidden.value.kind == "forbidden"


def test_cache_within_ttl_then_conditional_request_with_etag() -> None:
    clock, rec = Clock(), Recorder()
    with serve() as gh:
        gh.reply(PR, {"number": 1}, ETag='W/"v1"')
        c = client(gh.url, clock, rec, cache_ttl=timedelta(seconds=60))
        first = c.get(PR)
        assert c.get(PR) == first  # within the TTL: no request
        assert len(gh.requests) == 1
        clock.advance(seconds=61)
        gh.reply(PR, status=304, ETag='W/"v1"')
        again = c.get(PR)
    assert gh.requests[1].headers["If-None-Match"] == 'W/"v1"'
    assert again.body == {"number": 1}
    assert again.fetched_at == clock.now and not again.stale
    assert rec.outcomes == ["ok", "cached", "not_modified"]


def test_rate_limit_exhausted_backs_off_until_reset_and_serves_cache() -> None:
    clock, rec = Clock(), Recorder()
    reset = int((clock.now + timedelta(minutes=10)).timestamp())
    with serve() as gh:
        gh.reply(
            PR,
            {"number": 1},
            X_RateLimit_Remaining="0",
            X_RateLimit_Limit="5000",
            X_RateLimit_Reset=str(reset),
        )
        c = client(gh.url, clock, rec, cache_ttl=timedelta(0))
        fetched = c.get(PR)
        clock.advance(minutes=5)
        stale = c.get(PR)  # backing off: nothing sent, the cached answer is served
        assert len(gh.requests) == 1
        clock.advance(minutes=6)
        gh.reply(PR, {"number": 1}, X_RateLimit_Remaining="4999")
        c.get(PR)
    assert rec.rate == (4999, -1)
    assert stale.stale and stale.fetched_at == fetched.fetched_at
    assert len(gh.requests) == 2
    assert "backoff" in rec.outcomes


def test_retry_after_is_respected_and_backoff_without_cache_raises() -> None:
    clock = Clock()
    with serve() as gh:
        gh.reply(PR, {"message": "secondary rate limit"}, status=403, Retry_After="120")
        c = client(gh.url, clock)
        with pytest.raises(GitHubError) as limited:
            c.get(PR)
        with pytest.raises(GitHubError) as waiting:
            c.get(PR)
        assert len(gh.requests) == 1
        clock.advance(seconds=121)
        gh.reply(PR, {"number": 1})
        assert c.get(PR).body == {"number": 1}
    assert limited.value.kind == "rate_limited"
    assert waiting.value.kind == "backoff"


def test_backoff_is_capped() -> None:
    clock = Clock()
    with serve() as gh:
        gh.reply(PR, status=429, Retry_After="999999")
        c = client(gh.url, clock)
        with pytest.raises(GitHubError):
            c.get(PR)
    assert c.backoff_until == clock.now + timedelta(hours=1)


def test_network_error_degrades_and_backs_off() -> None:
    clock, rec = Clock(), Recorder()
    with serve() as gh:
        url = gh.url
    c = client(url, clock, rec)  # the server is gone: connection refused
    with pytest.raises(GitHubError) as err:
        c.get(PR)
    assert err.value.kind == "network"
    with pytest.raises(GitHubError):
        c.get(PR)
    assert rec.outcomes == ["network", "backoff"]


def test_server_error_and_bad_json() -> None:
    clock = Clock()
    with serve() as gh:
        gh.reply(PR, {"m": 1}, status=502)
        c = client(gh.url, clock, error_backoff=timedelta(0))
        with pytest.raises(GitHubError) as server:
            c.get(PR)
        gh.routes.clear()
        gh.reply("/repos/example/repo/pulls/3", "not an object")
        assert c.get("/repos/example/repo/pulls/3").body == "not an object"
    assert server.value.kind == "server"


def test_cache_is_bounded() -> None:
    with serve() as gh:
        for n in range(3):
            gh.reply(f"/repos/example/repo/pulls/{n}", {"n": n})
        c = client(gh.url, Clock(), max_cache_entries=2)
        for n in range(3):
            c.get(f"/repos/example/repo/pulls/{n}")
        c.get("/repos/example/repo/pulls/0")  # evicted: asked again
    assert len(gh.requests) == 4


def test_cache_grows_for_the_tick() -> None:
    with serve() as gh:
        for n in range(3):
            gh.reply(f"/repos/example/repo/pulls/{n}", {"n": n})
        c = client(gh.url, Clock(), max_cache_entries=2)
        c.begin_tick(100, timedelta(seconds=10), min_cache_entries=3)
        for n in [0, 1, 2, 0]:
            c.get(f"/repos/example/repo/pulls/{n}")
    assert len(gh.requests) == 3


def test_deeply_nested_json_is_a_bad_response() -> None:
    with serve() as gh:
        gh.reply(PR, b"[" * 200_000 + b"]" * 200_000)
        with pytest.raises(GitHubError) as err:
            client(gh.url, Clock()).get(PR)
    assert err.value.kind == "bad_response"


def test_permission_403_is_cached_for_its_url_only() -> None:
    clock = Clock()
    other_repo = "/repos/example/other/pulls/1"
    with serve() as gh:
        gh.reply(
            PR, {"message": "Resource not accessible"}, status=403, X_RateLimit_Remaining="4000"
        )
        gh.reply(other_repo, {"number": 1})
        gh.reply("/repos/example/repo/pulls/2", {"message": "gone"}, status=410)
        c = client(gh.url, clock)
        with pytest.raises(GitHubError) as first:
            c.get(PR)
        assert c.backoff_until is None  # no global back-off
        assert c.get(other_repo).body == {"number": 1}  # another repo keeps working
        with pytest.raises(GitHubError) as cached:
            c.get(PR)  # within the TTL: the cached answer, no request
        with pytest.raises(GitHubError) as other:
            c.get("/repos/example/repo/pulls/2")
        with pytest.raises(GitHubError):
            c.get("/repos/example/repo/pulls/2")
    assert first.value.kind == cached.value.kind == "forbidden"
    assert other.value.kind == "http_error" and other.value.status == 410
    assert gh.paths() == [PR, other_repo, "/repos/example/repo/pulls/2"]


def test_secondary_rate_limit_403_backs_off_everything_for_a_minute() -> None:
    clock = Clock()
    body = {"message": "You have exceeded a secondary rate limit."}
    with serve() as gh:
        gh.reply(PR, body, status=403, X_RateLimit_Remaining="4000")
        gh.reply("/repos/example/other/pulls/1", {"number": 1})
        c = client(gh.url, clock, error_backoff=timedelta(0))
        with pytest.raises(GitHubError) as limited:
            c.get(PR)
        with pytest.raises(GitHubError) as waiting:
            c.get("/repos/example/other/pulls/1")
        assert len(gh.requests) == 1
    assert limited.value.kind == "rate_limited" and waiting.value.kind == "backoff"
    assert c.backoff_until == clock.now + timedelta(seconds=60)


def test_response_too_large() -> None:
    with serve() as gh:
        gh.reply(PR, b'"' + b"x" * (4 * 1024 * 1024 + 10) + b'"')
        with pytest.raises(GitHubError) as err:
            client(gh.url, Clock()).get(PR)
    assert err.value.kind == "bad_response" and "too large" in str(err.value)


def test_tick_budgets_serve_the_cache() -> None:
    clock, mono = Clock(), [0.0]
    with serve() as gh:
        gh.reply(PR, {"number": 1})
        c = GitHubClient(gh.url, TOKEN, clock=clock, monotonic=lambda: mono[0])
        c.begin_tick(1, timedelta(seconds=10))
        c.get(PR)
        clock.advance(seconds=61)
        stale = c.get(PR)  # request budget spent: the cached answer
        assert stale.stale and len(gh.requests) == 1
        c.begin_tick(5, timedelta(seconds=10))
        mono[0] = 11.0  # the tick's time is up
        with pytest.raises(GitHubError) as err:
            c.get("/repos/example/repo/pulls/2")
        assert len(gh.requests) == 1
    assert err.value.kind == "budget"


@contextmanager
def drip(head: bytes, body: bytes, drip_head: bool) -> Iterator[str]:
    """A loopback server that sends one byte every 50 ms, of the headers or of the body."""
    server = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def run() -> None:
        conn, _ = server.accept()
        with conn:
            conn.recv(65536)
            if not drip_head:
                conn.sendall(head)
            for byte in head + body if drip_head else body:
                if stop.is_set():
                    return
                try:
                    conn.sendall(bytes([byte]))
                except OSError:
                    return
                time.sleep(0.05)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.getsockname()[1]}"
    finally:
        stop.set()
        server.close()
        thread.join(2)


@pytest.mark.parametrize("drip_head", [True, False])
def test_whole_request_deadline_on_a_dripping_server(drip_head: bool) -> None:
    body = b'{"number": 1, "pad": "' + b"x" * 200 + b'"}'
    head = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nX-Pad: "
        + b"y" * 100
        + b"\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
    )
    with drip(head, body, drip_head) as url:
        c = GitHubClient(url, TOKEN, clock=Clock(), timeout=timedelta(seconds=1))
        started = time.monotonic()
        with pytest.raises(GitHubError) as err:
            c.get(PR)
        elapsed = time.monotonic() - started
    assert err.value.kind == "network" and "timed out" in str(err.value)
    assert elapsed < 1.5


def test_https_goes_through_an_environment_proxy_as_a_tunnel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("https_proxy", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    c = GitHubClient("https://api.example.test", TOKEN)
    direct = c._connection(1.0)
    assert direct.host == "api.example.test" and direct._tunnel_host is None  # type: ignore[attr-defined]
    monkeypatch.setenv("https_proxy", "http://user:pw@proxy.example.test:3128")
    tunnel = c._connection(1.0)
    assert (tunnel.host, tunnel.port) == ("proxy.example.test", 3128)
    assert tunnel._tunnel_host == "api.example.test"  # type: ignore[attr-defined]
    assert tunnel._tunnel_headers["Proxy-Authorization"].startswith("Basic ")  # type: ignore[attr-defined]
    monkeypatch.setenv("no_proxy", "api.example.test")
    assert c._connection(1.0).host == "api.example.test"
    loopback = GitHubClient("http://127.0.0.1:1", TOKEN)._connection(1.0)
    assert loopback.host == "127.0.0.1" and loopback._tunnel_host is None  # type: ignore[attr-defined]
