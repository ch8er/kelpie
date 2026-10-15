"""Optional forwarding of accepted OTLP requests to Langfuse, Phoenix, Datadog or any other
OTLP/HTTP endpoint.

Kelpie forwards the original request body, byte for byte, with its ``Content-Type`` and
``Content-Encoding``: the destination sees what the exporter sent, including the message
content Kelpie itself never stores. Only requests Kelpie accepted (HTTP 200) are forwarded.

Forwarding never blocks or slows ingest. Each destination has a queue bounded by count and
by bytes, and one background thread; :meth:`Forwarder.submit` only enqueues, and drops the
request (counted in ``kelpie_traces_forward_dropped_total``) when the queue is full. The
thread sends with a timeout and retries network errors, HTTP 429 (honouring a
``Retry-After`` in seconds) and 5xx with exponential backoff. Redirects are never followed
(they would carry the credentials to another host) and count as failures, like other 4xx
answers and malformed requests. A request that still fails is counted in
``kelpie_traces_forward_failed_total``. Failure messages never include header values.
"""

from __future__ import annotations

import contextlib
import http.client
import logging
import queue
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from kelpie import __version__

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_QUEUE_SIZE = 1000
DEFAULT_QUEUE_BYTES = 64 * 1024 * 1024
DEFAULT_RETRIES = 3
MAX_RETRIES = 10
BACKOFF_BASE = 0.5  # seconds before the first retry; doubles each time
BACKOFF_MAX = 30.0

# RFC 9110 field names are tokens; values may not contain control characters (CR/LF
# would split the request, others are rejected by http.client with the value in the
# message).
_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_BAD_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def valid_header_name(name: str) -> bool:
    return _HEADER_NAME.fullmatch(name) is not None


def valid_header_value(value: str) -> bool:
    return _BAD_VALUE.search(value) is None


class ForwardObserver(Protocol):
    def forwarded(self, destination: str) -> None: ...

    def forward_failed(self, destination: str) -> None: ...

    def forward_dropped(self, destination: str, count: int = 1) -> None: ...


@dataclass(frozen=True, slots=True)
class Destination:
    name: str  # metric label
    endpoint: str
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)  # may hold secrets
    timeout: float = DEFAULT_TIMEOUT
    queue_size: int = DEFAULT_QUEUE_SIZE
    retries: int = DEFAULT_RETRIES
    queue_bytes: int = DEFAULT_QUEUE_BYTES


@dataclass(frozen=True, slots=True)
class Export:
    body: bytes
    content_type: str
    content_encoding: str | None = None


