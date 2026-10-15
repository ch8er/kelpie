"""OTLP/HTTP decoding: protobuf and OTLP/JSON give the same spans; bad input never raises
anything but DecodeError."""

from __future__ import annotations

import json
from typing import Any

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceResponse

from kelpie.traces import otlp
from kelpie.traces.otlp import JSON, PROTOBUF, DecodeError, decode
from tests.traces.otlp_builders import TRACE, S, as_json, ns, request, sid

ATTRS: dict[str, Any] = {
    "gen_ai.operation.name": "chat",
    "gen_ai.usage.input_tokens": 1200,
    "ratio": 0.5,
    "flag": True,
    "list": ["a", 1],
    "map": {"inner": "x"},
    "raw": b"\x01\x02",
}


def spans() -> list[S]:
    return [
        S(sid(1), attrs=ATTRS, error=True),
        S(sid(2), name="execute_tool shell", parent=sid(1)),
    ]


@pytest.mark.parametrize("encoding", [PROTOBUF, JSON])
def test_both_encodings_decode_alike(encoding: str) -> None:
    body = request(spans()) if encoding == PROTOBUF else as_json(spans())
    decoded = decode(body, encoding)
    assert not decoded.rejected
    first, second = decoded.spans
    assert first.trace_id == TRACE and first.span_id == sid(1) and first.parent_id is None
    assert first.error and not second.error
    assert second.parent_id == sid(1)
    assert first.start_ns == ns(S(sid(1)).start)
    assert first.attributes == {
        "gen_ai.operation.name": "chat",
        "gen_ai.usage.input_tokens": 1200,
        "ratio": 0.5,
        "flag": True,
        "list": ["a", 1],
        "map": {"inner": "x"},
        "raw": "0102",
    }
    assert first.resource == {"service.name": "synthetic-agent"}


def otlp_json(span: dict[str, Any], resource: dict[str, Any] | None = None) -> bytes:
    rs: dict[str, Any] = {"scopeSpans": [{"spans": [span]}]}
    if resource is not None:
        rs["resource"] = resource
    return json.dumps({"resourceSpans": [rs]}).encode()


GOOD = {
    "traceId": TRACE.upper(),
    "spanId": sid(7),
    "name": "chat",
    "startTimeUnixNano": "1772452800000000000",
    "endTimeUnixNano": 1772452801000000000,
}


def test_json_accepts_snake_case_string_enums_and_upper_hex() -> None:
    span = {
        "trace_id": TRACE.upper(),
        "span_id": sid(7),
        "parent_span_id": "",
        "name": "chat",
        "start_time_unix_nano": "1772452800000000000",
        "status": {"code": "STATUS_CODE_ERROR"},
        "attributes": [
            {"key": "n", "value": {"int_value": "42"}},
            {"key": "b", "value": {"boolValue": "true"}},
            {"key": "d", "value": {"doubleValue": "1.5"}},
            {"key": "nested", "value": {"arrayValue": {"values": [{"kvlistValue": {}}]}}},
            {"key": "empty", "value": {}},
            {"key": "", "value": {"stringValue": "no key"}},
            "not a kv",
        ],
    }
    (s,) = decode(otlp_json(span), JSON).spans
    assert s.trace_id == TRACE and s.error and s.end_ns == 0 and s.parent_id is None
    assert s.attributes == {"n": 42, "b": True, "d": 1.5, "nested": [{}]}


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"traceId": "zz" * 16}, "invalid_id"),
        ({"traceId": "00" * 16}, "invalid_id"),
        ({"spanId": "abc"}, "invalid_id"),
        ({"spanId": None}, "invalid_id"),
        ({"startTimeUnixNano": "0"}, "invalid_time"),
        ({"startTimeUnixNano": "soon"}, "malformed"),
        ({"startTimeUnixNano": 1.5}, "malformed"),
        ({"startTimeUnixNano": True}, "malformed"),
        ({"traceId": 12}, "malformed"),
    ],
)
def test_bad_spans_are_rejected_with_a_reason(change: dict[str, Any], reason: str) -> None:
    decoded = decode(otlp_json({**GOOD, **change}), JSON)
    assert decoded.spans == [] and dict(decoded.rejected) == {reason: 1}


def test_non_object_span_is_malformed() -> None:
    body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [1]}]}]}).encode()
    assert dict(decode(body, JSON).rejected) == {"malformed": 1}


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'"x"', b'{"resourceSpans": 5}', b"\xff\xfe"],
)
def test_bad_json_payloads_raise_decode_error(body: bytes) -> None:
    with pytest.raises(DecodeError):
        decode(body, JSON)


def test_empty_object_is_an_empty_request() -> None:
    assert decode(b"{}", JSON).spans == []
    assert decode(b"", PROTOBUF).spans == []


def test_bad_protobuf_raises_decode_error() -> None:
    with pytest.raises(DecodeError):
        decode(b"\x0a\xff\xff\xff\xff\x0f garbage", PROTOBUF)


def test_unknown_content_type() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        decode(b"{}", "text/plain")


def test_json_resource_attributes_and_bad_bytes() -> None:
    resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "svc"}}]}
    span = {**GOOD, "attributes": [{"key": "raw", "value": {"bytesValue": "!!"}}]}
    (s,) = decode(otlp_json(span, resource), JSON).spans
    assert s.resource == {"service.name": "svc"} and s.attributes == {}


def test_responses() -> None:
    assert otlp.encode_response(JSON) == b"{}"
    partial = json.loads(otlp.encode_response(JSON, 2, "2 invalid_id"))
    assert partial == {"partialSuccess": {"rejectedSpans": "2", "errorMessage": "2 invalid_id"}}
    assert otlp.encode_response(PROTOBUF) == b""
    msg = ExportTraceServiceResponse.FromString(otlp.encode_response(PROTOBUF, 3, "bad"))
    assert msg.partial_success.rejected_spans == 3 and msg.partial_success.error_message == "bad"


def test_status_bodies() -> None:
    assert json.loads(otlp.encode_status(JSON, 3, "nope")) == {"code": 3, "message": "nope"}
    long = "x" * 300  # length needs a two-byte varint
    body = otlp.encode_status(PROTOBUF, 16, long)
    assert body[:2] == b"\x08\x10" and body[2] == 0x12 and body[3:5] == b"\xac\x02"
    assert body[5:] == long.encode()
