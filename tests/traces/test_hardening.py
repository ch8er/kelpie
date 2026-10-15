"""Review fixes: exposure, auth before body, concurrency, timeouts, implausible times,
gzip members, incremental snapshot window, retention stop."""

from __future__ import annotations

import gzip
import io
import json
import socket
import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import ConfigError, Settings, build_app, check_exposure
from kelpie.cli import main
from kelpie.core.model import Snapshot, Span, SpanKind
from kelpie.core.spans import MemorySpanStore, SpanStore
from kelpie.core.storage import SqliteSpanStore
from kelpie.metrics import Metrics
from kelpie.server import make_server
from kelpie.traces import otlp, semconv
from kelpie.traces.config import TraceSettings, is_loopback
from kelpie.traces.ingest import TraceIngest, _gunzip, _TooLarge
from kelpie.traces.otlp import JSON, PROTOBUF
from kelpie.traces.store import SpanWindow
from tests.traces.otlp_builders import NOW, S, request, sid
from tests.traces.test_ingest import Headers, Observer

CHAT = {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 10}


# --- exposure ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.3.2.1", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("10.0.0.5", False),
        ("kelpie.internal", False),
    ],
)
def test_is_loopback(host: str, loopback: bool) -> None:
    assert is_loopback(host) is loopback


@pytest.mark.parametrize(
    ("traces", "env", "host", "ok"),
    [
        (TraceSettings(), {}, "127.0.0.1", True),
        (TraceSettings(), {}, "0.0.0.0", False),
        (TraceSettings(), {"KELPIE_TRACES_TOKEN": "t"}, "0.0.0.0", True),
        (TraceSettings(allow_unauthenticated=True), {}, "0.0.0.0", True),
        (TraceSettings(), {"KELPIE_TRACES_ALLOW_UNAUTHENTICATED": "1"}, "0.0.0.0", True),
        (TraceSettings(), {"KELPIE_TRACES_ALLOW_UNAUTHENTICATED": "no"}, "0.0.0.0", False),
        (TraceSettings(enabled=False), {}, "0.0.0.0", True),
    ],
)
def test_check_exposure(traces: TraceSettings, env: dict[str, str], host: str, ok: bool) -> None:
    if ok:
        traces.check_exposure(host, env)
    else:
        with pytest.raises(ValueError, match="accept spans from anyone"):
            traces.check_exposure(host, env)


def test_serve_refuses_an_open_endpoint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KELPIE_TRACES_TOKEN", raising=False)
    monkeypatch.delenv("KELPIE_TRACES_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    monkeypatch.chdir(Path(__file__).parent)  # no kelpie.toml here
    assert main(["serve", "--host", "0.0.0.0"]) == 2
    assert "KELPIE_TRACES_TOKEN" in capsys.readouterr().err
    with pytest.raises(ConfigError):
        check_exposure(Settings(), "0.0.0.0")


def test_allow_unauthenticated_config(tmp_path: Path) -> None:
    from kelpie.app import load_settings

    path = tmp_path / "kelpie.toml"
    path.write_text("[traces]\nallow_unauthenticated = true\n")
    assert load_settings(path).traces.allow_unauthenticated
    path.write_text('[traces]\nallow_unauthenticated = "yes"\n')
    with pytest.raises(ConfigError):
        load_settings(path)


# --- ingest: order of checks, concurrency ----------------------------------------------


def ingest(**kw: Any) -> tuple[TraceIngest, MemorySpanStore, Observer]:
    store, observer = MemorySpanStore(), Observer()
    return TraceIngest(store=store, clock=lambda: NOW, observer=observer, **kw), store, observer


class Reader:
    def __init__(self, body: bytes) -> None:
        self.body = io.BytesIO(body)
        self.calls = 0

    def read(self, n: int) -> bytes:
        self.calls += 1
        return self.body.read(n)


def headers(body: bytes, **extra: str) -> Headers:
    h = Headers({"Content-Type": PROTOBUF, "Content-Length": str(len(body))})
    h.update({k.replace("_", "-"): v for k, v in extra.items()})
    return h


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ({}, 401),
        ({"Content_Type": "text/plain", "Authorization": "Bearer tok"}, 415),
        ({"Content_Encoding": "br", "Authorization": "Bearer tok"}, 415),
    ],
)
def test_rejected_before_the_body_is_read(extra: dict[str, str], status: int) -> None:
    target = ingest(token="tok")[0]
    body = request([S(sid(1), attrs=CHAT)])
    reader = Reader(body)
    reply = target.handle(headers(body, **extra), reader.read)
    assert reply.status == status and reply.close and reader.calls == 0


