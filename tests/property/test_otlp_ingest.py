"""OTLP ingest: a sender controls the whole body. Ingest answers every export with a
status (never an exception), stores only spans dated within its window, and drops
Kelpie's reserved attributes whatever the sender put there."""

from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timedelta
from typing import Any

from hypothesis import example, given
from hypothesis import strategies as st

from kelpie.core.spans import INGEST_MARK, UNVERIFIED_ATTRIBUTE, MemorySpanStore
from kelpie.traces.ingest import MAX_FUTURE, TraceIngest
from kelpie.traces.otlp import JSON as OTLP_JSON
from kelpie.traces.otlp import PROTOBUF, DecodeError, decode
from tests.property.strategies import JSON
from tests.traces.otlp_builders import NOW, S, as_json, request, sid
from tests.traces.test_ingest import Headers

KEYS = st.sampled_from(
    [
        "gen_ai.operation.name",
        "gen_ai.tool.name",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.id",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "gen_ai.request.model",
        "gen_ai.system",
        "session.id",
        "kelpie.session_id",
        "kelpie.task_id",
        "tool_parameters",
        "tool_name",
        INGEST_MARK,
        UNVERIFIED_ATTRIBUTE,
    ]
) | st.text(max_size=10)
OPERATIONS = st.sampled_from(["chat", "execute_tool", "invoke_agent", "text_completion", ""])
# what the builders can encode: no None, no non-finite floats in protobuf ints
VALUES = st.recursive(
    st.booleans()
    | st.integers(min_value=-(2**63), max_value=2**63 - 1)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=30)
    | OPERATIONS
    | st.just("[" * 12_000),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=3)
    ),
    max_leaves=8,
)
SPANS = st.builds(
    S,
    span_id=st.integers(min_value=1, max_value=2**63).map(sid),
    name=st.text(max_size=30),
    attrs=st.dictionaries(KEYS, VALUES, max_size=8),
    parent=st.none() | st.integers(min_value=1, max_value=50).map(sid),
    start=st.datetimes(
        min_value=datetime(1970, 1, 2),
        max_value=datetime(2200, 1, 1),
        timezones=st.just(NOW.tzinfo),
    )
    | st.just(NOW - timedelta(minutes=1)),
    duration=st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=3)),
    error=st.booleans(),
)
RESOURCE = st.none() | st.dictionaries(KEYS, VALUES, max_size=4)


def _post(body: bytes, content_type: str) -> tuple[int, MemorySpanStore]:
    store = MemorySpanStore()
    target = TraceIngest(store=store, clock=lambda: NOW)
    headers = Headers({"Content-Type": content_type, "Content-Length": str(len(body))})
    reply = target.handle(headers, io.BytesIO(body).read)
    return reply.status, store


def _check_stored(store: MemorySpanStore) -> None:
    for span in store.spans_since(datetime.min.replace(tzinfo=NOW.tzinfo)):
        assert span.start <= NOW + MAX_FUTURE
        assert INGEST_MARK not in span.attributes
        assert UNVERIFIED_ATTRIBUTE not in span.attributes


@given(st.lists(SPANS, max_size=6), RESOURCE, st.sampled_from([PROTOBUF, OTLP_JSON]))
def test_any_export_gets_a_status(spans: list[S], resource: Any, encoding: str) -> None:
    body = request(spans, resource) if encoding == PROTOBUF else as_json(spans, resource)
    status, store = _post(body, encoding)
    assert status == 200
    _check_stored(store)


@given(st.binary(max_size=2000), st.sampled_from([PROTOBUF, OTLP_JSON, "text/plain", ""]))
@example(b"[" * 15_000, OTLP_JSON)
@example(
    b'{"resourceSpans": [{"scopeSpans": [{"spans": [{"startTimeUnixNano": "1e400"}]}]}]}', OTLP_JSON
)
def test_any_bytes_get_a_status(body: bytes, content_type: str) -> None:
    status, store = _post(body, content_type)
    assert status in (200, 400, 413, 415)
    _check_stored(store)


@given(JSON)
def test_any_json_value_decodes_or_is_refused(value: Any) -> None:
    body = json.dumps({"resourceSpans": value}, allow_nan=True).encode()
    with contextlib.suppress(DecodeError):
        decode(body, OTLP_JSON)
