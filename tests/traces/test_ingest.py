"""TraceIngest: auth, media types, size limits, gzip, partial success, store failures."""

from __future__ import annotations

import gzip
import io
import json
from collections import Counter
from datetime import datetime
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from kelpie.core.model import Span, SpanKind
from kelpie.core.spans import MemorySpanStore
from kelpie.traces.ingest import Reply, TraceIngest
from kelpie.traces.otlp import JSON, PROTOBUF
from tests.traces.otlp_builders import NOW, S, as_json, request, sid

CHAT = {"gen_ai.operation.name": "chat", "gen_ai.usage.input_tokens": 10}
TOOL = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "shell"}


class Headers(dict[str, str]):
    def get(self, name: str, default: str | None = None) -> str | None:  # type: ignore[override]
        for key, value in self.items():
            if key.lower() == name.lower():
                return value
        return default


class Observer:
    def __init__(self) -> None:
        self.codes: list[int] = []
        self.received: Counter[str] = Counter()
        self.rejected: Counter[str] = Counter()

    def traces_request(self, code: int) -> None:
        self.codes.append(code)

    def spans_received(self, kind: str, count: int) -> None:
        self.received[kind] += count

    def spans_rejected(self, reason: str, count: int) -> None:
        self.rejected[reason] += count


def ingest(**kw: Any) -> tuple[TraceIngest, MemorySpanStore, Observer]:
    store, observer = MemorySpanStore(), Observer()
    return TraceIngest(store=store, clock=lambda: NOW, observer=observer, **kw), store, observer


def post(
    target: TraceIngest,
    body: bytes,
    content_type: str = PROTOBUF,
    **headers: str,
) -> Reply:
    h = Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    h.update({k.replace("_", "-"): v for k, v in headers.items()})
    return target.handle(h, io.BytesIO(body).read)


SPANS = [S(sid(1), attrs=CHAT), S(sid(2), attrs=TOOL, parent=sid(1)), S(sid(3), name="http")]


@pytest.mark.parametrize("encoding", [PROTOBUF, JSON])
def test_accepts_both_encodings(encoding: str) -> None:
    target, store, observer = ingest()
    body = request(SPANS) if encoding == PROTOBUF else as_json(SPANS)
    reply = post(target, body, f"{encoding}; charset=utf-8")
    assert reply.status == 200 and reply.content_type == encoding
    assert reply.body == (b"" if encoding == PROTOBUF else b"{}")
    assert [s.kind for s in store.spans_since(datetime.min.replace(tzinfo=NOW.tzinfo))] == [
        SpanKind.LLM,
        SpanKind.TOOL,
    ]
    assert observer.received == Counter({"llm": 1, "tool": 1, "ignored": 1})
    assert observer.codes == [200]


def test_partial_success_reports_rejected_spans() -> None:
    target, store, observer = ingest()
    payload = json.loads(as_json([S(sid(1), attrs=CHAT)]))
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans.append({**spans[0], "spanId": "nothex"})
    spans.append({**spans[0], "spanId": sid(9), "startTimeUnixNano": "0"})
    reply = post(target, json.dumps(payload).encode(), JSON)
    assert reply.status == 200
    assert json.loads(reply.body) == {
        "partialSuccess": {"rejectedSpans": "2", "errorMessage": "1 invalid_id; 1 invalid_time"}
    }
    assert store.count() == 1
    assert observer.rejected == Counter({"invalid_id": 1, "invalid_time": 1})


def test_partial_success_in_protobuf() -> None:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    req = ExportTraceServiceRequest.FromString(request([S(sid(1), attrs=CHAT)]))
    req.resource_spans[0].scope_spans[0].spans[0].span_id = b"\x00" * 8
    reply = post(ingest()[0], req.SerializeToString())
    response = ExportTraceServiceResponse.FromString(reply.body)
    assert reply.status == 200 and response.partial_success.rejected_spans == 1