@pytest.mark.parametrize("auth", ["bearer tok", "BEARER tok", "Bearer   tok "])
def test_bearer_scheme_is_case_insensitive(auth: str) -> None:
    target = ingest(token="tok")[0]
    body = request([S(sid(1), attrs=CHAT)])
    assert target.handle(headers(body, Authorization=auth), io.BytesIO(body).read).status == 200


def test_busy_ingest_answers_503_without_reading() -> None:
    target, _, observer = ingest(max_concurrent=1)
    body = request([S(sid(1), attrs=CHAT)])
    entered, release = threading.Event(), threading.Event()

    def slow_read(n: int) -> bytes:
        entered.set()
        release.wait(5)
        return body

    first: list[int] = []
    t = threading.Thread(
        target=lambda: first.append(target.handle(headers(body), slow_read).status)
    )
    t.start()
    assert entered.wait(5)
    reader = Reader(body)
    busy = target.handle(headers(body), reader.read)
    release.set()
    t.join(5)
    assert busy.status == 503 and busy.close and busy.headers == {"Retry-After": "1"}
    assert reader.calls == 0 and first == [200]
    assert target.handle(headers(body), io.BytesIO(body).read).status == 200  # slot freed
    assert observer.codes == [503, 200, 200]


# --- implausible times ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "duration"),
    [
        (NOW + timedelta(hours=2), timedelta(seconds=1)),  # future start
        (NOW, timedelta(hours=3)),  # ends in the future
        (NOW - timedelta(days=8), timedelta(seconds=1)),  # older than retention
    ],
)
def test_implausible_times_are_rejected(start: datetime, duration: timedelta) -> None:
    target, store, observer = ingest()
    good = S(sid(1), attrs=CHAT)
    bad = S(sid(2), attrs=CHAT, start=start, duration=duration)
    reply = target.handle(headers(b := request([good, bad])), io.BytesIO(b).read)
    assert reply.status == 200 and store.count() == 1
    assert observer.rejected == Counter({"invalid_time": 1})


def test_huge_timestamps_never_reach_datetime() -> None:
    target, store, observer = ingest()
    span = {
        "traceId": "ab" * 16,
        "spanId": sid(3),
        "name": "chat",
        "startTimeUnixNano": str(10**27),
        "endTimeUnixNano": str(10**27),
        "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}],
    }
    body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [span]}]}]}).encode()
    reply = target.handle(headers(body, Content_Type=JSON), io.BytesIO(body).read)
    assert reply.status == 200 and store.count() == 0
    assert observer.rejected == Counter({"invalid_time": 1})


def test_map_spans_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = otlp.decode(request([S(sid(1), attrs=CHAT), S(sid(2), attrs=CHAT)]), PROTOBUF).spans
    real = semconv._span

    def flaky(r: otlp.RawSpan, task: str | None, session: str | None) -> Span | None:
        if r.span_id == sid(1):
            raise RuntimeError("unforeseen")
        if r.span_id == sid(2):
            raise OverflowError("date value out of range")
        return real(r, task, session)

    monkeypatch.setattr(semconv, "_span", flaky)
    mapped = semconv.map_spans(raw)
    assert mapped.spans == [] and mapped.rejected == Counter(malformed=1, invalid_time=1)


def test_deeply_nested_json_is_400() -> None:
    body = b'{"resourceSpans": ' + b"[" * 100_000 + b"]" * 100_000 + b"}"
    target = ingest(max_body_bytes=1_000_000)[0]
    reply = target.handle(headers(body, Content_Type=JSON), io.BytesIO(body).read)
    assert reply.status == 400


@pytest.mark.parametrize("bad", ["-" + "1" * 15, "0x" + "1" * 14, "a_" + "1" * 14, " " + "1" * 15])
def test_hex_ids_are_strict(bad: str) -> None:
    assert not otlp._is_hex(bad, 16)


# --- gzip -------------------------------------------------------------------------------


def test_gzip_members_and_trailing_junk() -> None:
    assert _gunzip(gzip.compress(b"ab") + gzip.compress(b"cd"), 10) == b"abcd"
    with pytest.raises(_TooLarge):
        _gunzip(gzip.compress(b"abcdef") + gzip.compress(b"ghijkl"), 10)
    with pytest.raises(Exception):  # noqa: B017 - zlib.error or ValueError
        _gunzip(gzip.compress(b"ab") + b"junk", 10)


