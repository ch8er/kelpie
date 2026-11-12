"""POST /v1/metrics: Claude Code's cost and token metrics as a spend source (#425 item 4).

Decoding (both encodings), what is kept and rejected, the same guards as /v1/traces, retries
kept once, and usage for budgets only for sessions with no model call in traces."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)
from opentelemetry.proto.metrics.v1 import metrics_pb2

from kelpie.core.spans import MemorySpanStore
from kelpie.core.storage import SqliteSpanStore
from kelpie.traces import metrics as m
from kelpie.traces.ingest import Reply, ScopedToken, TraceIngest
from kelpie.traces.otlp import JSON, PROTOBUF
from kelpie.traces.scope import ScopeGuard
from tests.traces.test_ingest import Headers
from tests.traces.test_span_store import span
from tests.traces.test_token_scopes import SCOPES

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
NS = int(NOW.timestamp()) * 1_000_000_000
TOKEN = "synthetic-metrics-token-0123456789"


def kv(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def point(value: float, session: str | None = "s-1", **attrs: str) -> dict[str, Any]:
    found = [kv("session.id", session)] if session else []
    found += [kv(k.replace("_", "."), v) for k, v in attrs.items()]
    return {
        "attributes": found,
        "startTimeUnixNano": str(NS - 60_000_000_000),
        "timeUnixNano": str(NS),
        "asDouble": value,
    }


def metric(name: str, points: list[dict[str, Any]], temporality: int = 1) -> dict[str, Any]:
    return {
        "name": name,
        "sum": {"aggregationTemporality": temporality, "isMonotonic": True, "dataPoints": points},
    }


def body(*metrics: dict[str, Any], resource: list[dict[str, Any]] | None = None) -> bytes:
    return json.dumps(
        {
            "resourceMetrics": [
                {
                    "resource": {"attributes": resource or [kv("service.name", "claude-code")]},
                    "scopeMetrics": [{"metrics": list(metrics)}],
                }
            ]
        }
    ).encode()


CLAUDE = body(
    metric(
        m.TOKENS,
        [
            point(100, model="claude-x", type="input"),
            point(50, model="claude-x", type="output"),
            point(400, model="claude-x", type="cacheRead"),
            point(25, model="claude-x", type="cacheCreation"),
        ],
    ),
    metric(m.COST, [point(0.25, model="claude-x")]),
    {"name": "claude_code.session.count", "sum": {"aggregationTemporality": 1,
     "dataPoints": [point(1)]}},
)  # fmt: skip


def test_json_points_of_claude_codes_two_metrics_are_kept() -> None:
    decoded = m.decode(CLAUDE, JSON)
    kinds = {(p.kind, p.value) for p in decoded.points}
    assert kinds == {
        ("input", 100), ("output", 50), ("cache_read", 400), ("cache_creation", 25),
        ("cost_usd", 0.25),
    }  # fmt: skip
    assert decoded.ignored == 1 and not decoded.rejected
    assert {p.session_id for p in decoded.points} == {"s-1"}
    assert {p.model for p in decoded.points} == {"claude-x"}


def test_protobuf_points_decode_the_same() -> None:
    request = ExportMetricsServiceRequest()
    rm = request.resource_metrics.add()
    sm = rm.scope_metrics.add()
    metric_ = sm.metrics.add(name=m.TOKENS)
    metric_.sum.aggregation_temporality = metrics_pb2.AGGREGATION_TEMPORALITY_DELTA
    p = metric_.sum.data_points.add(time_unix_nano=NS, start_time_unix_nano=NS - 1, as_int=7)
    for key, value in (("session.id", "s-2"), ("type", "output"), ("model", "claude-x")):
        p.attributes.add(key=key).value.string_value = value
    other = sm.metrics.add(name="claude_code.lines_of_code.count")
    other.sum.data_points.add(time_unix_nano=NS, as_int=3)
    cumulative = sm.metrics.add(name=m.COST)
    cumulative.sum.aggregation_temporality = metrics_pb2.AGGREGATION_TEMPORALITY_CUMULATIVE
    cumulative.sum.data_points.add(time_unix_nano=NS, as_double=1.0)
    decoded = m.decode(request.SerializeToString(), PROTOBUF)
    [only] = decoded.points
    assert (only.session_id, only.kind, only.value, only.model) == ("s-2", "output", 7, "claude-x")
    assert decoded.ignored == 1 and decoded.rejected == {"not_delta": 1}


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (body(metric(m.COST, [point(1.0)], temporality=2)), "not_delta"),
        (body(metric(m.COST, [point(1.0, session=None)])), "no_session"),
        (body(metric(m.COST, [point(-1.0)])), "bad_value"),
        (body(metric(m.COST, [point(1e9)])), "bad_value"),
        (body(metric(m.TOKENS, [point(5, type="reasoning")])), "unknown_type"),
        (body(metric(m.TOKENS, [point(5)])), "unknown_type"),
        (body({"name": m.COST, "gauge": {"dataPoints": [point(1.0)]}}), "not_a_sum"),
    ],
)
def test_points_that_cannot_count_are_rejected_with_a_reason(payload: bytes, reason: str) -> None:
    decoded = m.decode(payload, JSON)
    assert not decoded.points and decoded.rejected == {reason: 1}


def test_other_attributes_are_dropped_and_the_task_is_taken() -> None:
    payload = body(
        metric(m.COST, [point(1.0, user_email="a@example.invalid", organization_id="o")]),
        resource=[kv("kelpie.task_id", "T-7")],
    )
    [p] = m.decode(payload, JSON).points
    assert (p.task_id, p.model) == ("T-7", None)
    assert set(p.__slots__) == {  # type: ignore[attr-defined]
        "session_id", "kind", "value", "time_ns", "start_ns", "model", "task_id",
    }  # fmt: skip


@pytest.mark.parametrize("payload", [b"{broken", b"[]", b'{"resourceMetrics": 3}'])
def test_a_body_that_is_not_an_export_is_400(payload: bytes) -> None:
    reply = post(TraceIngest(MemorySpanStore(), lambda: NOW, token=TOKEN), payload)
    assert reply.status == 400


def post(target: TraceIngest, payload: bytes, token: str | None = TOKEN, **h: str) -> Reply:
    headers = Headers({"Content-Type": JSON, "Content-Length": str(len(payload))})
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(h)
    return target.handle_metrics(headers, io.BytesIO(payload).read)


class Observer:
    def __init__(self) -> None:
        self.codes: list[int] = []
        self.points: list[tuple[int, int, dict[str, int]]] = []

    def traces_request(self, code: int) -> None:  # not called for metrics
        raise AssertionError("a metrics request is not a traces request")

    def metrics_request(self, code: int) -> None:
        self.codes.append(code)

    def spans_received(self, kind: str, count: int) -> None: ...

    def spans_rejected(self, reason: str, count: int) -> None: ...

    def metric_points(self, stored: int, ignored: int, rejected: dict[str, int]) -> None:
        self.points.append((stored, ignored, rejected))


@pytest.fixture
def store(tmp_path: Path) -> SqliteSpanStore:
    found = SqliteSpanStore(tmp_path / "kelpie.db")
    yield found  # type: ignore[misc]
    found.close()


def test_metrics_are_stored_once_and_counted(store: SqliteSpanStore) -> None:
    observer = Observer()
    target = TraceIngest(store, lambda: NOW, observer=observer, token=TOKEN)  # type: ignore[arg-type]
    assert post(target, CLAUDE).status == 200
    assert post(target, CLAUDE).status == 200  # an exporter's retry: kept once
    assert observer.codes == [200, 200]
    assert observer.points[0] == (5, 1, {})
    [(usage,)] = store.metered_usage_since(NOW + timedelta(minutes=1), [timedelta(hours=1)])
    assert (usage.session_id, usage.model, usage.calls) == ("s-1", "claude-x", 0)
    # Input includes cache reads and writes, as a span's does.
    assert (usage.input_tokens, usage.output_tokens) == (525, 50)
    assert (usage.cached_tokens, usage.cache_write_tokens) == (400, 25)
    assert store.metered_between(NOW - timedelta(hours=1), NOW + timedelta(hours=1)) == (
        ("claude-x", 575, 0.25),
    )


def test_a_session_with_model_calls_in_traces_counts_from_traces(store: SqliteSpanStore) -> None:
    target = TraceIngest(store, lambda: NOW, token=TOKEN)  # type: ignore[arg-type]
    assert post(target, CLAUDE).status == 200
    store.append_spans(
        [span(1, NOW - timedelta(minutes=5), end=NOW, session="s-1", tokens=10)], NOW
    )
    later = NOW + timedelta(minutes=1)
    assert store.metered_usage_since(later, [timedelta(hours=1)]) == [()]
    assert store.metered_between(NOW - timedelta(hours=1), later) == ()


def test_metric_points_are_pruned_with_the_spans(store: SqliteSpanStore) -> None:
    target = TraceIngest(store, lambda: NOW, token=TOKEN)  # type: ignore[arg-type]
    assert post(target, CLAUDE).status == 200
    store.prune(NOW + timedelta(seconds=1), 1000)
    assert store.metered_usage_since(NOW, [timedelta(days=1)]) == [()]


def test_a_store_that_never_wrote_metrics_reads_none(tmp_path: Path) -> None:
    SqliteSpanStore(tmp_path / "k.db").close()
    import sqlite3

    with sqlite3.connect(tmp_path / "k.db") as db:
        db.execute("DROP TABLE metric_points")
    reader = SqliteSpanStore(tmp_path / "k.db", readonly=True)
    try:
        assert reader.metered_usage_since(NOW, [timedelta(days=1)]) == [()]
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("token", "headers", "status"),
    [
        (None, {}, 401),
        ("wrong-token-value-0123456789", {}, 401),
        (TOKEN, {"Content-Type": "text/plain"}, 415),
        (TOKEN, {"Content-Encoding": "br"}, 415),
        (TOKEN, {"Content-Length": str(10**9)}, 413),
    ],
)
def test_the_same_guards_as_traces(token: str | None, headers: dict[str, str], status: int) -> None:
    target = TraceIngest(MemorySpanStore(), lambda: NOW, token=TOKEN)
    assert post(target, CLAUDE, token, **headers).status == status


def test_a_web_page_cannot_post_metrics_to_an_open_ingest() -> None:
    target = TraceIngest(MemorySpanStore(), lambda: NOW)
    assert post(target, CLAUDE, None, Origin="https://example.invalid").status == 403
    assert post(target, CLAUDE, None).status == 200


def test_a_per_agent_token_cannot_post_metrics() -> None:
    store = MemorySpanStore()
    guard = ScopeGuard(SCOPES, store=store)
    target = TraceIngest(
        store, lambda: NOW, scoped=[ScopedToken(next(iter(SCOPES)), TOKEN)], guard=guard
    )
    reply = post(target, CLAUDE)
    assert reply.status == 403 and b"shared traces token" in reply.body


def test_the_reply_is_an_export_metrics_response_with_partial_success() -> None:
    target = TraceIngest(MemorySpanStore(), lambda: NOW, token=TOKEN)
    payload = body(metric(m.COST, [point(1.0), point(1.0, session=None)]))
    reply = post(target, payload)
    assert reply.status == 200
    assert json.loads(reply.body) == {
        "partialSuccess": {"rejectedDataPoints": "1", "errorMessage": "1 no_session"}
    }
    pb = m.encode_response(PROTOBUF, 2, "2 not_delta")
    parsed = ExportMetricsServiceResponse.FromString(pb)
    assert parsed.partial_success.rejected_data_points == 2


def test_points_outside_the_span_age_limits_are_rejected() -> None:
    target = TraceIngest(MemorySpanStore(), lambda: NOW + timedelta(days=30), token=TOKEN)
    reply = post(target, body(metric(m.COST, [point(1.0)])))
    assert reply.status == 200 and b"invalid_time" in reply.body


def test_budgets_read_metered_usage_beside_the_spans(store: SqliteSpanStore) -> None:
    from kelpie.traces.store import SpanWindow

    target = TraceIngest(store, lambda: NOW, token=TOKEN)  # type: ignore[arg-type]
    assert post(target, CLAUDE).status == 200
    store.append_spans([span(1, NOW, end=NOW, session="s-9", tokens=10)], NOW)
    window = SpanWindow(store, timedelta(hours=1), timedelta(hours=1))
    rows = window.usage(NOW + timedelta(minutes=1), [timedelta(hours=1)])[timedelta(hours=1)]
    assert [(r.session_id, r.calls) for r in rows] == [("s-1", 0), ("s-9", 1)]


@pytest.mark.parametrize("start", [2**64 - 1, NS + 1])
def test_a_start_time_sqlite_cannot_hold_or_after_the_point_is_rejected(start: int) -> None:
    payload = body(metric(m.COST, [{**point(1.0), "startTimeUnixNano": str(start)}]))
    assert m.decode(payload, JSON).rejected == {"invalid_time": 1}


def test_a_retry_of_a_point_without_a_model_is_kept_once(store: SqliteSpanStore) -> None:
    target = TraceIngest(store, lambda: NOW, token=TOKEN)  # type: ignore[arg-type]
    payload = body(metric(m.TOKENS, [point(5, type="input")]))
    assert post(target, payload).status == 200
    assert post(target, payload).status == 200
    [(usage,)] = store.metered_usage_since(NOW + timedelta(minutes=1), [timedelta(hours=1)])
    assert (usage.model, usage.input_tokens) == (None, 5)


def test_parts_of_one_counter_in_one_export_are_summed(store: SqliteSpanStore) -> None:
    target = TraceIngest(store, lambda: NOW, token=TOKEN)  # type: ignore[arg-type]
    parts = [point(5, type="input", model="claude-x", query_source="a"),
             point(7, type="input", model="claude-x", query_source="b")]  # fmt: skip
    assert post(target, body(metric(m.TOKENS, parts))).status == 200
    [(usage,)] = store.metered_usage_since(NOW + timedelta(minutes=1), [timedelta(hours=1)])
    assert usage.input_tokens == 12


def test_metric_points_beyond_the_row_cap_go_oldest_first(store: SqliteSpanStore) -> None:
    rows = [("s-1", None, "m", "input", 1.0, NS + n, NS + n) for n in range(10)]
    assert store.append_metric_points(rows) == 10
    store.prune(NOW - timedelta(days=1), 4)
    [(usage,)] = store.metered_usage_since(NOW + timedelta(minutes=1), [timedelta(hours=1)])
    assert usage.input_tokens == 4


def test_the_session_is_found_as_a_spans_is(store: SqliteSpanStore) -> None:
    # The host names its sessions with kelpie.session_id: its spans and its metric points
    # land on the same session, so the traces win and nothing counts twice.
    target = TraceIngest(store, lambda: NOW, token=TOKEN)  # type: ignore[arg-type]
    payload = body(
        metric(m.TOKENS, [point(5, type="input", model="claude-x")]),
        resource=[kv("kelpie.session_id", "host-7")],
    )
    [p] = m.decode(payload, JSON).points
    assert p.session_id == "host-7"
    assert post(target, payload).status == 200
    store.append_spans([span(1, NOW, end=NOW, session="host-7", tokens=10)], NOW)
    assert store.metered_usage_since(NOW + timedelta(minutes=1), [timedelta(hours=1)]) == [()]


def test_gzip_bodies_and_their_limit() -> None:
    import gzip

    target = TraceIngest(MemorySpanStore(), lambda: NOW, token=TOKEN, max_body_bytes=4096)
    packed = gzip.compress(CLAUDE)
    assert post(target, packed, **{"Content-Encoding": "gzip"}).status == 200
    bomb = gzip.compress(b" " * 100_000)
    assert post(target, bomb, **{"Content-Encoding": "gzip"}).status == 413


def test_a_store_that_cannot_write_is_503_so_the_exporter_retries() -> None:
    class Broken(MemorySpanStore):
        def append_metric_points(self, rows: object) -> int:
            raise OSError("disk full")

    target = TraceIngest(Broken(), lambda: NOW, token=TOKEN)
    assert post(target, CLAUDE).status == 503


def test_points_without_a_model_are_reported_as_unknown(store: SqliteSpanStore) -> None:
    target = TraceIngest(store, lambda: NOW, token=TOKEN)  # type: ignore[arg-type]
    assert post(target, body(metric(m.COST, [point(0.5)]))).status == 200
    window = (NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert store.metered_between(*window) == ((None, 0, 0.5),)
