"""The delivery layer behind the ``Alerter`` port, shared by every channel.

:class:`Delivery` routes an alert to each channel whose ``min_severity`` it reaches. Per
channel there is a small bounded queue and one worker thread, so a slow or failing chat
service never blocks or crashes the control loop: ``send`` only enqueues. The worker retries
transient failures over a time horizon (default 15 minutes: capped exponential backoff,
honouring a server's ``retry_after``), as long as the queue behind the alert has room; a
429 also holds back the alerts queued after it until its ``retry_after`` has passed.
Alerts that do not fit the queue, or repeat within the dedup window, are dropped and
counted per channel. On ``close`` the queue is delivered until one shared deadline and the
rest counted as dropped ("shutdown"); after ``close``, ``send`` drops and counts too.

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
window here is a cheap safety net. An alert is a duplicate only when the last alert sent to
that channel for the same (rule, subject) is identical, so open, resolved, open again within
the window is delivered in full. An alert whose delivery fails for good gives up its
entry. The fingerprint covers the wording, severity, transition and what Kelpie did.
"""

from __future__ import annotations

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
DEFAULT_RETRY_HORIZON = timedelta(minutes=15)
MAX_WAIT = 60.0  # longest backoff between two attempts (a server's retry_after may be longer)
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
        self,
        channel: Channel,
        observer: AlertObserver | None,
        queue_size: int,
        backoff: float,
        horizon: float,
        lost: Callable[[AlertMessage], None],
    ) -> None:
        self.channel = channel
        self.observer = observer
        self.backoff = backoff
        self.horizon = horizon
        self.lost = lost  # called for an alert that will never be delivered
        self.queue: queue.Queue[AlertMessage] = queue.Queue(queue_size)
        self.stopping = threading.Event()
        self.deadline = float("inf")
        self.blocked_until = 0.0  # flood control: no send before this (monotonic) time
        # Guards the hand-over of alerts: ``current`` (taken from the queue, not settled),
        # ``closed`` (nothing may be queued any more) and ``abandoned`` (close gave up on a
        # worker stuck inside ``send``; whatever it does later is not counted again).
        self._lock = threading.Lock()
        self.current: AlertMessage | None = None
        self.closed = False
        self.abandoned = False
        self.thread = threading.Thread(
            target=self._run, name=f"kelpie-alerts-{channel.name}", daemon=True
        )
        self.thread.start()

    def offer(self, message: AlertMessage) -> str | None:
        """None when queued, else why not: ``queue_full`` or ``shutdown``."""
        with self._lock:
            if self.closed:
                return "shutdown"
            try:
                self.queue.put_nowait(message)
            except queue.Full:
                return "queue_full"
        return None

    def stop(self, deadline: float) -> None:
        """Deliver what is queued until ``deadline`` (monotonic); the rest is dropped."""
        self.deadline = deadline
        self.stopping.set()

    def abandon(self) -> None:
        """Give up on a worker that is still inside ``send`` after the deadline: count the
        alert it holds and everything queued as dropped, and ignore its late outcome."""
        with self._lock:
            self.abandoned = True
            held, self.current = self.current, None
        if held is not None:
            self._shutdown_drop(held)
        self._drain()

    def _expired(self) -> bool:
        return self.stopping.is_set() and time.monotonic() >= self.deadline

    def _run(self) -> None:
        while not self.abandoned:
            try:
                message = self.queue.get(timeout=0.1)
            except queue.Empty:
                if self.stopping.is_set():
                    self._drain()
                    return
                continue
            with self._lock:
                if self.abandoned:  # close gave up between get() and here
                    held = message
                else:
                    held = None
                    self.current = message
            if held is not None:
                self._shutdown_drop(held)
                return
            self._deliver(message)
            if self._expired():
                self._drain()
                return

    def _drain(self) -> None:
        """Nothing more is queued from now on; count what is queued as dropped."""
        with self._lock:
            self.closed = True
        dropped = 0
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            dropped += 1
            self.lost(message)
            _notify(self.observer, "alert_dropped", self.channel.name, "shutdown")
        if dropped:
            log.warning(
                "alert channel %s: %d queued alert(s) dropped at shutdown",
                self.channel.name,
                dropped,
            )

    def _settle(self) -> bool:
        """The alert in hand is done (sent, failed or dropped). False when ``close`` already
        counted it as dropped."""
        with self._lock:
            if self.abandoned:
                return False
            self.current = None
            return True

    def _deliver(self, message: AlertMessage) -> None:
        """Send, retrying transient errors with a capped exponential backoff (or the
        server's ``retry_after``) until ``horizon`` seconds have passed since the alert was
        taken. Gives up earlier when the queue behind it is full (waiting longer would only
        drop newer alerts), and drops it when shutdown runs out of time."""
        started = time.monotonic()
        attempt = 0
        while True:
            now = time.monotonic()
            wait = max(self.blocked_until - now, 0.0)  # flood control, also from earlier alerts
            if self.stopping.is_set() and now + wait >= self.deadline:
                self._shutdown_drop_current(message)
                return
            if wait > 0:
                if self.stopping.is_set():
                    time.sleep(wait)  # the deadline check above bounds it
                else:
                    self.stopping.wait(wait)  # wakes on stop; the checks above run again
                continue
            try:
                self.channel.send(message)
            except TransientError as exc:
                asked = self._block(exc, attempt)
                attempt += 1
                if time.monotonic() + asked - started > self.horizon:
                    self._failed(message, exc.cause, str(exc))
                    return
                if self.queue.full():
                    self._failed(message, exc.cause, f"{exc} (queue full, not retried)")
                    return
            except PermanentError as exc:
                self._failed(message, exc.cause, str(exc))
                return
            except Exception as exc:  # a channel bug must not kill the worker
                self._failed(message, "error", type(exc).__name__)  # the type only: no message
                return
            else:
                if self._settle():
                    _notify(self.observer, "alert_sent", self.channel.name)
                return

    def _block(self, exc: TransientError, attempt: int) -> float:
        """No send before the server's ``retry_after`` (or our backoff) has passed: for the
        next attempt and for every alert behind this one (up to the horizon). Returns the
        wait that was asked for."""
        if exc.retry_after is not None:
            asked = max(exc.retry_after, 0.0)
        else:
            asked = min(self.backoff * 2 ** min(attempt, 20), MAX_WAIT)
        until = time.monotonic() + min(asked, self.horizon)
        self.blocked_until = max(self.blocked_until, until)
        return asked

    def _shutdown_drop(self, message: AlertMessage) -> None:
        self.lost(message)
        _notify(self.observer, "alert_dropped", self.channel.name, "shutdown")
        log.warning("alert channel %s: an alert was dropped at shutdown", self.channel.name)

    def _shutdown_drop_current(self, message: AlertMessage) -> None:
        if self._settle():
            self._shutdown_drop(message)

    def _failed(self, message: AlertMessage, cause: str, detail: str) -> None:
        log.warning("alert channel %s: delivery failed (%s): %s", self.channel.name, cause, detail)
        self.lost(message)
        if self._settle():
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
        retry_horizon: timedelta = DEFAULT_RETRY_HORIZON,
    ) -> None:
        self.channels = tuple(channels)
        self.observer = observer
        self.window = window.total_seconds()
        self.clock = clock
        self._lock = threading.Lock()
        self._closed = False
        # (channel, rule, subject) -> (fingerprint, time) of the last alert queued for it
        self._last: dict[tuple[str, str, str], tuple[tuple[object, ...], float]] = {}
        self._workers = [
            _Worker(
                c,
                observer,
                queue_size,
                backoff,
                retry_horizon.total_seconds(),
                self._lost_for(c.name),
            )
            for c in self.channels
        ]

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
        name = worker.channel.name
        if self._closed:  # after close nothing is delivered; say so instead of losing it
            self._dropped(name, "shutdown")
        elif self._duplicate(name, message):
            self._dropped(name, "duplicate")
        elif (why := worker.offer(message)) is not None:
            self._lost(name, message)
            self._dropped(name, why)

    def _duplicate(self, channel: str, message: AlertMessage) -> bool:
        now = self.clock()
        key = (channel, message.rule, message.subject)
        with self._lock:
            for old in [k for k, (_, at) in self._last.items() if now - at >= self.window]:
                del self._last[old]
            previous = self._last.get(key)
            if previous is not None and previous[0] == message.fingerprint:
                return True
            self._last[key] = (message.fingerprint, now)
        return False

    def _lost_for(self, channel: str) -> Callable[[AlertMessage], None]:
        return lambda message: self._lost(channel, message)

    def _lost(self, channel: str, message: AlertMessage) -> None:
        """An alert that was not delivered must not suppress its own retry later."""
        key = (channel, message.rule, message.subject)
        with self._lock:
            entry = self._last.get(key)
            if entry is not None and entry[0] == message.fingerprint:
                del self._last[key]

    def _dropped(self, channel: str, reason: str) -> None:
        log.warning("alert channel %s: dropped an alert (%s)", channel, reason)
        _notify(self.observer, "alert_dropped", channel, reason)

    def close(self, timeout: float = 2.0) -> None:
        """Deliver what is queued within ``timeout`` (one deadline for all channels), then
        count and log the rest as dropped ("shutdown"). A worker still stuck inside a
        channel's ``send`` at the deadline is abandoned: the alert it holds is counted as
        dropped too, and whatever it does later is ignored. ``send`` after ``close`` drops
        and counts."""
        deadline = time.monotonic() + max(timeout, 0.0)
        self._closed = True
        for worker in self._workers:
            worker.stop(deadline)
        for worker in self._workers:
            worker.thread.join(max(deadline - time.monotonic(), 0.0))
        for worker in self._workers:
            if worker.thread.is_alive():
                worker.abandon()


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
