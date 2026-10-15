"""Forwarding accepted OTLP requests to other backends, against a local fake receiver."""

from __future__ import annotations

import gzip
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from kelpie.app import ConfigError, Settings, build_app, load_rules, load_settings, render_status
from kelpie.metrics import Metrics
from kelpie.server import make_server
from kelpie.traces import forward
from kelpie.traces.config import ForwardSettings, TraceSettings
from kelpie.traces.forward import Destination, Export, Forwarder, SendError, send_http
from kelpie.traces.otlp import PROTOBUF
from tests.traces.otlp_builders import NOW, S, request, sid

CHAT = {"gen_ai.operation.name": "chat"}


@dataclass
class Received:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Receiver:
    """A fake OTLP backend: records requests and answers with scripted status codes."""

    codes: list[int] = field(default_factory=list)  # consumed in order, then 200
    headers: dict[str, str] = field(default_factory=dict)  # sent with every reply
    received: list[Received] = field(default_factory=list)
    event: threading.Event = field(default_factory=threading.Event)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                receiver.received.append(Received(self.path, dict(self.headers.items()), body))
                code = receiver.codes.pop(0) if receiver.codes else 200
                self.send_response(code)
                for name, value in receiver.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.end_headers()
                receiver.event.set()

            def log_message(self, format: str, *args: object) -> None:
                pass

        return Handler


