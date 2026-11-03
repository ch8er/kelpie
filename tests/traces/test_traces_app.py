"""Traces in the app: [traces] config, POST /v1/traces end to end, snapshot merge, and
ingest that does not stall the control loop."""

from __future__ import annotations

import gzip
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import (
    ConfigError,
    ControlLoop,
    Settings,
    build_app,
    load_rules,
    load_settings,
    merged_snapshot,
    render_status,
)
from kelpie.core.config import EngineConfig
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot, Span, SpanKind
from kelpie.core.spans import MemorySpanStore
from kelpie.metrics import Metrics
from kelpie.server import KelpieHTTPServer, make_server, run
from kelpie.traces.config import TraceSettings
from kelpie.traces.otlp import JSON, PROTOBUF
from kelpie.traces.store import SpanWindow
from tests.fakes import FakeAdapter
from tests.test_app import assert_valid_exposition
from tests.traces.otlp_builders import NOW, S, as_json, request, sid

CHAT = {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 100}
TOOL = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "shell"}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


# --- config -----------------------------------------------------------------------------


def test_defaults() -> None:
    t = load_settings(None).traces
    assert t == TraceSettings()
    assert t.enabled and t.window == timedelta(minutes=30) and t.retention == timedelta(days=7)
    assert t.max_body_bytes == 8 * 1024 * 1024 and t.max_rows == 1_000_000
    assert (t.task_attribute, t.session_attribute) == ("kelpie.task_id", "kelpie.session_id")
    assert t.token({}) is None


def test_values(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[traces]\nenabled = false\ntoken_env = "FARM_TOKEN"\nwindow = "30m"\n'
        'totals_window = "12h"\nretention = "3d"\nmax_rows = 5000\nmax_body_bytes = 1024\n'
        'task_attribute = "farm.task"\nsession_attribute = "farm.run"\n',
    )
    t = load_settings(path).traces
    assert not t.enabled and t.token_env == "FARM_TOKEN" and t.token_env_explicit
    assert t.window == timedelta(minutes=30) and t.totals_window == timedelta(hours=12)
    assert t.retention == timedelta(days=3) and (t.max_rows, t.max_body_bytes) == (5000, 1024)
    assert t.options.task_attribute == "farm.task" and t.options.session_attribute == "farm.run"
    assert t.token({"FARM_TOKEN": " abc "}) == "abc"


