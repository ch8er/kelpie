"""OTLP/HTTP trace ingest: ``POST /v1/traces``, independent of the HTTP server.

:meth:`TraceIngest.handle` takes the request headers and a body reader and returns the
reply. Everything that can be decided from the headers is checked before a byte of the
body is read: length (411/413), bearer token (401), media type and content encoding (415)
and a free ingest slot (503; at most ``max_concurrent`` requests are decoded at once, so
memory stays bounded). Then it decodes the ``ExportTraceServiceRequest``
(:mod:`kelpie.traces.otlp`), drops spans with implausible times, maps spans onto
:class:`~kelpie.core.model.Span` (:mod:`kelpie.traces.semconv`) and appends them to the
span store. It runs on the HTTP server's request thread and never touches the control
loop: the span store has its own lock and writes in short batches.

Status codes follow the OTLP/HTTP specification: 200 with an ``ExportTraceServiceResponse``
in the request's encoding (``partial_success`` when spans were rejected), 400 for a body
that cannot be decoded, 401 for a missing or wrong bearer token, 411/413 for a missing or
too large ``Content-Length``, 415 for an unsupported media type or content encoding, 503
when all ingest slots are busy or the store cannot be written (exporters retry). Error
bodies are ``google.rpc.Status``. Replies sent before the body was read close the
connection.
"""

from __future__ import annotations

import hmac
import logging
import threading
import zlib
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Protocol

from kelpie.core.model import Span
from kelpie.core.spans import SpanStore
from kelpie.traces import otlp
from kelpie.traces.auth import TokenCheck, TokenFile
from kelpie.traces.semconv import MappingOptions, map_spans

log = logging.getLogger(__name__)

PATH = "/v1/traces"
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_AGE = timedelta(days=7)
MAX_FUTURE = timedelta(hours=1)  # clock skew tolerated for span times
IGNORED = "ignored"  # kind label for decoded spans that are not GenAI steps
INVALID_TIME = "invalid_time"
_EPOCH_NS_PER_US = 1000


class IngestObserver(Protocol):
    """Where ingest reports what it did; :class:`kelpie.metrics.Metrics` implements it."""

    def traces_request(self, code: int) -> None: ...

    def spans_received(self, kind: str, count: int) -> None: ...

    def spans_rejected(self, reason: str, count: int) -> None: ...

    def traces_grace_request(self) -> None:
        """A request authenticated by a rotated-out token within its grace period."""
        ...


class Headers(Protocol):
    def get(self, name: str, default: str | None = None) -> str | None: ...


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: bytes
    content_type: str
    headers: Mapping[str, str] = field(default_factory=dict)
    close: bool = False  # the body was not read: close the connection