@pytest.fixture
def receiver() -> Iterator[tuple[Receiver, str]]:
    r = Receiver()
    server = ThreadingHTTPServer(("127.0.0.1", 0), r.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield r, f"http://127.0.0.1:{server.server_address[1]}/api/public/otel/v1/traces"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(forward, "BACKOFF_BASE", 0.01)


def wait_for(cond: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(cond) and cond():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def counts(metrics: Metrics, name: str) -> tuple[int, int, int]:
    c = metrics.forward.get(name, Counter())
    return c["forwarded"], c["failed"], c["dropped"]


# --- forwarder --------------------------------------------------------------------------


def test_forwards_the_original_bytes_and_headers(receiver: tuple[Receiver, str]) -> None:
    r, url = receiver
    metrics = Metrics()
    dest = Destination("langfuse", url, {"Authorization": "Basic cGs6c2s="}, timeout=5)
    fwd = Forwarder([dest], metrics)
    metrics.set_forward_destinations(["langfuse"])
    fwd.start()
    body = gzip.compress(request([S(sid(1), attrs=CHAT)]))
    fwd.submit(body, PROTOBUF, "gzip")
    wait_for(lambda: counts(metrics, "langfuse")[0] == 1)
    fwd.close()
    (got,) = r.received
    assert got.path == "/api/public/otel/v1/traces" and got.body == body
    assert got.headers["Content-Type"] == PROTOBUF
    assert got.headers["Content-Encoding"] == "gzip"
    assert got.headers["Authorization"] == "Basic cGs6c2s="
    assert got.headers["User-Agent"].startswith("kelpie/")
    assert "cGs6c2s=" not in repr(dest)  # secrets stay out of logs


def test_retries_5xx_and_429_then_succeeds(receiver: tuple[Receiver, str]) -> None:
    r, url = receiver
    r.codes = [503, 429]
    metrics = Metrics()
    fwd = Forwarder([Destination("d", url, retries=3)], metrics)
    fwd.start()
    fwd.submit(b"{}", "application/json", None)
    wait_for(lambda: metrics.forward.get("d", {}).get("forwarded") == 1)
    fwd.close()
    assert len(r.received) == 3


def test_client_errors_are_not_retried(receiver: tuple[Receiver, str]) -> None:
    r, url = receiver
    r.codes = [401]
    metrics = Metrics()
    fwd = Forwarder([Destination("d", url, retries=3)], metrics)
    fwd.start()
    fwd.submit(b"{}", "application/json", None)
    wait_for(lambda: metrics.forward.get("d", {}).get("failed") == 1)
    fwd.close()
    assert len(r.received) == 1


def test_gives_up_after_retries() -> None:
    metrics = Metrics()
    # Nothing listens on port 9 of localhost (discard); connection refused is retryable.
    fwd = Forwarder([Destination("down", "http://127.0.0.1:9/v1/traces", retries=2)], metrics)
    fwd.start()
    fwd.submit(b"{}", "application/json", None)
    wait_for(lambda: metrics.forward.get("down", {}).get("failed") == 1)
    fwd.close()


def test_send_http_classifies_errors(receiver: tuple[Receiver, str]) -> None:
    r, url = receiver
    r.codes = [500, 404]
    export = Export(b"{}", "application/json")
    with pytest.raises(SendError) as exc:
        send_http(Destination("d", url), export)
    assert exc.value.retryable
    with pytest.raises(SendError) as exc:
        send_http(Destination("d", url), export)
    assert not exc.value.retryable
    with pytest.raises(SendError) as exc:
        send_http(Destination("d", "nota://url"), export)
    assert exc.value.retryable


def test_full_queue_drops_without_blocking() -> None:
    release = threading.Event()
    sent: list[Export] = []

    def slow(destination: Destination, export: Export) -> None:
        release.wait(5)
        sent.append(export)

    metrics = Metrics()
    fwd = Forwarder([Destination("slow", "http://unused", queue_size=1)], metrics, sender=slow)
    fwd.start()
    started = time.monotonic()
    fwd.submit(b"1", "application/json", None)
    wait_for(lambda: fwd._workers[0].queue.empty())  # the thread took the first one
    for n in range(2, 6):
        fwd.submit(str(n).encode(), "application/json", None)
    assert time.monotonic() - started < 1.0
    assert counts(metrics, "slow") == (0, 0, 3)
    release.set()
    wait_for(lambda: counts(metrics, "slow")[0] == 2)
    fwd.close()
    assert [e.body for e in sent] == [b"1", b"2"]


def test_close_interrupts_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(destination: Destination, export: Export) -> None:
        raise SendError("down", retryable=True)

    monkeypatch.setattr(forward, "BACKOFF_BASE", 30.0)
    fwd = Forwarder([Destination("d", "http://unused", retries=5)], sender=failing)
    fwd.start()
    fwd.submit(b"{}", "application/json", None)
    time.sleep(0.05)
    started = time.monotonic()
    fwd.close(timeout=5)
    assert time.monotonic() - started < 2
    assert not fwd._workers[0].thread.is_alive()


def test_a_crashing_sender_is_counted_and_the_thread_survives() -> None:
    calls: list[bytes] = []

    def buggy(destination: Destination, export: Export) -> None:
        calls.append(export.body)
        if export.body == b"bad":
            raise RuntimeError("bug")

    metrics = Metrics()
    fwd = Forwarder([Destination("d", "http://unused")], metrics, sender=buggy)
    fwd.start()
    fwd.submit(b"bad", "application/json", None)
    fwd.submit(b"good", "application/json", None)
    wait_for(lambda: counts(metrics, "d")[:2] == (1, 1))
    fwd.close()


# --- config -----------------------------------------------------------------------------


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


LANGFUSE = """
[[traces.forward]]
name = "langfuse"
endpoint = "https://cloud.langfuse.com/api/public/otel/v1/traces"
headers_env = { Authorization = "LANGFUSE_AUTH" }
headers = { "x-langfuse-ingestion-version" = "4" }
timeout = "5s"

[[traces.forward]]
endpoint = "http://phoenix.internal:6006/v1/traces"
queue_size = 10
retries = 0
"""


def test_forward_config(tmp_path: Path) -> None:
    langfuse, phoenix = load_settings(write(tmp_path, LANGFUSE)).traces.forward
    assert langfuse == ForwardSettings(
        name="langfuse",
        endpoint="https://cloud.langfuse.com/api/public/otel/v1/traces",
        headers={"x-langfuse-ingestion-version": "4"},
        headers_env={"Authorization": "LANGFUSE_AUTH"},
        timeout=timedelta(seconds=5),
    )
    assert phoenix.name == "phoenix.internal" and phoenix.queue_size == 10 and phoenix.retries == 0
    dest = langfuse.destination({"LANGFUSE_AUTH": "Basic abc"})
    assert dest.headers == {"x-langfuse-ingestion-version": "4", "Authorization": "Basic abc"}
    assert dest.timeout == 5.0
    with pytest.raises(ValueError, match="LANGFUSE_AUTH"):
        langfuse.destination({})


def test_no_forward_by_default() -> None:
    assert load_settings(None).traces.forward == ()


@pytest.mark.parametrize(
    "text",
    [
        "[traces]\nforward = 5\n",
        "[[traces.forward]]\nname = 'x'\n",
        "[[traces.forward]]\nendpoint = 'ftp://host/v1/traces'\n",
        "[[traces.forward]]\nendpoint = 'not a url'\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nport = 1\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nname = ''\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nheaders = { A = 1 }\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nheaders_env = 'X'\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\ntimeout = 'soon'\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\ntimeout = 0\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nqueue_size = 0\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nretries = -1\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nretries = true\n",
        "[[traces.forward]]\nendpoint = 'http://h/a'\n[[traces.forward]]\nendpoint = 'http://h/b'\n",
    ],
)
def test_bad_forward_config(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


def test_missing_header_env_stops_kelpie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGFUSE_AUTH", raising=False)
    with pytest.raises(ConfigError, match="LANGFUSE_AUTH"):
        build_app(load_settings(write(tmp_path, LANGFUSE)))


def test_status_lists_destinations(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, LANGFUSE))
    text = render_status(settings, load_rules(settings.engine))
    assert "forward to langfuse (https://cloud.langfuse.com/api/public/otel/v1/traces)" in text


# --- end to end -------------------------------------------------------------------------


def test_kelpie_forwards_what_it_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receiver: tuple[Receiver, str]
) -> None:
    r, url = receiver
    monkeypatch.setenv("FAKE_AUTH", "Basic c3ludGhldGlj")
    traces = TraceSettings(
        forward=(ForwardSettings("fake", url, headers_env={"Authorization": "FAKE_AUTH"}),)
    )
    app = build_app(Settings(db_path=tmp_path / "kelpie.db", traces=traces), clock=lambda: NOW)
    assert app.forwarder is not None
    server = make_server("127.0.0.1", 0, app.metrics, app.ingest)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}/v1/traces"
    body = request([S(sid(1), attrs=CHAT)])
    try:
        req = urllib.request.Request(base, data=body, method="POST")
        req.add_header("Content-Type", PROTOBUF)
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.status == 200
        bad = urllib.request.Request(base, data=b"{oops", method="POST")
        bad.add_header("Content-Type", "application/json")
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad, timeout=5)
        wait_for(lambda: counts(app.metrics, "fake")[0] == 1)
    finally:
        server.shutdown()
        server.server_close()
        app.close()
    (got,) = r.received  # the rejected request was not forwarded
    assert got.body == body and got.headers["Authorization"] == "Basic c3ludGhldGlj"
    text = app.metrics.render()
    assert 'kelpie_traces_forwarded_total{destination="fake"} 1' in text
    assert 'kelpie_traces_forward_dropped_total{destination="fake"} 0' in text
    assert 'kelpie_traces_forward_failed_total{destination="fake"} 0' in text


