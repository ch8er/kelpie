"""OTLP/HTTP trace ingest: ``POST /v1/traces``, independent of the HTTP server.

:meth:`TraceIngest.handle` takes the request headers and a body reader and returns the
reply. It authenticates, checks the media type, encoding and size, decodes the
``ExportTraceServiceRequest`` (:mod:`kelpie.traces.otlp`), maps spans onto
:class:`~kelpie.core.model.Span` (:mod:`kelpie.traces.semconv`) and appends them to the
span store. It runs on the HTTP server's request thread and never touches the control
loop: the span store has its own lock and writes in short batches.

Status codes follow the OTLP/HTTP specification: 200 with an ``ExportTraceServiceResponse``
in the request's encoding (``partial_success`` when spans were rejected), 400 for a body
that cannot be decoded, 401 for a missing or wrong bearer token, 411/413 for a missing or
too large ``Content-Length``, 415 for an unsupported media type or content encoding, 503
when the store cannot be written (exporters retry). Error bodies are ``google.rpc.Status``.
"""

from __future__ import annotations

import hmac
import logging
import zlib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from kelpie.core.model import Span
from kelpie.core.spans import SpanStore
from kelpie.traces import otlp
from kelpie.traces.semconv import MappingOptions, map_spans

log = logging.getLogger(__name__)

PATH = "/v1/traces"
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
IGNORED = "ignored"  # kind label for decoded spans that are not GenAI steps


class IngestObserver(Protocol):
    """Where ingest reports what it did; :class:`kelpie.metrics.Metrics` implements it."""

    def traces_request(self, code: int) -> None: ...

    def spans_received(self, kind: str, count: int) -> None: ...

    def spans_rejected(self, reason: str, count: int) -> None: ...

    def span_rows_added(self, count: int) -> None: ...


class Headers(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: bytes
    content_type: str
    headers: Mapping[str, str] = field(default_factory=dict)
    close: bool = False  # the body may not have been read: close the connection


@dataclass(slots=True)
class TraceIngest:
    store: SpanStore
    clock: Callable[[], datetime]
    observer: IngestObserver | None = None
    token: str | None = None  # bearer token; None accepts unauthenticated requests
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    options: MappingOptions = field(default_factory=MappingOptions)

    def handle(self, headers: Headers, read: Callable[[int], bytes]) -> Reply:
        reply = self._handle(headers, read)
        if self.observer is not None:
            self.observer.traces_request(reply.status)
        return reply

    def _handle(self, headers: Headers, read: Callable[[int], bytes]) -> Reply:
        media = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # Errors before the media type is known are answered in JSON.
        encoding = media if media in (otlp.JSON, otlp.PROTOBUF) else otlp.JSON
        if self.token is not None and not _authorized(headers.get("Authorization"), self.token):
            return _error(
                401, encoding, otlp.UNAUTHENTICATED, "missing or invalid bearer token", close=True
            )
        if media not in (otlp.JSON, otlp.PROTOBUF):
            message = f"unsupported content type {media!r}; use {otlp.PROTOBUF} or {otlp.JSON}"
            return _error(415, encoding, otlp.INVALID_ARGUMENT, message, close=True)
        content_encoding = (headers.get("Content-Encoding") or "identity").strip().lower()
        if content_encoding not in ("identity", "gzip"):
            message = f"unsupported content encoding {content_encoding!r}; use gzip or none"
            return _error(415, encoding, otlp.INVALID_ARGUMENT, message, close=True)
        raw_length = headers.get("Content-Length")
        if raw_length is None:
            return _error(411, encoding, otlp.INVALID_ARGUMENT, "Content-Length required", True)
        try:
            length = int(raw_length)
            if length < 0:
                raise ValueError(raw_length)
        except ValueError:
            return _error(400, encoding, otlp.INVALID_ARGUMENT, "bad Content-Length", True)
        if length > self.max_body_bytes:
            message = f"body over {self.max_body_bytes} bytes"
            return _error(413, encoding, otlp.RESOURCE_EXHAUSTED, message, close=True)
        body = read(length)
        if content_encoding == "gzip":
            try:
                body = _gunzip(body, self.max_body_bytes)
            except _TooLarge:
                message = f"decompressed body over {self.max_body_bytes} bytes"
                return _error(413, encoding, otlp.RESOURCE_EXHAUSTED, message)
            except (zlib.error, ValueError) as exc:
                return _error(400, encoding, otlp.INVALID_ARGUMENT, f"bad gzip body: {exc}")
        try:
            decoded = otlp.decode(body, media)
        except otlp.DecodeError as exc:
            return _error(400, encoding, otlp.INVALID_ARGUMENT, str(exc))
        mapped = map_spans(decoded.spans, self.options)
        try:
            added = self.store.append_spans(mapped.spans, self.clock())
            if mapped.spans:
                self.store.inherit_links({s.trace_id for s in mapped.spans})
        except Exception as exc:  # the store is down: let the exporter retry
            log.error("cannot store %d spans: %s", len(mapped.spans), exc)
            return _error(503, encoding, otlp.UNAVAILABLE, "span store unavailable")
        self._observe(mapped.spans, mapped.ignored, decoded.rejected, added)
        rejected = sum(decoded.rejected.values())
        message = (
            "; ".join(f"{n} {reason}" for reason, n in sorted(decoded.rejected.items()))
            if rejected
            else ""
        )
        return Reply(200, otlp.encode_response(media, rejected, message), media)

    def _observe(
        self, spans: Sequence[Span], ignored: int, rejected: Mapping[str, int], added: int
    ) -> None:
        if self.observer is None:
            return
        kinds = Counter(str(span.kind) for span in spans)
        if ignored:
            kinds[IGNORED] = ignored
        for kind, n in sorted(kinds.items()):
            self.observer.spans_received(kind, n)
        for reason, n in sorted(rejected.items()):
            self.observer.spans_rejected(reason, n)
        self.observer.span_rows_added(added)


def _authorized(header: str | None, token: str) -> bool:
    expected = f"Bearer {token}".encode()
    return hmac.compare_digest((header or "").strip().encode(), expected)


class _TooLarge(ValueError):
    pass


def _gunzip(body: bytes, limit: int) -> bytes:
    """Decompress a gzip body without ever holding more than ``limit`` bytes."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = inflater.decompress(body, limit + 1)
    if len(out) > limit:
        raise _TooLarge
    if not inflater.eof:
        raise ValueError("truncated gzip stream")
    return out


def _error(status: int, encoding: str, code: int, message: str, close: bool = False) -> Reply:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return Reply(status, otlp.encode_status(encoding, code, message), encoding, headers, close)