class SendError(Exception):
    def __init__(self, message: str, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


Sender = Callable[[Destination, Export], None]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # urllib then raises HTTPError with the 3xx code


_OPENER = urllib.request.build_opener(_NoRedirect)


def send_http(destination: Destination, export: Export) -> None:
    """POST one export; raises :class:`SendError` on failure (never with header values)."""
    request = urllib.request.Request(destination.endpoint, data=export.body, method="POST")
    request.add_header("User-Agent", f"kelpie/{__version__}")
    for name, value in destination.headers.items():
        request.add_header(name, value)
    request.add_header("Content-Type", export.content_type)
    if export.content_encoding:
        request.add_header("Content-Encoding", export.content_encoding)
    try:
        with _OPENER.open(request, timeout=destination.timeout) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SendError(f"HTTP {exc.code} redirect (not followed)", False) from None
        retryable = exc.code == 429 or exc.code >= 500
        raise SendError(f"HTTP {exc.code}", retryable, _retry_after(exc)) from None
    except urllib.error.URLError as exc:
        raise SendError(f"connection failed ({type(exc.reason).__name__})", True) from None
    except (OSError, TimeoutError) as exc:
        raise SendError(f"connection failed ({type(exc).__name__})", True) from None
    except http.client.HTTPException as exc:  # a garbled response, e.g. a bad status line
        raise SendError(f"bad response ({type(exc).__name__})", True) from None
    except ValueError as exc:  # a malformed request: retrying will not help
        raise SendError(f"invalid request ({type(exc).__name__})", False) from None


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    value = exc.headers.get("Retry-After") if exc.headers else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None  # an HTTP date: fall back to our own backoff


class _Worker:
    def __init__(
        self,
        destination: Destination,
        sender: Sender,
        observer: ForwardObserver | None,
        stop: threading.Event,
    ) -> None:
        self.destination = destination
        self.queue: queue.Queue[Export | None] = queue.Queue(maxsize=destination.queue_size)
        self.queued_bytes = 0
        self._bytes_lock = threading.Lock()
        self._sender = sender
        self._observer = observer
        self._stop = stop
        self.thread = threading.Thread(
            target=self._run, name=f"kelpie-forward-{destination.name}", daemon=True
        )

    def offer(self, export: Export) -> bool:
        size = len(export.body)
        with self._bytes_lock:
            if self.queued_bytes + size > self.destination.queue_bytes:
                return False
            try:
                self.queue.put_nowait(export)
            except queue.Full:
                return False
            self.queued_bytes += size
        return True

    def _take(self, export: Export) -> None:
        with self._bytes_lock:
            self.queued_bytes -= len(export.body)

    def _run(self) -> None:
        while not self._stop.is_set():
            export = self.queue.get()
            if export is None:  # wake-up on close
                return
            self._take(export)
            self._deliver(export)

    def drain(self) -> int:
        """Remove what is still queued; returns how many requests that was."""
        dropped = 0
        while True:
            try:
                export = self.queue.get_nowait()
            except queue.Empty:
                return dropped
            if export is not None:
                self._take(export)
                dropped += 1

    def _deliver(self, export: Export) -> None:
        name = self.destination.name
        retries = min(self.destination.retries, MAX_RETRIES)
        for attempt in range(retries + 1):
            try:
                self._sender(self.destination, export)
            except SendError as exc:
                if attempt == retries or not exc.retryable:
                    log.warning("forwarding traces to %s failed: %s", name, exc)
                    if self._observer is not None:
                        self._observer.forward_failed(name)
                    return
                delay = BACKOFF_BASE * 2**attempt
                if exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
                if self._stop.wait(min(BACKOFF_MAX, delay)):
                    return
            except Exception as exc:  # a bug must not kill the thread; no message: no secrets
                log.error("forwarding traces to %s failed (%s)", name, type(exc).__name__)
                if self._observer is not None:
                    self._observer.forward_failed(name)
                return
            else:
                if self._observer is not None:
                    self._observer.forwarded(name)
                return


class Forwarder:
    """Fans accepted OTLP requests out to destinations, one queue and thread each."""

    def __init__(
        self,
        destinations: Sequence[Destination],
        observer: ForwardObserver | None = None,
        sender: Sender = send_http,
    ) -> None:
        self._stop = threading.Event()
        self._observer = observer
        self._workers = [_Worker(d, sender, observer, self._stop) for d in destinations]

    @property
    def destinations(self) -> list[Destination]:
        return [w.destination for w in self._workers]

    def start(self) -> None:
        for worker in self._workers:
            worker.thread.start()

    def submit(self, body: bytes, content_type: str, content_encoding: str | None) -> None:
        """Enqueue for every destination; never blocks."""
        export = Export(body, content_type, content_encoding)
        for worker in self._workers:
            if not worker.offer(export) and self._observer is not None:
                self._observer.forward_dropped(worker.destination.name)

    def close(self, timeout: float = 2.0) -> None:
        """Stop the threads; requests still queued are dropped and counted."""
        self._stop.set()
        for worker in self._workers:
            dropped = worker.drain()
            if dropped and self._observer is not None:
                self._observer.forward_dropped(worker.destination.name, dropped)
            with contextlib.suppress(queue.Full):  # busy: it sees the stop flag next
                worker.queue.put_nowait(None)
        for worker in self._workers:
            if worker.thread.is_alive():
                worker.thread.join(timeout)
