"""Build synthetic OTLP ``ExportTraceServiceRequest`` bodies in protobuf and OTLP/JSON."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from google.protobuf import json_format
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
TRACE = "0af7651916cd43dd8448eb211c80319c"


def sid(n: int) -> str:
    """A synthetic 16-hex-digit span id."""
    return f"{n:016x}"


def ns(at: datetime) -> int:
    return int(at.timestamp()) * 1_000_000_000 + at.microsecond * 1000


@dataclass
class S:
    span_id: str
    name: str = "chat synthetic-model"
    attrs: Mapping[str, Any] = field(default_factory=dict)
    parent: str | None = None
    trace: str = TRACE
    start: datetime = NOW - timedelta(minutes=5)
    duration: timedelta = timedelta(seconds=2)
    error: bool = False


def any_value(value: Any) -> AnyValue:
    v = AnyValue()
    if isinstance(value, bool):
        v.bool_value = value
    elif isinstance(value, int):
        v.int_value = value
    elif isinstance(value, float):
        v.double_value = value
    elif isinstance(value, bytes):
        v.bytes_value = value
    elif isinstance(value, list):
        v.array_value.values.extend(any_value(x) for x in value)
    elif isinstance(value, dict):
        v.kvlist_value.values.extend(KeyValue(key=k, value=any_value(x)) for k, x in value.items())
    else:
        v.string_value = str(value)
    return v


def request(spans: Sequence[S], resource: Mapping[str, Any] | None = None) -> bytes:
    """A serialized protobuf request; see :func:`as_json` for the same in OTLP/JSON."""
    return build(spans, resource).SerializeToString()


def build(spans: Sequence[S], resource: Mapping[str, Any] | None = None) -> Any:
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    for k, x in (resource or {"service.name": "synthetic-agent"}).items():
        rs.resource.attributes.add(key=k, value=any_value(x))
    ss = rs.scope_spans.add()
    ss.scope.name = "synthetic.instrumentation"
    for s in spans:
        p = ss.spans.add()
        p.trace_id = bytes.fromhex(s.trace)
        p.span_id = bytes.fromhex(s.span_id)
        if s.parent:
            p.parent_span_id = bytes.fromhex(s.parent)
        p.name = s.name
        p.kind = 1
        p.start_time_unix_nano = ns(s.start)
        p.end_time_unix_nano = ns(s.start + s.duration)
        if s.error:
            p.status.code = 2
        for k, x in s.attrs.items():
            p.attributes.add(key=k, value=any_value(x))
    return req


def as_json(spans: Sequence[S], resource: Mapping[str, Any] | None = None) -> bytes:
    """OTLP/JSON: the proto3 JSON mapping with hex ids and integer enums."""
    data = json_format.MessageToDict(build(spans, resource), use_integers_for_enums=True)
    for rs in data.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                for key in ("traceId", "spanId", "parentSpanId"):
                    if key in span:
                        span[key] = base64.b64decode(span[key]).hex()
    return json.dumps(data).encode()