# --- server -----------------------------------------------------------------------------


def test_stalled_client_is_dropped_and_counted(tmp_path: Path) -> None:
    app = build_app(Settings(db_path=tmp_path / "k.db"), clock=lambda: NOW)
    server = make_server("127.0.0.1", 0, app.metrics, app.ingest)
    server.RequestHandlerClass.timeout = 0.2  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address) as sock:
            sock.sendall(
                b"POST /v1/traces HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n{"
            )
            sock.settimeout(5)
            assert sock.recv(100) == b""  # closed without a reply
        deadline = time.monotonic() + 5
        while 'kelpie_traces_requests_total{code="408"} 1' not in app.metrics.render():
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        server.RequestHandlerClass.timeout = 30  # type: ignore[attr-defined]
        server.shutdown()
        server.server_close()
        app.close()


def test_dripping_client_hits_the_body_deadline(tmp_path: Path) -> None:
    """A byte just under the socket timeout each time still ends at the body deadline."""
    app = build_app(Settings(db_path=tmp_path / "k.db"), clock=lambda: NOW)
    server = make_server("127.0.0.1", 0, app.metrics, app.ingest)
    handler = server.RequestHandlerClass
    handler.timeout, handler.body_deadline = 0.5, 0.6  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address) as sock:
            sock.sendall(
                b"POST /v1/traces HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                b"Content-Length: 100\r\n\r\n"
            )
            closed = False
            for _ in range(20):
                time.sleep(0.2)
                try:
                    sock.sendall(b" ")
                except OSError:
                    closed = True
                    break
            sock.settimeout(5)
            assert closed or sock.recv(100) == b""
        deadline = time.monotonic() + 5
        while 'kelpie_traces_requests_total{code="408"} 1' not in app.metrics.render():
            assert time.monotonic() < deadline
            time.sleep(0.02)
    finally:
        handler.timeout, handler.body_deadline = 30, 60.0  # type: ignore[attr-defined]
        server.shutdown()
        server.server_close()
        app.close()


def test_body_in_several_chunks_is_read_whole(tmp_path: Path) -> None:
    import urllib.request

    app = build_app(Settings(db_path=tmp_path / "k.db"), clock=lambda: NOW)
    server = make_server("127.0.0.1", 0, app.metrics, app.ingest)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = b"{" + b" " * 200_000 + b"}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/traces",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        app.close()


def test_serve_defaults_to_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    import kelpie.cli as cli

    seen: list[str] = []
    monkeypatch.delenv("KELPIE_HOST", raising=False)
    monkeypatch.delenv("KELPIE_TRACES_TOKEN", raising=False)
    monkeypatch.delenv("KELPIE_TRACES_ALLOW_UNAUTHENTICATED", raising=False)
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    monkeypatch.chdir(Path(__file__).parent)
    monkeypatch.setattr(cli, "check_exposure", lambda settings, host: seen.append(host))
    monkeypatch.setattr(cli, "build_app", lambda settings: (_ for _ in ()).throw(ConfigError("x")))
    assert cli.main(["serve"]) == 2
    assert seen == ["127.0.0.1"]


def test_ingest_bug_is_500_and_counted(tmp_path: Path) -> None:
    import urllib.error
    import urllib.request

    class Boom:
        def handle(self, headers: object, read: object) -> object:
            raise RuntimeError("bug")

    metrics = Metrics()
    metrics.set_traces(True)
    server = make_server("127.0.0.1", 0, metrics, Boom())  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/traces", data=b"{}", method="POST"
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 500
        assert 'kelpie_traces_requests_total{code="500"} 1' in metrics.render()
    finally:
        server.shutdown()
        server.server_close()


# --- incremental snapshot window ---------------------------------------------------------


def span(n: int, start: datetime, **kw: Any) -> Span:
    base = Span(
        trace_id="t" * 32,
        span_id=f"{n:016x}",
        name="chat",
        kind=SpanKind.LLM,
        start=start,
        end=start,
        session_id="s-1",
    )
    return replace(base, **kw)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "memory":
        yield MemorySpanStore()
        return
    s = SqliteSpanStore(tmp_path / "k.db", batch=3)
    yield s
    s.close()


class WindowMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def snapshot_spans(self, count: int, dropped: int) -> None:
        self.calls.append((count, dropped))


def ids(spans: tuple[Span, ...]) -> list[int]:
    return [int(s.span_id, 16) for s in spans]


