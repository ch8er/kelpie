"""The delivery layer behind the ``Alerter`` port, shared by every channel.

:class:`Delivery` routes an alert to each channel whose ``min_severity`` it reaches. Per
channel there is a small bounded queue and one worker thread, so a slow or failing chat
service never blocks or crashes the control loop: ``send`` only enqueues. The worker retries
transient failures with backoff (honouring a server's ``retry_after``); alerts that do not
fit the queue, or repeat within the dedup window, are dropped and counted per channel.

A channel is a small object (``name``, ``min_severity``, ``send(message)``). It raises
:class:`TransientError` (network, 429, 5xx: retried) or :class:`PermanentError` (bad token,
unknown chat: not retried). Error details must never hold a secret; they are logged.

Two entry points, both never blocking and never raising:

- ``Delivery.send(text, severity, channels=None)`` is the public method for any producer
  (the daily report): free text to the named channels, or, with ``channels`` None, to every
  channel whose ``min_severity`` the severity reaches. Named channels are sent to regardless
  of ``min_severity``; unknown names are logged and skipped.
- ``Delivery.alerter()`` returns the engine-facing ``Alerter`` port, a thin wrapper that
  reads the engine's alert wording (transition, what Kelpie did) and routes it the same way.

The engine already de-duplicates through the action ledger and state transitions; the
window here is a cheap safety net for identical alerts.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Protocol, runtime_checkable

from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertMessage, Did, Transition, parse_alert
from kelpie.core.ports import Alerter

log = logging.getLogger(__name__)

DEFAULT_WINDOW = timedelta(minutes=10)
DEFAULT_QUEUE_SIZE = 50
MAX_ATTEMPTS = 5
MAX_WAIT = 60.0  # longest single wait before a retry, whatever the server asks
_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


class TransientError(Exception):
    """Worth retrying. ``cause`` is a short metric label; ``retry_after`` is in seconds, if
    the other side said how long to wait."""

    def __init__(self, cause: str, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(detail or cause)
        self.cause = cause
        self.retry_after = retry_after


class PermanentError(Exception):
    """Retrying cannot help (rejected credentials, unknown chat, a redirect)."""

    def __init__(self, cause: str, detail: str = "") -> None:
        super().__init__(detail or cause)
        self.cause = cause


@runtime_checkable
class Channel(Protocol):
    name: str
    min_severity: Severity

    def send(self, message: AlertMessage) -> None: ...


class AlertObserver(Protocol):
    def alert_sent(self, channel: str) -> None: ...

    def alert_failed(self, channel: str, cause: str) -> None: ...

    def alert_dropped(self, channel: str, reason: str) -> None: ...


def _notify(observer: AlertObserver | None, method: str, *args: str) -> None:
    """Metrics must never break delivery."""
    if observer is None:
        return
    try:
        getattr(observer, method)(*args)
    except Exception:
        log.exception("alert observer failed")


class _Worker:
    def __init__(
        self, channel: Channel, observer: AlertObserver | None, queue_size: int, backoff: float
    ) -> None:
        self.channel = channel
        self.observer = observer
        self.backoff = backoff
        self.queue: queue.Queue[AlertMessage | None] = queue.Queue(queue_size)
        self.stopping = threading.Event()
        self.thread = threading.Thread(
            target=self._run, name=f"kelpie-alerts-{channel.name}", daemon=True
        )
        self.thread.start()

    def offer(self, message: AlertMessage) -> bool:
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            return False
        return True

    def stop(self, timeout: float) -> None:
        self.stopping.set()
        with contextlib.suppress(queue.Full):  # else the worker sees ``stopping`` after its message
            self.queue.put_nowait(None)
        self.thread.join(timeout)

    def _run(self) -> None:
        while not self.stopping.is_set():
            message = self.queue.get()
            if message is None:
                return
            self._deliver(message)

    def _deliver(self, message: AlertMessage) -> None:
        name = self.channel.name
        for attempt in range(MAX_ATTEMPTS):
            try:
                self.channel.send(message)
            except TransientError as exc:
                if attempt == MAX_ATTEMPTS - 1:
                    self._failed(exc.cause, str(exc))
                    return
                wait = exc.retry_after if exc.retry_after is not None else self.backoff * 2**attempt
                if self.stopping.wait(min(max(wait, 0.0), MAX_WAIT)):
                    self._failed("shutdown", "stopped while retrying")
                    return
            except PermanentError as exc:
                self._failed(exc.cause, str(exc))
                return
            except Exception as exc:  # a channel bug must not kill the worker
                self._failed("error", type(exc).__name__)  # the type only: no message
                return
            else:
                _notify(self.observer, "alert_sent", name)
                return

    def _failed(self, cause: str, detail: str) -> None:
        log.warning("alert channel %s: delivery failed (%s): %s", self.channel.name, cause, detail)
        _notify(self.observer, "alert_failed", self.channel.name, cause)


class Delivery:
    """Routes messages to channels. See the module docstring for the two entry points."""

    def __init__(
        self,
        channels: Sequence[Channel],
        observer: AlertObserver | None = None,
        window: timedelta = DEFAULT_WINDOW,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        backoff: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.channels = tuple(channels)
        self.observer = observer
        self.window = window.total_seconds()
        self.clock = clock
        self._lock = threading.Lock()
        self._seen: dict[tuple[str, tuple[object, ...]], float] = {}
        self._workers = [_Worker(c, observer, queue_size, backoff) for c in self.channels]

    def send(self, text: str, severity: str, channels: Sequence[str] | None = None) -> None:
        """Send free ``text`` (as it is, up to each channel's length limit)."""
        try:
            level = _severity(severity)
            message = AlertMessage("", "", text, level, Transition.NEW, Did.NOTIFIED, text)
            self.deliver(message, channels)
        except Exception:
            log.exception("alert routing failed")

    def alerter(self) -> Alerter:
        """The engine-facing ``Alerter`` port."""
        return _EngineAlerter(self)

    def deliver(self, message: AlertMessage, channels: Sequence[str] | None = None) -> None:
        names = None if channels is None else set(channels)
        if names is not None and (unknown := names - {c.name for c in self.channels}):
            log.warning("alerts: unknown channels %s skipped", sorted(unknown))
        for worker in self._workers:
            if names is None:
                if _RANK[message.severity] >= _RANK[worker.channel.min_severity]:
                    self._route(worker, message)
            elif worker.channel.name in names:
                self._route(worker, message)

    def _route(self, worker: _Worker, message: AlertMessage) -> None:
        channel = worker.channel
        if self._duplicate(channel.name, message):
            self._dropped(channel.name, "duplicate")
        elif not worker.offer(message):
            self._forget(channel.name, message)
            self._dropped(channel.name, "queue_full")

    def _duplicate(self, channel: str, message: AlertMessage) -> bool:
        now = self.clock()
        key = (channel, message.fingerprint)
        with self._lock:
            for old in [k for k, at in self._seen.items() if now - at >= self.window]:
                del self._seen[old]
            if key in self._seen:
                return True
            self._seen[key] = now
        return False

    def _forget(self, channel: str, message: AlertMessage) -> None:
        with self._lock:
            self._seen.pop((channel, message.fingerprint), None)

    def _dropped(self, channel: str, reason: str) -> None:
        log.warning("alert channel %s: dropped an alert (%s)", channel, reason)
        _notify(self.observer, "alert_dropped", channel, reason)

    def close(self, timeout: float = 2.0) -> None:
        for worker in self._workers:
            worker.stop(timeout)


class _EngineAlerter:
    """``kelpie.core.ports.Alerter`` over a :class:`Delivery`."""

    def __init__(self, delivery: Delivery) -> None:
        self._delivery = delivery

    def send(self, title: str, body: str, severity: str) -> None:
        try:
            self._delivery.deliver(parse_alert(title, body, severity))
        except Exception:
            log.exception("alert routing failed")


def _severity(value: str) -> Severity:
    try:
        return Severity(value)
    except ValueError:
        return Severity.WARNING
