"""Decode OTLP/HTTP ``ExportTraceServiceRequest`` bodies and encode the responses.

Both encodings of the OTLP specification are accepted:

- ``application/x-protobuf``: the binary protobuf message, parsed with the generated
  classes of ``opentelemetry-proto``;
- ``application/json``: OTLP/JSON, the proto3 JSON mapping with the OTLP exceptions: trace
  and span ids are hex strings (not base64), enums are integers, 64-bit integers may be
  strings. Field names are lowerCamelCase; the original snake_case names are accepted too.

Both decoders produce the same flat :class:`RawSpan` list. A span that cannot be used
(missing or malformed ids, no start time) is counted as rejected with a reason; a payload
that is not an ``ExportTraceServiceRequest`` at all raises :class:`DecodeError`.

Span events and links are ignored: GenAI instrumentations put message content in events,
and Kelpie does not keep content (see :mod:`kelpie.traces.semconv`).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from google.protobuf.message import DecodeError as ProtobufDecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue

JSON = "application/json"
PROTOBUF = "application/x-protobuf"
STATUS_ERROR = 2  # opentelemetry.proto.trace.v1.Status.StatusCode.STATUS_CODE_ERROR
_MAX_DEPTH = 8  # nested AnyValue arrays/kvlists deeper than this are dropped


class DecodeError(ValueError):
    """The body is not an ``ExportTraceServiceRequest`` in the declared encoding."""


@dataclass(frozen=True, slots=True)
class RawSpan:
    trace_id: str  # 32 lowercase hex digits
    span_id: str  # 16 lowercase hex digits
    parent_id: str | None
    name: str
    start_ns: int
    end_ns: int
    error: bool  # status code ERROR
    attributes: dict[str, Any]
    resource: dict[str, Any]


@dataclass(slots=True)
class Decoded:
    spans: list[RawSpan] = field(default_factory=list)
    rejected: Counter[str] = field(default_factory=Counter)  # reason -> spans


def decode(body: bytes, content_type: str) -> Decoded:
    """Decode a request body; ``content_type`` is :data:`JSON` or :data:`PROTOBUF`."""
    if content_type == PROTOBUF:
        return _decode_protobuf(body)
    if content_type == JSON:
        return _decode_json(body)
    raise ValueError(f"unsupported content type {content_type!r}")


# --- protobuf ---------------------------------------------------------------------------


def _decode_protobuf(body: bytes) -> Decoded:
    request = ExportTraceServiceRequest()
    try:
        request.ParseFromString(body)
    except (ProtobufDecodeError, RuntimeError, ValueError, RecursionError) as exc:
        raise DecodeError(f"invalid protobuf ExportTraceServiceRequest: {exc}") from exc
    out = Decoded()
    for rs in request.resource_spans:
        resource = _pb_attributes(rs.resource.attributes)
        for ss in rs.scope_spans:
            for s in ss.spans:
                _add(
                    out,
                    trace_id=s.trace_id.hex(),
                    span_id=s.span_id.hex(),
                    parent_id=s.parent_span_id.hex(),
                    name=s.name,
                    start_ns=s.start_time_unix_nano,
                    end_ns=s.end_time_unix_nano,
                    error=s.status.code == STATUS_ERROR,
                    attributes=_pb_attributes(s.attributes),
                    resource=resource,
                )
    return out


def _pb_attributes(kvs: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kv in kvs:
        value = _pb_value(kv.value, 0)
        if kv.key and value is not None:
            out[kv.key] = value
    return out


def _pb_value(v: AnyValue, depth: int) -> Any:
    match v.WhichOneof("value"):
        case "string_value":
            return v.string_value
        case "bool_value":
            return v.bool_value
        case "int_value":
            return v.int_value
        case "double_value":
            return v.double_value
        case "bytes_value":
            return v.bytes_value.hex()
        case "array_value" if depth < _MAX_DEPTH:
            items = (_pb_value(x, depth + 1) for x in v.array_value.values)
            return [x for x in items if x is not None]
        case "kvlist_value" if depth < _MAX_DEPTH:
            pairs = ((kv.key, _pb_value(kv.value, depth + 1)) for kv in v.kvlist_value.values)
            return {k: x for k, x in pairs if k and x is not None}
        case _:
            return None


# --- JSON -------------------------------------------------------------------------------


def _decode_json(body: bytes) -> Decoded:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise DecodeError(f"invalid JSON: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise DecodeError("ExportTraceServiceRequest must be a JSON object")
    out = Decoded()
    for rs in _list(payload, "resourceSpans", "resource_spans"):
        resource_obj = _get(rs, "resource")
        resource = _json_attributes(resource_obj) if isinstance(resource_obj, dict) else {}
        for ss in _list(rs, "scopeSpans", "scope_spans"):
            for s in _list(ss, "spans"):
                if not isinstance(s, dict):
                    out.rejected["malformed"] += 1
                    continue
                try:
                    status = _get(s, "status")
                    code = _get(status, "code") if isinstance(status, dict) else None
                    _add(
                        out,
                        trace_id=_json_id(_get(s, "traceId", "trace_id")),
                        span_id=_json_id(_get(s, "spanId", "span_id")),
                        parent_id=_json_id(_get(s, "parentSpanId", "parent_span_id")),
                        name=str(_get(s, "name") or ""),
                        start_ns=_json_int(_get(s, "startTimeUnixNano", "start_time_unix_nano")),
                        end_ns=_json_int(_get(s, "endTimeUnixNano", "end_time_unix_nano")),
                        error=code in (STATUS_ERROR, "STATUS_CODE_ERROR", str(STATUS_ERROR)),
                        attributes=_json_attributes(s),
                        resource=resource,
                    )
                except (TypeError, ValueError, OverflowError):
                    out.rejected["malformed"] += 1
    return out


def _get(obj: Any, *names: str) -> Any:
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name in obj:
            return obj[name]
    return None


def _list(obj: Any, *names: str) -> list[Any]:
    value = _get(obj, *names)
    if value is None:
        return []
    if not isinstance(value, list):
        raise DecodeError(f"{names[0]} must be a list")
    return value


def _json_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError("ids must be hex strings")
    return value.lower()


def _json_int(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, bool):
        raise ValueError("expected an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("expected an integer")
        return int(value)
    return int(value)  # int, or the decimal string OTLP/JSON uses for 64-bit integers


def _json_attributes(obj: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    raw = obj.get("attributes")
    if not isinstance(raw, list):
        return out
    for kv in raw:
        if not isinstance(kv, dict) or not isinstance(kv.get("key"), str) or not kv["key"]:
            continue
        value = _json_value(kv.get("value"), 0)
        if value is not None:
            out[kv["key"]] = value
    return out


def _json_value(v: Any, depth: int) -> Any:
    if not isinstance(v, dict):
        return None
    try:
        if (x := _get(v, "stringValue", "string_value")) is not None:
            return str(x)
        if (x := _get(v, "boolValue", "bool_value")) is not None:
            return bool(x) if isinstance(x, bool) else str(x).lower() == "true"
        if (x := _get(v, "intValue", "int_value")) is not None:
            return _json_int(x)
        if (x := _get(v, "doubleValue", "double_value")) is not None:
            return float(x)
        if (x := _get(v, "bytesValue", "bytes_value")) is not None:
            return base64.b64decode(str(x), validate=True).hex()
        if depth >= _MAX_DEPTH:
            return None
        if isinstance(x := _get(v, "arrayValue", "array_value"), dict):
            items = (_json_value(i, depth + 1) for i in x.get("values") or [])
            return [i for i in items if i is not None]
        if isinstance(x := _get(v, "kvlistValue", "kvlist_value"), dict):
            out: dict[str, Any] = {}
            for kv in x.get("values") or []:
                if isinstance(kv, dict) and isinstance(kv.get("key"), str):
                    value = _json_value(kv.get("value"), depth + 1)
                    if value is not None:
                        out[kv["key"]] = value
            return out
    except (TypeError, ValueError, OverflowError, binascii.Error):
        return None
    return None


# --- shared -----------------------------------------------------------------------------


_HEX = re.compile(r"[0-9a-f]+")


def _is_hex(value: str, length: int) -> bool:
    """Exactly ``length`` lowercase hex digits, not all zero (an invalid id in OTLP)."""
    return len(value) == length and _HEX.fullmatch(value) is not None and value.strip("0") != ""


def _add(
    out: Decoded,
    *,
    trace_id: str,
    span_id: str,
    parent_id: str,
    name: str,
    start_ns: int,
    end_ns: int,
    error: bool,
    attributes: dict[str, Any],
    resource: dict[str, Any],
) -> None:
    if not _is_hex(trace_id, 32) or not _is_hex(span_id, 16):
        out.rejected["invalid_id"] += 1
        return
    if start_ns <= 0 or end_ns < 0:
        out.rejected["invalid_time"] += 1
        return
    out.spans.append(
        RawSpan(
            trace_id=trace_id,
            span_id=span_id,
            parent_id=parent_id if _is_hex(parent_id, 16) else None,
            name=name,
            start_ns=start_ns,
            end_ns=end_ns,
            error=error,
            attributes=attributes,
            resource=resource,
        )
    )


def encode_response(content_type: str, rejected: int = 0, message: str = "") -> bytes:
    """An ``ExportTraceServiceResponse``; ``partial_success`` only when spans were rejected."""
    if content_type == PROTOBUF:
        response = ExportTraceServiceResponse()
        if rejected or message:
            response.partial_success.rejected_spans = rejected
            response.partial_success.error_message = message
        return response.SerializeToString()
    body: dict[str, Any] = {}
    if rejected or message:
        body["partialSuccess"] = {"rejectedSpans": str(rejected), "errorMessage": message}
    return json.dumps(body).encode()


# google.rpc.Code values used in error responses.
INVALID_ARGUMENT = 3
UNAUTHENTICATED = 16
RESOURCE_EXHAUSTED = 8
UNAVAILABLE = 14


def encode_status(content_type: str, code: int, message: str) -> bytes:
    """A ``google.rpc.Status`` error body, as the OTLP/HTTP spec asks for failures.

    Encoded by hand (fields 1 ``code`` and 2 ``message``) to avoid a dependency on the
    googleapis common protos for two fields.
    """
    if content_type == PROTOBUF:
        text = message.encode()
        return b"\x08" + _varint(code) + b"\x12" + _varint(len(text)) + text
    return json.dumps({"code": code, "message": message}).encode()


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)