def test_changes_are_incremental(store: SpanStore) -> None:
    store.append_spans([span(1, NOW), span(2, NOW)], NOW)
    first = store.changes(NOW - timedelta(hours=1), 0)
    assert ids(first.spans) == [1, 2] and first.version > 0
    assert store.changes(NOW - timedelta(hours=1), first.version).spans == ()
    store.append_spans([span(3, NOW, parent_id=f"{1:016x}", session_id=None)], NOW)
    store.append_spans([span(1, NOW)], NOW)  # a retry changes nothing
    later = store.changes(NOW - timedelta(hours=1), first.version)
    assert ids(later.spans) == [3]
    store.inherit_links({"t" * 32})  # the link update is a change too
    linked = store.changes(NOW - timedelta(hours=1), later.version)
    assert ids(linked.spans) == [3] and linked.spans[0].session_id == "s-1"


def test_window_cache_follows_inserts_links_eviction_and_deletes(store: SpanStore) -> None:
    observer = WindowMetrics()
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24), 3, observer)
    store.append_spans([span(1, NOW - timedelta(minutes=20)), span(2, NOW)], NOW)
    assert ids(window.spans(NOW)) == [1, 2]
    store.append_spans([span(3, NOW, parent_id=f"{2:016x}", session_id=None)], NOW)
    store.inherit_links({"t" * 32})
    got = window.spans(NOW)
    assert ids(got) == [1, 2, 3] and got[2].session_id == "s-1"
    assert ids(window.spans(NOW + timedelta(minutes=15))) == [2, 3]  # 1 left the window
    store.append_spans([span(n, NOW + timedelta(seconds=n)) for n in (4, 5)], NOW)
    assert ids(window.spans(NOW + timedelta(minutes=15))) == [3, 4, 5]  # capped at 3
    assert observer.calls[-1] == (3, 1)
    store.prune(NOW - timedelta(days=1), max_rows=2)  # deletes: the cache starts over
    assert ids(window.spans(NOW + timedelta(minutes=15))) == [4, 5]


def test_totals_are_cached_for_a_minute(store: SpanStore) -> None:
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24))
    assert window.totals(NOW) == ()
    store.append_spans([span(1, NOW)], NOW)
    assert window.totals(NOW + timedelta(seconds=30)) == ()
    assert [t.spans for t in window.totals(NOW + timedelta(minutes=1))] == [1]
    assert [t.spans for t in window.totals(NOW - timedelta(minutes=5))] == [1]  # clock back


def test_spans_for_session_index() -> None:
    a, b, c = span(1, NOW), span(2, NOW, session_id="s-2"), span(3, NOW, session_id=None)
    snap = Snapshot(now=NOW, spans=(a, b, c))
    assert snap.spans_for_session("s-1") == (a,)
    assert snap.spans_for_session("s-2") == (b,)
    assert snap.spans_for_session("none") == ()
    assert snap == Snapshot(now=NOW, spans=(a, b, c))  # the cache is not compared
    assert replace(snap, spans=(b,)).spans_for_session("s-1") == ()


def test_close_stops_a_long_prune(tmp_path: Path) -> None:
    store = SqliteSpanStore(tmp_path / "k.db", batch=1)
    store.append_spans([span(n, NOW - timedelta(days=30)) for n in range(1, 50)], NOW)
    calls = 0
    real = store._delete

    def delete(sql: str, args: tuple[object, ...]) -> int:
        nonlocal calls
        calls += 1
        if calls == 3:
            store._closing.set()
        return real(sql, args)

    store._delete = delete  # type: ignore[method-assign]
    assert store.prune(NOW, max_rows=1000) == 2
    store.close()


def test_count_does_not_wait_for_writes(tmp_path: Path) -> None:
    store = SqliteSpanStore(tmp_path / "k.db")
    with store._lock:  # a write batch in progress
        assert store.count() == 0
    store.close()


def test_snapshot_metrics_render() -> None:
    metrics = Metrics()
    metrics.set_traces(True)
    metrics.snapshot_spans(10, 4)
    metrics.snapshot_spans(12, 1)
    text = metrics.render()
    assert "kelpie_snapshot_spans 12" in text
    assert "kelpie_snapshot_spans_dropped_total 5" in text
    assert "kelpie_span_store_rows 0" in text


def test_span_rows_source_failure_reads_zero() -> None:
    metrics = Metrics()
    metrics.set_traces(True)

    def broken() -> int:
        raise OSError("closed")

    metrics.set_span_rows_source(broken)
    assert "kelpie_span_store_rows 0" in metrics.render()