@pytest.mark.parametrize(
    "text",
    [
        "[traces]\nport = 4318\n",
        "traces = 5\n",
        "[traces]\nenabled = 1\n",
        '[traces]\ntoken_env = ""\n',
        "[traces]\ntask_attribute = 3\n",
        '[traces]\nwindow = "soon"\n',
        '[traces]\nwindow = "0s"\n',
        '[traces]\nretention = "1h"\n',  # shorter than the window
        '[traces]\nretention = "1000000d"\n',  # would overflow now - retention
        "[traces]\nmax_rows = 0\n",
        "[traces]\nmax_body_bytes = true\n",
        '[traces]\nmax_rows = "10"\n',
    ],
)
def test_bad_config(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


def test_explicit_token_env_must_be_set(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FARM_TOKEN", raising=False)
    settings = load_settings(write(tmp_path, '[traces]\ntoken_env = "FARM_TOKEN"\n'))
    with pytest.raises(ConfigError, match="FARM_TOKEN is not set"):
        build_app(settings)


def test_disabled_traces_have_no_ingest(tmp_path: Path) -> None:
    app = build_app(Settings(db_path=None, traces=TraceSettings(enabled=False)))
    assert app.ingest is None and app.span_store is None
    assert "kelpie_spans_received_total" not in app.metrics.render()
    server = make_server("127.0.0.1", 0, app.metrics, app.ingest)
    with serving(server) as base, pytest.raises(urllib.error.HTTPError) as exc:
        post(base, request([S(sid(1), attrs=CHAT)]))
    assert exc.value.code == 404
    app.close()


# --- end to end over HTTP ---------------------------------------------------------------


@pytest.fixture
def serving_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, object, list[datetime]]]:
    monkeypatch.setenv("KELPIE_TRACES_TOKEN", "synthetic-token")
    clock = [NOW]
    app = build_app(Settings(db_path=tmp_path / "kelpie.db"), clock=lambda: clock[0])
    server = make_server("127.0.0.1", 0, app.metrics, app.ingest)
    thread = threading.Thread(target=run, args=(app, server), daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", app, clock
    server.shutdown()
    thread.join(5)


def post(
    base: str,
    body: bytes,
    content_type: str = PROTOBUF,
    token: str | None = None,
    path: str = "/v1/traces",
    **headers: str,
) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{base}{path}", data=body, method="POST")
    req.add_header("Content-Type", content_type)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in headers.items():
        req.add_header(k.replace("_", "-"), v)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


@dataclass
class Serving:
    server: KelpieHTTPServer

    def __enter__(self) -> str:
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def serving(server: KelpieHTTPServer) -> Serving:
    return Serving(server)


def test_end_to_end(serving_app: tuple[str, object, list[datetime]]) -> None:
    base, app, clock = serving_app
    session = {"kelpie.task_id": "T-1", "kelpie.session_id": "run-1"}
    spans = [
        S(sid(1), name="invoke_agent", attrs={"gen_ai.operation.name": "invoke_agent", **session}),
        S(sid(2), parent=sid(1), attrs=CHAT),
        S(sid(3), parent=sid(1), attrs=TOOL),
    ]
    assert post(base, request(spans), token="synthetic-token") == (200, b"")
    more = gzip.compress(as_json([S(sid(4), parent=sid(1), attrs=CHAT)]))
    status, body = post(base, more, JSON, "synthetic-token", Content_Encoding="gzip")
    assert (status, body) == (200, b"{}")
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base, request(spans))
    assert exc.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base, b"{broken", JSON, "synthetic-token")
    assert exc.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as exc:
        post(base, b"{}", JSON, "synthetic-token", path="/v1/metrics")
    assert exc.value.code == 404

    clock[0] = NOW + timedelta(minutes=2)  # totals are recomputed once a minute
    snapshot = app.loop.snapshot_source()  # type: ignore[attr-defined]
    assert [s.kind for s in snapshot.spans] == [
        SpanKind.SESSION,
        SpanKind.LLM,
        SpanKind.TOOL,
        SpanKind.LLM,
    ]
    assert {(s.task_id, s.session_id) for s in snapshot.spans} == {("T-1", "run-1")}
    (totals,) = snapshot.span_totals
    assert (totals.session_id, totals.llm_calls, totals.input_tokens) == ("run-1", 2, 200)
    app.loop.tick_once()  # type: ignore[attr-defined]

    with urllib.request.urlopen(f"{base}/metrics") as resp:
        text = resp.read().decode()
    assert 'kelpie_spans_received_total{kind="llm"} 2' in text
    assert 'kelpie_spans_received_total{kind="task"} 0' in text
    assert 'kelpie_traces_requests_total{code="200"} 2' in text
    assert 'kelpie_traces_requests_total{code="401"} 1' in text
    assert 'kelpie_traces_requests_total{code="400"} 1' in text
    assert "kelpie_span_store_rows 4" in text
    assert_valid_exposition(text)


def test_spans_persist_across_restart(tmp_path: Path) -> None:
    settings = Settings(db_path=tmp_path / "kelpie.db")
    app = build_app(settings, clock=lambda: NOW)
    assert app.ingest is not None and app.span_store is not None
    app.span_store.append_spans([span(1, NOW)], NOW)
    app.close()
    again = build_app(settings, clock=lambda: NOW)
    assert [s.span_id for s in again.loop.snapshot_source().spans] == ["s1"]
    assert "kelpie_span_store_rows 1" in again.metrics.render()
    again.close()


# --- snapshot merge ---------------------------------------------------------------------


def span(n: int, start: datetime, session: str = "run-1") -> Span:
    return Span(
        trace_id="t" * 32,
        span_id=f"s{n}",
        name="tool shell",
        kind=SpanKind.TOOL,
        start=start,
        end=start,
        session_id=session,
        tool="shell",
    )


@dataclass
class SpanAdapter(FakeAdapter):
    spans: tuple[Span, ...] = ()

    def snapshot(self, now: datetime) -> Snapshot:
        return Snapshot(now=now, spans=self.spans)