def test_retry_is_idempotent() -> None:
    target, store, _ = ingest()
    body = request(SPANS)
    post(target, body)
    post(target, body)
    assert store.count() == 2


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": "Basic c2VjcmV0"}, 401),
        ({"Authorization": "Bearer s3cret-token"}, 200),
        ({"authorization": "  Bearer s3cret-token "}, 200),
    ],
)
def test_bearer_token(headers: dict[str, str], status: int) -> None:
    target, store, _ = ingest(token="s3cret-token")
    h = Headers({"Content-Type": PROTOBUF, **headers})
    body = request(SPANS)
    h["Content-Length"] = str(len(body))
    reply = target.handle(h, io.BytesIO(body).read)
    assert reply.status == status
    if status == 401:
        assert reply.headers == {"WWW-Authenticate": "Bearer"} and reply.close
        assert store.count() == 0
        assert reply.body.startswith(b"\x08\x10")  # google.rpc.Status code 16


def test_unsupported_media_type_and_encoding() -> None:
    target = ingest()[0]
    assert post(target, b"{}", "text/plain").status == 415
    assert post(target, b"{}", "").status == 415
    assert post(target, b"{}", JSON, Content_Encoding="br").status == 415


def test_size_limits() -> None:
    target = ingest(max_body_bytes=1000)[0]
    reads: list[int] = []

    def read(n: int) -> bytes:
        reads.append(n)
        return b""

    reply = target.handle(Headers({"Content-Type": PROTOBUF, "Content-Length": "1001"}), read)
    assert reply.status == 413 and reply.close and reads == []  # body never read
    missing = target.handle(Headers({"Content-Type": PROTOBUF}), read)
    assert missing.status == 411
    for bad in ("-1", "lots"):
        h = Headers({"Content-Type": PROTOBUF, "Content-Length": bad})
        assert target.handle(h, read).status == 400


def test_gzip() -> None:
    target, store, _ = ingest(max_body_bytes=4096)
    body = gzip.compress(request(SPANS))
    assert post(target, body, PROTOBUF, Content_Encoding="gzip").status == 200
    assert store.count() == 2
    bomb = gzip.compress(b"\x00" * 100_000)  # small on the wire, large inflated
    assert len(bomb) < 4096
    assert post(target, bomb, PROTOBUF, Content_Encoding="gzip").status == 413
    assert post(target, b"not gzip", PROTOBUF, Content_Encoding="gzip").status == 400
    truncated = gzip.compress(request(SPANS))[:-12]
    assert post(target, truncated, PROTOBUF, Content_Encoding="gzip").status == 400


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"{not json", JSON),
        (b"[1, 2]", JSON),
        (b'{"resourceSpans": [{"scopeSpans": "x"}]}', JSON),
        (b"\x0a\xff\xff\xff\xff\x0f", PROTOBUF),
    ],
)
def test_bad_payloads_are_400_in_the_request_encoding(body: bytes, content_type: str) -> None:
    reply = post(ingest()[0], body, content_type)
    assert reply.status == 400 and reply.content_type == content_type


class BrokenStore(MemorySpanStore):
    def append_spans(self, spans: Any, received_at: datetime) -> int:
        raise OSError("disk full")


def test_store_failure_is_503_so_exporters_retry() -> None:
    target = TraceIngest(store=BrokenStore(), clock=lambda: NOW)
    assert post(target, request(SPANS)).status == 503


def test_without_observer() -> None:
    target = TraceIngest(store=MemorySpanStore(), clock=lambda: NOW)
    assert post(target, request(SPANS)).status == 200


def test_spans_are_kelpie_spans() -> None:
    target, store, _ = ingest()
    post(target, request([S(sid(1), attrs={**CHAT, "kelpie.session_id": "run-1"})]))
    (s,) = store.spans_since(NOW.replace(year=2000))
    assert isinstance(s, Span) and s.session_id == "run-1" and s.input_tokens == 10