# --- review fixes -----------------------------------------------------------------------


@pytest.fixture
def second_receiver() -> Iterator[tuple[Receiver, str]]:
    r = Receiver()
    server = ThreadingHTTPServer(("127.0.0.1", 0), r.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield r, f"http://127.0.0.1:{server.server_address[1]}/elsewhere"
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("code", [301, 302, 307, 308])
def test_redirects_are_not_followed(
    receiver: tuple[Receiver, str], second_receiver: tuple[Receiver, str], code: int
) -> None:
    r, url = receiver
    other, elsewhere = second_receiver
    r.codes = [code]
    r.headers = {"Location": elsewhere}
    metrics = Metrics()
    fwd = Forwarder([Destination("d", url, {"Authorization": "Basic c2VjcmV0"})], metrics)
    fwd.start()
    fwd.submit(b"{}", "application/json", None)
    wait_for(lambda: counts(metrics, "d")[1] == 1)
    fwd.close()
    assert len(r.received) == 1 and other.received == []  # no retry, no follow
    assert counts(metrics, "d") == (0, 1, 0)


def test_bad_header_values_are_refused_at_startup_without_the_value() -> None:
    settings = ForwardSettings("d", "https://h/v1/traces", headers_env={"Authorization": "X_AUTH"})
    with pytest.raises(ValueError) as exc:
        settings.destination({"X_AUTH": "Basic top\nsecret-value"})
    assert "X_AUTH" in str(exc.value) and "secret-value" not in str(exc.value)


def test_send_errors_never_carry_header_values(
    receiver: tuple[Receiver, str], caplog: pytest.LogCaptureFixture
) -> None:
    _, url = receiver
    dest = Destination("d", url, {"X-Key": "top\nsecret-value"})  # http.client: ValueError
    with pytest.raises(SendError) as exc:
        send_http(dest, Export(b"{}", "application/json"))
    assert not exc.value.retryable and "secret-value" not in str(exc.value)
    metrics = Metrics()
    fwd = Forwarder([dest], metrics)
    fwd.start()
    fwd.submit(b"{}", "application/json", None)
    wait_for(lambda: counts(metrics, "d")[1] == 1)
    fwd.close()
    assert "secret-value" not in caplog.text


def test_queue_is_bounded_by_bytes() -> None:
    release = threading.Event()

    def slow(destination: Destination, export: Export) -> None:
        release.wait(5)

    metrics = Metrics()
    dest = Destination("d", "http://unused", queue_size=100, queue_bytes=10)
    fwd = Forwarder([dest], metrics, sender=slow)
    fwd.start()
    fwd.submit(b"x" * 8, "application/json", None)  # taken by the thread
    wait_for(lambda: fwd._workers[0].queue.empty())
    fwd.submit(b"y" * 8, "application/json", None)  # queued: 8 of 10 bytes
    fwd.submit(b"z" * 8, "application/json", None)  # would exceed: dropped
    assert counts(metrics, "d") == (0, 0, 1) and fwd._workers[0].queued_bytes == 8
    release.set()
    wait_for(lambda: counts(metrics, "d")[0] == 2)
    assert fwd._workers[0].queued_bytes == 0
    fwd.close()


def test_close_counts_what_was_still_queued() -> None:
    release = threading.Event()

    def slow(destination: Destination, export: Export) -> None:
        release.wait(5)

    metrics = Metrics()
    fwd = Forwarder([Destination("d", "http://unused")], metrics, sender=slow)
    fwd.start()
    for n in range(4):
        fwd.submit(str(n).encode(), "application/json", None)
    wait_for(lambda: fwd._workers[0].queue.qsize() == 3)
    release.set()
    fwd.close()
    assert counts(metrics, "d")[2] == 3


def test_retry_after_is_honoured() -> None:
    calls: list[float] = []

    def limited(destination: Destination, export: Export) -> None:
        calls.append(time.monotonic())
        if len(calls) == 1:
            raise SendError("HTTP 429", retryable=True, retry_after=0.3)

    metrics = Metrics()
    fwd = Forwarder([Destination("d", "http://unused")], metrics, sender=limited)
    fwd.start()
    fwd.submit(b"{}", "application/json", None)
    wait_for(lambda: counts(metrics, "d")[0] == 1)
    fwd.close()
    assert calls[1] - calls[0] >= 0.3


def test_retry_after_header_is_parsed(receiver: tuple[Receiver, str]) -> None:
    r, url = receiver
    r.codes = [429]
    r.headers = {"Retry-After": "0"}
    with pytest.raises(SendError) as exc:
        send_http(Destination("d", url), Export(b"{}", "application/json"))
    assert exc.value.retryable and exc.value.retry_after == 0.0
    r.codes = [503]
    r.headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    with pytest.raises(SendError) as exc:
        send_http(Destination("d", url), Export(b"{}", "application/json"))
    assert exc.value.retry_after is None


@pytest.mark.parametrize(
    "text",
    [
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nretries = 11\n",
        "[[traces.forward]]\nendpoint = 'https://user:pw@h/v1/traces'\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nheaders = { 'Bad Name' = 'x' }\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nheaders = { A = \"a\\nb\" }\n",
        "[[traces.forward]]\nendpoint = 'http://h/v1/traces'\nqueue_bytes = 0\n",
        "[traces]\nenabled = false\n[[traces.forward]]\nendpoint = 'http://h/v1/traces'\n",
    ],
)
def test_more_bad_forward_config(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


def test_endpoint_query_is_not_shown_and_clear_text_secrets_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KEY", "Bearer k")
    text = (
        "[[traces.forward]]\nname = 'plain'\nendpoint = 'http://collector.internal:4318/v1/traces?key=abc'\n"
        "headers_env = { Authorization = 'KEY' }\n"
    )
    settings = load_settings(write(tmp_path, text))
    status = render_status(settings, load_rules(settings.engine))
    assert "http://collector.internal:4318/v1/traces)" in status and "abc" not in status
    assert "accepted traces are still forwarded" in status
    app = build_app(replace_db(settings, tmp_path))
    app.close()
    assert "plain sends credentials over plain http" in caplog.text
    local = ForwardSettings("l", "http://127.0.0.1:4318/v1/traces", headers_env={"A": "KEY"})
    assert not local.sends_secrets_in_clear


def replace_db(settings: Settings, tmp_path: Path) -> Settings:
    from dataclasses import replace

    return replace(settings, db_path=tmp_path / "k.db")


def test_garbled_response_is_a_retryable_failure() -> None:
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def answer() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.recv(65536)
            conn.sendall(b"NOT-HTTP garbage\r\n\r\n")

    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{listener.getsockname()[1]}/v1/traces"
        with pytest.raises(SendError) as exc:
            send_http(Destination("d", url, {}), Export(b"{}", "application/json"))
        assert exc.value.retryable
    finally:
        thread.join(5)
        listener.close()