def test_stored_spans_merge_with_adapter_spans() -> None:
    store = MemorySpanStore()
    stored = [span(1, NOW - timedelta(minutes=5)), span(2, NOW - timedelta(hours=3))]
    store.append_spans(stored, NOW)
    adapter = SpanAdapter(name="hooks", spans=(span(1, NOW - timedelta(minutes=5)), span(3, NOW)))
    window = SpanWindow(store, timedelta(hours=2), timedelta(hours=24))
    snap = merged_snapshot([adapter], Metrics(), lambda: NOW, window)()
    assert [s.span_id for s in snap.spans] == ["s1", "s3"]
    assert [t.spans for t in snap.span_totals] == [2]


class BrokenStore(MemorySpanStore):
    def spans_since(self, after: datetime) -> list[Span]:
        raise OSError("disk I/O error")


def test_span_store_failure_keeps_the_tick(caplog: pytest.LogCaptureFixture) -> None:
    metrics = Metrics()
    metrics.set_traces(True)
    adapter = SpanAdapter(name="hooks", spans=(span(3, NOW),))
    window = SpanWindow(BrokenStore(), timedelta(hours=2), timedelta(hours=24))
    snap = merged_snapshot([adapter], metrics, lambda: NOW, window)()
    assert [s.span_id for s in snap.spans] == ["s3"]
    assert "kelpie_span_store_errors_total 1" in metrics.render()
    assert "disk I/O error" in caplog.text


def test_loop_runs_housekeeping_and_survives_its_failure() -> None:
    calls: list[int] = []

    def housekeeping() -> None:
        calls.append(1)
        raise RuntimeError("prune bug")

    engine = Engine([], config=EngineConfig(interval=timedelta(milliseconds=1)))
    loop = ControlLoop(engine, lambda: Snapshot(now=NOW), Metrics(), housekeeping=housekeeping)
    loop.start()
    try:
        for _ in range(200):
            if len(calls) >= 2:
                break
            time.sleep(0.01)
    finally:
        loop.stop()
    assert len(calls) >= 2


def test_large_ingest_does_not_stall_ticks(tmp_path: Path) -> None:
    """A large export lands while the loop ticks; no tick waits for the whole insert."""
    app = build_app(Settings(db_path=tmp_path / "kelpie.db"), clock=lambda: NOW)
    assert app.ingest is not None
    spans = [
        S(f"{n + 1:016x}", attrs={**TOOL, "kelpie.session_id": f"run-{n % 50}"})
        for n in range(10_000)
    ]
    body = request(spans)
    assert len(body) < app.settings.traces.max_body_bytes
    server = make_server("127.0.0.1", 0, app.metrics, app.ingest)
    durations: list[float] = []
    done = threading.Event()
    with serving(server) as base:
        result: list[int] = []
        sender = threading.Thread(target=lambda: result.append(post(base, body)[0]))
        started = time.monotonic()
        sender.start()
        while not done.is_set():
            t0 = time.monotonic()
            app.loop.tick_once()
            durations.append(time.monotonic() - t0)
            if not sender.is_alive():
                done.set()
            time.sleep(0.01)  # a real loop ticks every 30s; a busy loop only starves the GIL
        sender.join()
        ingest_seconds = time.monotonic() - started
    assert result == [200]
    assert app.span_store is not None and app.span_store.count() == 10_000
    # Ticks read only what changed (other connection); none waits for the whole insert.
    assert len(durations) >= 2
    assert max(durations[:-1]) < max(1.0, ingest_seconds * 0.9)
    app.close()


def test_example_config_has_the_trace_defaults() -> None:
    example = Path(__file__).resolve().parents[2] / "kelpie.example.toml"
    assert load_settings(example).traces == TraceSettings()


def test_status_shows_traces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_TRACES_TOKEN", raising=False)
    settings = load_settings(None)
    loaded = load_rules(settings.engine)
    assert "traces:   POST /v1/traces, open ($KELPIE_TRACES_TOKEN unset)" in render_status(
        settings, loaded
    )
    monkeypatch.setenv("KELPIE_TRACES_TOKEN", "x")
    assert "bearer token from $KELPIE_TRACES_TOKEN" in render_status(settings, loaded)
    off = Settings(traces=TraceSettings(enabled=False))
    assert "traces:   disabled" in render_status(off, loaded)
    strict = Settings(traces=TraceSettings(token_env="NOPE_UNSET", token_env_explicit=True))
    assert "NOT ready" in render_status(strict, loaded)
