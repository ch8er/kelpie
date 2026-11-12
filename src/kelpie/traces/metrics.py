"""Claude Code's OpenTelemetry cost and token metrics: ``POST /v1/metrics`` (#425 item 4).

Claude Code exports ``claude_code.token.usage`` (tokens, by ``type``: ``input``,
``output``, ``cacheRead``, ``cacheCreation``) and ``claude_code.cost.usage`` (USD) as OTLP
sums, each data point carrying ``session.id`` and ``model``. Kelpie keeps those two and
nothing else: every other metric is counted as ignored, never stored.

What is taken from a data point: the session (``session.id``, required: without it the
spend cannot be told apart from the traces of the same session), the model, the token
type, the task (``kelpie.task_id``, on the point or the resource, as for spans), the time
and the value. Every other attribute (user, organization, account, terminal) is dropped.
Only **delta** temporality is accepted (Claude Code's default): a cumulative point would
count the whole session again on every export, so it is rejected with a reason
(``OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta``). A point with a negative,
non-finite or implausibly large value, or a time outside the span age limits, is rejected
too.

Both OTLP encodings are accepted, like ``/v1/traces`` (:mod:`kelpie.traces.otlp`); ingest
applies the same token, ``Origin``, size, media type and slot checks before decoding
(:meth:`kelpie.traces.ingest.TraceIngest.handle_metrics`).

How the points count: :meth:`kelpie.core.storage.SqliteSpanStore.metered_usage_since`
turns them into token usage for budgets for sessions that have no model call in traces in
the window, so a session exported both ways is counted once, from its traces.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from google.protobuf.message import DecodeError as ProtobufDecodeError
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
    ExportMetricsServiceResponse,
)

from kelpie.traces import otlp

PATH = "/v1/metrics"
TOKENS = "claude_code.token.usage"
COST = "claude_code.cost.usage"
DELTA = 1  # AggregationTemporality.AGGREGATION_TEMPORALITY_DELTA
# Token types as Claude Code names them -> the kinds Kelpie stores.
TOKEN_TYPES = {
    "input": "input",
    "output": "output",
    "cacheRead": "cache_read",
    "cacheCreation": "cache_creation",
}
COST_KIND = "cost_usd"
MAX_TOKENS_PER_POINT = 1_000_000_000
MAX_USD_PER_POINT = 100_000.0
MAX_ID = 256  # characters of a session id, task id or model
TASK_ATTRIBUTE = "kelpie.task_id"


@dataclass(frozen=True, slots=True)
class MetricPoint:
    """One delta data point of a Claude Code metric, as stored."""

    session_id: str
    kind: str  # input | output | cache_read | cache_creation | cost_usd
    value: float
    time_ns: int
    start_ns: int
    model: str | None = None
    task_id: str | None = None

    @property
    def at(self) -> datetime:
        return datetime.fromtimestamp(self.time_ns / 1e9, tz=UTC)


@dataclass(slots=True)
class DecodedMetrics:
    points: list[MetricPoint] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)  # reason -> data points
    ignored: int = 0  # data points of metrics Kelpie does not read


def decode(body: bytes, content_type: str, task_attribute: str = TASK_ATTRIBUTE) -> DecodedMetrics:
    """Decode an ``ExportMetricsServiceRequest``; ``content_type`` is :data:`otlp.JSON` or
    :data:`otlp.PROTOBUF`. :class:`kelpie.traces.otlp.DecodeError` when it is not one."""
    if content_type == otlp.PROTOBUF:
        return _from_protobuf(body, task_attribute)
    if content_type == otlp.JSON:
        return _from_json(body, task_attribute)
    raise ValueError(f"unsupported content type {content_type!r}")


def _from_protobuf(body: bytes, task_attribute: str) -> DecodedMetrics:
    request = ExportMetricsServiceRequest()
    try:
        request.ParseFromString(body)
    except (ProtobufDecodeError, RuntimeError, ValueError, RecursionError) as exc:
        raise otlp.DecodeError(f"invalid protobuf ExportMetricsServiceRequest: {exc}") from exc
    out = DecodedMetrics()
    for rm in request.resource_metrics:
        resource = otlp._pb_attributes(rm.resource.attributes)
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                kind = m.WhichOneof("data")
                points = getattr(m, kind).data_points if kind is not None else ()
                if m.name not in (TOKENS, COST):
                    out.ignored += len(points)
                    continue
                if kind != "sum":
                    out.rejected["not_a_sum"] += len(points)
                    continue
                for p in m.sum.data_points:
                    which = p.WhichOneof("value")
                    value: float | None = (
                        float(p.as_double) if which == "as_double" else
                        float(p.as_int) if which == "as_int" else None
                    )  # fmt: skip
                    _add(
                        out,
                        m.name,
                        m.sum.aggregation_temporality,
                        otlp._pb_attributes(p.attributes),
                        resource,
                        value,
                        p.time_unix_nano,
                        p.start_time_unix_nano,
                        task_attribute,
                    )
    return out


def _from_json(body: bytes, task_attribute: str) -> DecodedMetrics:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as exc:
        raise otlp.DecodeError(f"invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise otlp.DecodeError("an ExportMetricsServiceRequest must be a JSON object")
    out = DecodedMetrics()
    for rm in otlp._list(payload, "resourceMetrics", "resource_metrics"):
        resource_obj = otlp._get(rm, "resource")
        resource = otlp._json_attributes(resource_obj) if isinstance(resource_obj, dict) else {}
        for sm in otlp._list(rm, "scopeMetrics", "scope_metrics"):
            for m in otlp._list(sm, "metrics"):
                if not isinstance(m, dict):
                    out.rejected["malformed"] += 1
                    continue
                total = otlp._get(m, "sum")
                if m.get("name") not in (TOKENS, COST):
                    out.ignored += sum(
                        len(otlp._list(d, "dataPoints", "data_points"))
                        for d in (m.get(k) for k in _DATA_KINDS)
                        if isinstance(d, dict)
                    )
                    continue
                if not isinstance(total, dict):
                    out.rejected["not_a_sum"] += 1
                    continue
                try:
                    temporality = otlp._json_int(
                        otlp._get(total, "aggregationTemporality", "aggregation_temporality")
                    )
                except (TypeError, ValueError):
                    temporality = 0
                for p in otlp._list(total, "dataPoints", "data_points"):
                    if not isinstance(p, dict):
                        out.rejected["malformed"] += 1
                        continue
                    try:
                        value = _json_number(p)
                        time_ns = otlp._json_int(otlp._get(p, "timeUnixNano", "time_unix_nano"))
                        start_ns = otlp._json_int(
                            otlp._get(p, "startTimeUnixNano", "start_time_unix_nano")
                        )
                    except (TypeError, ValueError, OverflowError):
                        out.rejected["malformed"] += 1
                        continue
                    _add(
                        out,
                        str(m["name"]),
                        temporality,
                        otlp._json_attributes(p),
                        resource,
                        value,
                        time_ns,
                        start_ns,
                        task_attribute,
                    )
    return out


_DATA_KINDS = ("gauge", "sum", "histogram", "exponentialHistogram", "summary")


def _json_number(p: Mapping[str, Any]) -> float | None:
    if (x := otlp._get(p, "asDouble", "as_double")) is not None:
        if isinstance(x, bool):
            raise ValueError("expected a number")
        return float(x)
    if (x := otlp._get(p, "asInt", "as_int")) is not None:
        return float(otlp._json_int(x))
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() and len(value.strip()) <= MAX_ID:
        return value.strip()
    return None


def _add(
    out: DecodedMetrics,
    name: str,
    temporality: int,
    attributes: Mapping[str, Any],
    resource: Mapping[str, Any],
    value: float | None,
    time_ns: int,
    start_ns: int,
    task_attribute: str,
) -> None:
    if temporality != DELTA:
        out.rejected["not_delta"] += 1
        return
    if value is None or not math.isfinite(value) or value < 0:
        out.rejected["bad_value"] += 1
        return
    if name == COST:
        kind: str | None = COST_KIND
        top = MAX_USD_PER_POINT
    else:
        kind = TOKEN_TYPES.get(str(attributes.get("type", "")))
        top = MAX_TOKENS_PER_POINT
    if kind is None:
        out.rejected["unknown_type"] += 1
        return
    if value > top:
        out.rejected["bad_value"] += 1
        return
    session = _text(attributes.get("session.id")) or _text(resource.get("session.id"))
    if session is None:
        out.rejected["no_session"] += 1
        return
    if time_ns <= 0:
        out.rejected["invalid_time"] += 1
        return
    task = _text(attributes.get(task_attribute)) or _text(resource.get(task_attribute))
    model = _text(attributes.get("model"))
    out.points.append(MetricPoint(session, kind, value, time_ns, max(0, start_ns), model, task))


def plausible(point: MetricPoint, oldest_ns: int, latest_ns: int) -> bool:
    """A point's time within [oldest, latest], compared as integers like spans' times."""
    return oldest_ns <= point.time_ns <= latest_ns


def encode_response(content_type: str, rejected: int = 0, message: str = "") -> bytes:
    """An ``ExportMetricsServiceResponse``; ``partial_success`` only when points were
    rejected."""
    if content_type == otlp.PROTOBUF:
        response = ExportMetricsServiceResponse()
        if rejected or message:
            response.partial_success.rejected_data_points = rejected
            response.partial_success.error_message = message
        return response.SerializeToString()
    body: dict[str, Any] = {}
    if rejected or message:
        body["partialSuccess"] = {"rejectedDataPoints": str(rejected), "errorMessage": message}
    return json.dumps(body).encode()


def summarize(points: Iterable[MetricPoint]) -> Counter[str]:
    """Data points per kind (metrics and logs)."""
    return Counter(p.kind for p in points)
