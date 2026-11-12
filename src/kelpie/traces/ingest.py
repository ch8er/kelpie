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
too large ``Content-Length``, 403 for a request with an ``Origin`` header when no token is
needed (a web page, not an exporter: #406), 415 for an unsupported media type or content
encoding, 503 when all ingest slots are busy or the store cannot be written (exporters
retry). Error
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
from kelpie.traces import metrics as otlp_metrics
from kelpie.traces import otlp
from kelpie.traces.auth import TokenCheck, TokenFile
from kelpie.traces.scope import MAX_FUTURE, ScopeGuard, stamp
from kelpie.traces.semconv import MappingOptions, map_spans

log = logging.getLogger(__name__)

PATH = "/v1/traces"
DEFAULT_MAX_BODY_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_MAX_AGE = timedelta(days=7)
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


@dataclass(slots=True)
class ScopedToken:
    """A ``[[traces.tokens]]`` token: from its env var (``token``) or its token file
    (``file``, rotated without a restart). Its scope is ``ScopeGuard.scopes[name]``."""

    name: str
    token: str | None = field(default=None, repr=False)
    file: TokenFile | None = None

    def check(self, credentials: str) -> TokenCheck:
        if self.file is not None:
            return self.file.check(credentials)
        if self.token is not None and hmac.compare_digest(
            credentials.encode(), self.token.encode()
        ):
            return TokenCheck.CURRENT
        return TokenCheck.INVALID


_SHARED = ""  # _authorized: the shared token (or no token needed)


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
    # Per-agent tokens ([[traces.tokens]], #302) and the guard holding their scopes; with
    # them, a request needs a token even when the shared one is not set.
    scoped: Sequence[ScopedToken] = ()
    guard: ScopeGuard | None = None
    _slots: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._slots = threading.BoundedSemaphore(max(1, self.max_concurrent))
        if self.scoped and (
            self.guard is None or any(t.name not in self.guard.scopes for t in self.scoped)
        ):
            raise ValueError("every scoped traces token needs its scope in the guard")

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

    def _open(self) -> bool:
        """No token is needed: every request is taken as the shared token's."""
        return self.token is None and self.tokens is None and not self.scoped

    def _authorized(self, header: str | None) -> str | None:
        """Who sent the request: ``_SHARED`` for the shared token (or when no token is
        needed), a scoped token's name, or None (refused). Every token is compared, in
        constant time each."""
        credentials = _credentials(header)
        found: str | None = None
        grace = False
        if credentials is not None:
            for scoped in self.scoped:
                verdict = scoped.check(credentials)
                if verdict is not TokenCheck.INVALID and found is None:
                    found, grace = scoped.name, verdict is TokenCheck.GRACE
        if found is None:
            if self.tokens is None:
                if self.token is None:
                    open_ingest = not self.scoped
                    return _SHARED if open_ingest else None
                ok = credentials is not None and hmac.compare_digest(
                    credentials.encode(), self.token.encode()
                )
                return _SHARED if ok else None
            verdict = TokenCheck.INVALID if credentials is None else self.tokens.check(credentials)
            if verdict is TokenCheck.INVALID:
                return None
            found, grace = _SHARED, verdict is TokenCheck.GRACE
        if grace and self.observer is not None:
            hook = getattr(self.observer, "traces_grace_request", None)
            if callable(hook):
                hook()
        return found

    def handle_metrics(self, headers: Headers, read: Callable[[int], bytes]) -> Reply:
        """Ingest one ``POST /v1/metrics`` export (Claude Code's cost and token metrics,
        :mod:`kelpie.traces.metrics`): the same checks as a trace export, before a byte of
        the body is read. Only the shared token (or none, when none is needed) may send
        metrics: a per-agent token's scope is its own sessions' spans, and a metric point
        names a session no span may have shown yet, so it is refused (403)."""
        reply = self._handle(headers, read, False, None, metrics=True)
        hook = getattr(self.observer, "metrics_request", None)
        if callable(hook):
            hook(reply.status)
        return reply

    def _handle(
        self,
        headers: Headers,
        read: Callable[[int], bytes],
        authenticated: bool,
        rewrite: Callable[[Span], Span] | None,
        *,
        metrics: bool = False,
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
        sender = _SHARED if authenticated else self._authorized(headers.get("Authorization"))
        if sender is None:
            return _error(401, encoding, otlp.UNAUTHENTICATED, "missing or invalid bearer token")
        if metrics and sender:
            message = "metrics need the shared traces token, not a per-agent one"
            return _error(403, encoding, otlp.PERMISSION_DENIED, message)
        if not authenticated and self._open() and headers.get("Origin") is not None:
            # No exporter sends an Origin; a browser does on every cross-site POST. Without
            # a token to ask for, it is the one sign of a web page (#406).
            message = "requests from a web page are refused (set a traces token to allow them)"
            return _error(403, encoding, otlp.PERMISSION_DENIED, message)
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
            if metrics:
                return self._ingest_metrics(body, media, content_encoding, encoding)
            return self._ingest(
                body, media, original_type, content_encoding, encoding, rewrite, sender
            )
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
        sender: str = _SHARED,
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
        if not sender:  # no span of the shared token may claim a scoped token's name
            mapped = replace(mapped, spans=[stamp(s, None) for s in mapped.spans])
            stored = self._store(mapped.spans, now, encoding)
        else:  # a scoped token: only its own sessions, judged and stored as one step
            assert self.guard is not None  # __post_init__
            with self.guard.barrier.shared():  # the guard's tick cannot run in between
                kept, refused = self.guard.ingest_filter(sender, mapped.spans, now)
                rejected.update(refused)
                mapped = replace(mapped, spans=kept)
                stored = self._store(mapped.spans, now, encoding)
        if stored is not None:
            return stored
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

    def _ingest_metrics(
        self, body: bytes, media: str, content_encoding: str, encoding: str
    ) -> Reply:
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
            decoded = otlp_metrics.decode(body, media, self.options.task_attribute)
        except otlp.DecodeError as exc:
            return _error(400, encoding, otlp.INVALID_ARGUMENT, str(exc), close=False)
        now = self.clock()
        oldest, latest = _ns(now - self.max_age), _ns(now + MAX_FUTURE)
        rejected = Counter(decoded.rejected)
        points = [p for p in decoded.points if otlp_metrics.plausible(p, oldest, latest)]
        if len(points) < len(decoded.points):
            rejected[INVALID_TIME] += len(decoded.points) - len(points)
        rows = [
            (p.session_id, p.task_id, p.model, p.kind, p.value, p.time_ns, p.start_ns)
            for p in points
        ]
        append = getattr(self.store, "append_metric_points", None)
        if rows and callable(append):
            try:
                append(rows)
            except Exception as exc:  # the store is down: let the exporter retry
                log.error("cannot store %d metric points: %s", len(rows), exc)
                return _error(503, encoding, otlp.UNAVAILABLE, "store unavailable", close=False)
        hook = getattr(self.observer, "metric_points", None)
        if callable(hook):
            hook(len(points), decoded.ignored, dict(rejected))
        total = sum(rejected.values())
        message = "; ".join(f"{n} {reason}" for reason, n in sorted(rejected.items()))
        return Reply(200, otlp_metrics.encode_response(media, total, message), media)

    def _store(self, spans: Sequence[Span], now: datetime, encoding: str) -> Reply | None:
        """Store ``spans`` and fill their links; the error reply when the store is down."""
        try:
            self.store.append_spans(spans, now)
            if spans:
                self.store.inherit_links({s.trace_id for s in spans})
        except Exception as exc:  # the store is down: let the exporter retry
            log.error("cannot store %d spans: %s", len(spans), exc)
            return _error(503, encoding, otlp.UNAVAILABLE, "span store unavailable", close=False)
        return None

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


MAX_GZIP_MEMBERS = 64  # exporters send one; many tiny members would cost quadratic CPU


def _gunzip(body: bytes, limit: int) -> bytes:
    """Decompress a gzip body (at most :data:`MAX_GZIP_MEMBERS` members) without ever
    holding more than ``limit`` bytes. Data after the last member that is not gzip is an
    error."""
    out = bytearray()
    data = body
    for _ in range(MAX_GZIP_MEMBERS):
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out += inflater.decompress(data, limit + 1 - len(out))
        if len(out) > limit:
            raise _TooLarge
        if not inflater.eof:
            raise ValueError("truncated gzip stream")
        data = inflater.unused_data
        if not data:
            return bytes(out)
    raise ValueError("too many gzip members")


def _error(status: int, encoding: str, code: int, message: str, close: bool = True) -> Reply:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    return Reply(status, otlp.encode_status(encoding, code, message), encoding, headers, close)