@dataclass(slots=True)
class TraceIngest:
    store: SpanStore
    clock: Callable[[], datetime]
    observer: IngestObserver | None = None
    token: str | None = field(default=None, repr=False)  # bearer token; None: no auth
    tokens: TokenFile | None = None  # [traces] token_file: replaces ``token`` when set
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    options: MappingOptions = field(default_factory=MappingOptions)
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    max_age: timedelta = DEFAULT_MAX_AGE  # older spans would be pruned at once: rejected
    _slots: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._slots = threading.BoundedSemaphore(max(1, self.max_concurrent))

    # Called with every accepted request as received (body, Content-Type, Content-Encoding),
    # e.g. :meth:`kelpie.traces.forward.Forwarder.submit`. Must not block.
    on_accepted: Callable[[bytes, str, str | None], None] | None = None

    def handle(
        self,
        headers: Headers,
        read: Callable[[int], bytes],
        *,
        authenticated: bool = False,
        rewrite: Callable[[Span], Span] | None = None,
    ) -> Reply:
        """Ingest one export. ``authenticated``: the caller already checked who sent it (the
        multi-host hub, by agent token), so ``token`` is not asked for. ``rewrite`` maps
        every span before it is stored (the hub puts it in the agent's namespace)."""
        reply = self._handle(headers, read, authenticated, rewrite)
        if self.observer is not None:
            self.observer.traces_request(reply.status)
        return reply

    def _authorized(self, header: str | None) -> bool:
        if self.tokens is None:
            return self.token is None or _authorized(header, self.token)
        credentials = _credentials(header)
        verdict = TokenCheck.INVALID if credentials is None else self.tokens.check(credentials)
        if verdict is TokenCheck.GRACE and self.observer is not None:
            grace = getattr(self.observer, "traces_grace_request", None)
            if callable(grace):
                grace()
        return verdict is not TokenCheck.INVALID

    def _handle(
        self,
        headers: Headers,
        read: Callable[[int], bytes],
        authenticated: bool,
        rewrite: Callable[[Span], Span] | None,
    ) -> Reply:
        media = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # Errors before the media type is known are answered in JSON.
        encoding = media if media in (otlp.JSON, otlp.PROTOBUF) else otlp.JSON
        raw_length = headers.get("Content-Length")
        if raw_length is None:
            return _error(411, encoding, otlp.INVALID_ARGUMENT, "Content-Length required")
        try:
            length = int(raw_length)
            if length < 0:
                raise ValueError(raw_length)
        except ValueError:
            return _error(400, encoding, otlp.INVALID_ARGUMENT, "bad Content-Length")
        if length > self.max_body_bytes:
            message = f"body over {self.max_body_bytes} bytes"
            return _error(413, encoding, otlp.RESOURCE_EXHAUSTED, message)
        if not authenticated and not self._authorized(headers.get("Authorization")):
            return _error(401, encoding, otlp.UNAUTHENTICATED, "missing or invalid bearer token")
        if media not in (otlp.JSON, otlp.PROTOBUF):
            message = f"unsupported content type {media!r}; use {otlp.PROTOBUF} or {otlp.JSON}"
            return _error(415, encoding, otlp.INVALID_ARGUMENT, message)
        content_encoding = (headers.get("Content-Encoding") or "identity").strip().lower()
        if content_encoding not in ("identity", "gzip"):
            message = f"unsupported content encoding {content_encoding!r}; use gzip or none"
            return _error(415, encoding, otlp.INVALID_ARGUMENT, message)
        if not self._slots.acquire(blocking=False):
            reply = _error(503, encoding, otlp.UNAVAILABLE, "ingest busy, retry later")
            return Reply(reply.status, reply.body, encoding, {"Retry-After": "1"}, close=True)
        try:
            original_type = headers.get("Content-Type") or media
            body = read(length)
            return self._ingest(body, media, original_type, content_encoding, encoding, rewrite)
        finally:
            self._slots.release()

    def _ingest(
        self,
        body: bytes,
        media: str,
        original_type: str,
        content_encoding: str,
        encoding: str,
        rewrite: Callable[[Span], Span] | None = None,
    ) -> Reply:
        received = body  # forwarded as received, still compressed
        if content_encoding == "gzip":
            try:
                body = _gunzip(body, self.max_body_bytes)
            except _TooLarge:
                message = f"decompressed body over {self.max_body_bytes} bytes"
                return _error(413, encoding, otlp.RESOURCE_EXHAUSTED, message, close=False)
            except (zlib.error, ValueError) as exc:
                message = f"bad gzip body: {exc}"
                return _error(400, encoding, otlp.INVALID_ARGUMENT, message, close=False)
        try:
            decoded = otlp.decode(body, media)
        except otlp.DecodeError as exc:
            return _error(400, encoding, otlp.INVALID_ARGUMENT, str(exc), close=False)
        now = self.clock()
        rejected = Counter(decoded.rejected)
        raw = [s for s in decoded.spans if _plausible(s, now, self.max_age)]
        if len(raw) < len(decoded.spans):
            rejected[INVALID_TIME] += len(decoded.spans) - len(raw)
        mapped = map_spans(raw, self.options)
        rejected.update(mapped.rejected)
        if rewrite is not None:
            mapped = replace(mapped, spans=[rewrite(s) for s in mapped.spans])
        try:
            self.store.append_spans(mapped.spans, now)
            if mapped.spans:
                self.store.inherit_links({s.trace_id for s in mapped.spans})
        except Exception as exc:  # the store is down: let the exporter retry
            log.error("cannot store %d spans: %s", len(mapped.spans), exc)
            return _error(503, encoding, otlp.UNAVAILABLE, "span store unavailable", close=False)
        self._observe(mapped.spans, mapped.ignored, rejected)
        total = sum(rejected.values())
        message = "; ".join(f"{n} {reason}" for reason, n in sorted(rejected.items()))
        if self.on_accepted is not None:
            try:
                gzip = content_encoding if content_encoding != "identity" else None
                self.on_accepted(received, original_type, gzip)
            except Exception:
                log.exception("accepted-request hook failed")
        return Reply(200, otlp.encode_response(media, total, message), media)

    def _observe(self, spans: Sequence[Span], ignored: int, rejected: Mapping[str, int]) -> None:
        if self.observer is None:
            return
        kinds = Counter(str(span.kind) for span in spans)
        if ignored:
            kinds[IGNORED] = ignored
        for kind, n in sorted(kinds.items()):
            self.observer.spans_received(kind, n)
        for reason, n in sorted(rejected.items()):
            self.observer.spans_rejected(reason, n)


def _ns(at: datetime) -> int:
    whole = int(at.timestamp())
    return whole * 1_000_000_000 + at.microsecond * _EPOCH_NS_PER_US


def _plausible(span: otlp.RawSpan, now: datetime, max_age: timedelta) -> bool:
    """Span times within [now - max_age, now + MAX_FUTURE], compared as integers so a
    hostile ``1e27`` never reaches datetime arithmetic."""
    latest = _ns(now + MAX_FUTURE)
    oldest = _ns(now - max_age)
    last = max(span.start_ns, span.end_ns)
    return span.start_ns <= latest and span.end_ns <= latest and last >= oldest


def _credentials(header: str | None) -> str | None:
    scheme, _, credentials = (header or "").strip().partition(" ")
    return credentials.strip() if scheme.lower() == "bearer" else None


def _authorized(header: str | None, token: str) -> bool:
    credentials = _credentials(header)
    return credentials is not None and hmac.compare_digest(credentials.encode(), token.encode())


class _TooLarge(ValueError):
    pass


def _gunzip(body: bytes, limit: int) -> bytes:
    """Decompress a gzip body (all members) without ever holding more than ``limit``
    bytes. Data after the last member that is not gzip is an error."""
    out = bytearray()
    data = body
    while True:
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out += inflater.decompress(data, limit + 1 - len(out))
        if len(out) > limit:
            raise _TooLarge
        if not inflater.eof:
            raise ValueError("truncated gzip stream")
        data = inflater.unused_data
        if not data:
            return bytes(out)


def _error(status: int, encoding: str, code: int, message: str, close: bool = True) -> Reply:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return Reply(status, otlp.encode_status(encoding, code, message), encoding, headers, close)
