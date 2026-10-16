"""An agent's OpenTelemetry exporter: GenAI model-call spans as an OTLP/protobuf request.

What an instrumented agent sends to Kelpie's ``/v1/traces`` for each model call, following
the OpenTelemetry GenAI semantic conventions (``gen_ai.operation.name = chat``, the model,
token usage) with the task and session it belongs to (``kelpie.task_id``,
``kelpie.session_id``). No prompt or completion: Kelpie would drop them anyway.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

CONTENT_TYPE = "application/x-protobuf"


@dataclass(frozen=True, slots=True)
class ModelCall:
    task_id: str
    session_id: str
    model: str
    input_tokens: int
    output_tokens: int
    at: datetime
    duration: timedelta = timedelta(seconds=3)


def _id(text: str, size: int) -> bytes:
    return hashlib.sha256(text.encode()).digest()[:size]


def _ns(at: datetime) -> int:
    return int(at.timestamp() * 1_000_000_000)


def request(calls: Sequence[ModelCall], service: str = "simulated-agent") -> bytes:
    """One export request with a span per call."""
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.add(key="service.name").value.string_value = service
    scope = rs.scope_spans.add()
    scope.scope.name = "kelpie.sim"
    for call in calls:
        key = f"{call.session_id}/{call.at.isoformat()}"
        span = scope.spans.add()
        span.trace_id = _id(call.session_id, 16)
        span.span_id = _id(key, 8)
        span.name = f"chat {call.model}"
        span.kind = 3  # CLIENT
        span.start_time_unix_nano = _ns(call.at)
        span.end_time_unix_nano = _ns(call.at + call.duration)
        for name, text in (
            ("gen_ai.operation.name", "chat"),
            ("gen_ai.request.model", call.model),
            ("kelpie.task_id", call.task_id),
            ("kelpie.session_id", call.session_id),
        ):
            span.attributes.add(key=name).value.string_value = text
        for name, count in (
            ("gen_ai.usage.input_tokens", call.input_tokens),
            ("gen_ai.usage.output_tokens", call.output_tokens),
        ):
            span.attributes.add(key=name).value.int_value = count
    return req.SerializeToString()
