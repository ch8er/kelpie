"""The shared delivery layer: fan-out, severity filters, dedup, queue, retries."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest

from kelpie.alerts import Delivery, PermanentError, TransientError
from kelpie.alerts.delivery import DEFAULT_RETRY_HORIZON, MAX_WAIT
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, Did, Transition
from kelpie.metrics import Metrics


class FakeChannel:
    def __init__(self, name: str, level: Severity = Severity.INFO) -> None:
        self.name = name
        self.min_severity = level
        self.sent: list[AlertEvent] = []
        self.errors: list[Exception] = []  # raised one per call, in order
        self.gate: threading.Event | None = None  # when set, send blocks until opened
        self.calls = 0
        self.delay = 0.0

    def send(self, message: AlertEvent) -> None:
        self.calls += 1
        time.sleep(self.delay)
        if self.gate is not None:
            self.gate.wait(5)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(message)


def event(
    reason: str,
    severity: str = "warning",
    *,
    subject: str = "alert",
    rule: str = "r",
    **kw: object,
) -> AlertEvent:
    return AlertEvent(rule, subject, reason, Severity(severity), **kw)  # type: ignore[arg-type]


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.005)


def test_fan_out_respects_each_channels_min_severity() -> None:
    both, only_critical = FakeChannel("a", Severity.WARNING), FakeChannel("b", Severity.CRITICAL)
    delivery = Delivery([both, only_critical], backoff=0.001)
    delivery.alerter().send(event("disk is 91% full", "info"))
    delivery.alerter().send(event("disk is 95% full"))
    delivery.alerter().send(event("disk is 99% full", "critical"))
    wait_for(lambda: len(both.sent) == 2 and len(only_critical.sent) == 1)
    assert [m.severity for m in both.sent] == [Severity.WARNING, Severity.CRITICAL]
    assert [m.reason for m in only_critical.sent] == ["disk is 99% full"]
    delivery.close()


def test_identical_alerts_in_the_window_are_sent_once_per_channel() -> None:
    now = [0.0]
    a, b = FakeChannel("a"), FakeChannel("b")
    metrics = Metrics()
    delivery = Delivery(
        [a, b], metrics, window=timedelta(minutes=10), clock=lambda: now[0], backoff=0.001
    )
    for _ in range(3):
        delivery.alerter().send(event("stuck", subject="alert t1"))
    delivery.alerter().send(event("stuck", "critical", subject="alert t1"))  # another severity
    wait_for(lambda: len(a.sent) == 2 and len(b.sent) == 2)
    assert metrics.alerts_dropped[("a", "duplicate")] == 2
    now[0] = 601.0  # the window is over
    delivery.alerter().send(event("stuck", subject="alert t1"))
    wait_for(lambda: len(a.sent) == 3 and len(b.sent) == 3)
    delivery.close()


def test_a_full_queue_drops_and_counts_without_blocking() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.gate = threading.Event()
    delivery = Delivery([channel], metrics, queue_size=2, backoff=0.001)
    delivery.alerter().send(event("m0"))
    wait_for(lambda: channel.calls == 1)  # the worker holds m0
    started = time.monotonic()
    for i in range(1, 6):
        delivery.alerter().send(event(f"m{i}"))
    assert time.monotonic() - started < 1.0  # send never waits for the channel
    assert metrics.alerts_dropped[("a", "queue_full")] == 3  # m1, m2 queued; m3-m5 dropped
    channel.gate.set()
    wait_for(lambda: len(channel.sent) == 3)
    delivery.alerter().send(event("m3"))  # a dropped alert may be sent again later
    wait_for(lambda: len(channel.sent) == 4)
    delivery.close()


def test_transient_errors_are_retried_with_retry_after_then_counted_as_failed() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("rate_limited", "HTTP 429", 0.01), TransientError("x")]
    delivery = Delivery([channel], metrics, backoff=0.001, retry_horizon=timedelta(seconds=0.2))
    delivery.alerter().send(event("one"))
    wait_for(lambda: len(channel.sent) == 1)
    assert channel.calls == 3
    assert metrics.alerts_sent["a"] == 1

    channel.errors = [TransientError("server_error")] * 10_000
    delivery.alerter().send(event("two"))
    wait_for(lambda: metrics.alerts_failed[("a", "server_error")] == 1)
    assert channel.calls > 5  # retried over the horizon, not a fixed handful of times
    delivery.close()


def test_permanent_and_unexpected_errors_are_not_retried(caplog: pytest.LogCaptureFixture) -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [PermanentError("rejected", "HTTP 401"), RuntimeError("secret-in-message")]
    delivery = Delivery([channel], metrics, backoff=0.001)
    with caplog.at_level(logging.DEBUG):
        delivery.alerter().send(event("one"))
        delivery.alerter().send(event("two"))
        wait_for(lambda: metrics.alerts_failed[("a", "error")] == 1)
    assert channel.calls == 2
    assert metrics.alerts_failed[("a", "rejected")] == 1
    assert "secret-in-message" not in caplog.text  # an unexpected error is logged by type only
    delivery.close()


def test_no_channels_and_a_broken_observer_never_raise() -> None:
    Delivery([]).alerter().send(event("x"))

    class Broken:
        def alert_sent(self, channel: str) -> None:
            raise RuntimeError

        def alert_failed(self, channel: str, cause: str) -> None:
            raise RuntimeError

        def alert_dropped(self, channel: str, reason: str) -> None:
            raise RuntimeError

    channel = FakeChannel("a")
    delivery = Delivery([channel], Broken(), backoff=0.001)
    delivery.alerter().send(event("x"))
    delivery.alerter().send(event("x"))  # a duplicate: the observer fails inside send
    delivery.alerter().send(event("y"))
    wait_for(lambda: len(channel.sent) == 2)  # the worker survived the failing observer
    delivery.close()


def test_public_send_takes_free_text_for_named_channels_or_all_that_pass() -> None:
    a, b = FakeChannel("a", Severity.WARNING), FakeChannel("b", Severity.CRITICAL)
    delivery = Delivery([a, b], backoff=0.001)
    delivery.send("Daily report\nall quiet", "info")  # below both filters: nobody
    delivery.send("Daily report\n3 fixes", "warning")  # a only
    delivery.send("Daily report\nneeds you", "info", ["b"])  # named: filter not applied
    delivery.send("Daily report\nmissing", "info", ["nope"])  # unknown: skipped, no raise
    delivery.send("Daily report\nnone", "critical", [])  # an empty list is no channel
    delivery.send("Daily report\nboth", "bogus-level")  # unknown severity counts as warning
    wait_for(lambda: len(a.sent) == 2 and len(b.sent) == 1)
    assert [m.text for m in a.sent] == ["Daily report\n3 fixes", "Daily report\nboth"]
    assert [m.text for m in b.sent] == ["Daily report\nneeds you"]
    delivery.close()


def test_a_flap_within_the_window_is_delivered_but_a_true_repeat_is_not() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    delivery = Delivery([channel], metrics, window=timedelta(hours=1), backoff=0.001)
    alerter = delivery.alerter()
    opened = event("task-1 has no owner.", rule="orphans", subject="alert task-1")
    resolved = replace(
        opened,
        severity=Severity.INFO,
        transition=Transition.RESOLVED,
        age=timedelta(minutes=5),
    )
    alerter.send(opened)
    alerter.send(opened)  # identical to the last one sent: a duplicate
    alerter.send(resolved)  # a different alert for the subject resets the dedup
    alerter.send(opened)  # opened again within the window: delivered
    wait_for(lambda: len(channel.sent) == 3)
    assert [m.transition for m in channel.sent] == [
        Transition.NEW,
        Transition.RESOLVED,
        Transition.NEW,
    ]
    assert metrics.alerts_dropped[("a", "duplicate")] == 1
    delivery.close()


def test_an_alert_that_failed_for_good_does_not_block_its_own_repeat() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [PermanentError("rejected", "HTTP 400"), TransientError("x", "", 3600.0)]
    delivery = Delivery(
        [channel],
        metrics,
        window=timedelta(hours=1),
        backoff=0.001,
        retry_horizon=timedelta(seconds=0.1),
    )
    alerter = delivery.alerter()
    alerter.send(event("one"))
    wait_for(lambda: metrics.alerts_failed[("a", "rejected")] == 1)
    alerter.send(event("one"))  # retried by the caller later: not a duplicate
    wait_for(lambda: metrics.alerts_failed[("a", "x")] == 1)
    assert channel.calls == 2  # a retry_after beyond the horizon fails at once, no retries
    alerter.send(event("one"))
    wait_for(lambda: len(channel.sent) == 1)
    delivery.close()


def test_close_delivers_the_queue_within_the_timeout() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.delay = 0.02
    delivery = Delivery([channel], metrics, backoff=0.001)
    for i in range(5):
        delivery.alerter().send(event(f"m{i}"))
    delivery.close(timeout=3.0)
    assert len(channel.sent) == 5
    assert metrics.alerts_dropped[("a", "shutdown")] == 0


def test_close_counts_what_it_could_not_deliver_as_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.delay = 0.3
    delivery = Delivery([channel], metrics, backoff=0.001)
    for i in range(6):
        delivery.alerter().send(event(f"m{i}"))
    with caplog.at_level(logging.WARNING):
        delivery.close(timeout=0.2)
    dropped = metrics.alerts_dropped[("a", "shutdown")]
    assert dropped >= 3
    assert len(channel.sent) + dropped == 6
    assert (
        len([r for r in caplog.records if "queued alert(s) dropped at shutdown" in r.getMessage()])
        == 1
    )


# --- the dedup fingerprint ----------------------------------------------------------------


def test_the_fingerprint_covers_the_transition_the_age_and_what_kelpie_did() -> None:
    base = AlertEvent("r", "s", "same words", Severity.WARNING, Transition.NEW, Did.NOTIFIED)
    other_transition = AlertEvent(
        "r", "s", "same words", Severity.WARNING, Transition.CHANGED, Did.NOTIFIED
    )
    other_did = AlertEvent("r", "s", "same words", Severity.WARNING, Transition.NEW, Did.FAILED)
    assert len({base.fingerprint, other_transition.fingerprint, other_did.fingerprint}) == 3

    def still_open(minutes: int) -> AlertEvent:
        return AlertEvent(
            "r",
            "s",
            "same words",
            Severity.WARNING,
            Transition.ESCALATED,
            age=timedelta(hours=4, minutes=minutes),
        )

    # The age counts as worded: a minute later is the same alert, a tenth of an hour is not.
    assert still_open(1).fingerprint == still_open(2).fingerprint
    assert still_open(1).fingerprint != still_open(7).fingerprint

    channel, metrics = FakeChannel("a"), Metrics()
    delivery = Delivery([channel], metrics, window=timedelta(hours=1), backoff=0.001)
    for message in (base, base, other_transition, other_did, other_did):
        delivery.deliver(message)
    wait_for(lambda: len(channel.sent) == 3)
    assert metrics.alerts_dropped[("a", "duplicate")] == 2
    delivery.close()


# --- retry horizon and flood control ------------------------------------------------------


def test_transient_errors_are_retried_for_the_whole_horizon_not_a_few_attempts() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("server_error")] * 9  # more than the old five attempts
    delivery = Delivery([channel], metrics, backoff=0.001, retry_horizon=timedelta(seconds=30))
    delivery.alerter().send(event("one"))
    wait_for(lambda: len(channel.sent) == 1)
    assert channel.calls == 10
    assert metrics.alerts_sent["a"] == 1 and not metrics.alerts_failed
    delivery.close()


def test_the_default_horizon_is_fifteen_minutes_of_capped_backoff() -> None:
    assert timedelta(minutes=15) == DEFAULT_RETRY_HORIZON
    assert MAX_WAIT <= 60.0  # so 15 minutes are a few dozen attempts, not a handful


def test_a_retry_after_within_the_horizon_is_waited_out_beyond_it_fails_at_once() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("rate_limited", "HTTP 429", 0.15)]
    delivery = Delivery([channel], metrics, backoff=0.001, retry_horizon=timedelta(seconds=5))
    started = time.monotonic()
    delivery.alerter().send(event("one"))
    wait_for(lambda: len(channel.sent) == 1)
    assert time.monotonic() - started >= 0.14  # not retried before the server's wait is over
    channel.errors = [TransientError("rate_limited", "HTTP 429", 600.0)]
    delivery.alerter().send(event("two"))
    wait_for(lambda: metrics.alerts_failed[("a", "rate_limited")] == 1)
    delivery.close()


def test_retrying_gives_up_while_the_queue_is_full() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.gate = threading.Event()
    channel.errors = [TransientError("server_error")]
    delivery = Delivery([channel], metrics, queue_size=1, backoff=0.001)
    delivery.alerter().send(event("head"))
    wait_for(lambda: channel.calls == 1)  # the worker holds the head, blocked in send
    delivery.alerter().send(event("behind"))  # the queue is now full
    channel.gate.set()
    wait_for(lambda: len(channel.sent) == 1)  # the alert behind it
    assert metrics.alerts_failed[("a", "server_error")] == 1  # the head was not retried
    assert channel.calls == 2
    delivery.close()


def test_a_429_holds_back_the_next_queued_alert_too() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    stamps: list[float] = []
    original = channel.send

    def timed(message: AlertEvent) -> None:
        stamps.append(time.monotonic())
        original(message)

    channel.send = timed  # type: ignore[method-assign]
    # The first alert fails for good after a 429 whose wait exceeds the horizon: the one
    # queued behind it must still not be sent at once.
    channel.errors = [TransientError("rate_limited", "HTTP 429", 5.0)]
    delivery = Delivery([channel], metrics, backoff=0.001, retry_horizon=timedelta(seconds=0.4))
    delivery.alerter().send(event("first"))
    delivery.alerter().send(event("second"))
    wait_for(lambda: len(channel.sent) == 1)
    assert metrics.alerts_failed[("a", "rate_limited")] == 1
    assert len(stamps) == 2 and stamps[1] - stamps[0] >= 0.35
    delivery.close()


# --- shutdown -----------------------------------------------------------------------------


def hung_channels(count: int) -> list[FakeChannel]:
    channels = [FakeChannel(f"c{i}") for i in range(count)]
    for c in channels:
        c.gate = threading.Event()  # send blocks until the test opens it
    return channels


def test_close_has_one_shared_deadline_and_counts_every_alert() -> None:
    channels, metrics = hung_channels(3), Metrics()
    delivery = Delivery(channels, metrics, backoff=0.001)
    for i in range(4):  # per channel: one held inside send, three queued
        delivery.alerter().send(event(f"m{i}"))
    wait_for(lambda: all(c.calls == 1 for c in channels))
    started = time.monotonic()
    delivery.close(2.0)
    took = time.monotonic() - started
    assert 1.9 <= took < 2.6  # not 3 x (2.0 + 0.5)
    delivered = sum(metrics.alerts_sent.values())
    dropped = sum(v for (_, reason), v in metrics.alerts_dropped.items() if reason == "shutdown")
    assert delivered + dropped == 12  # none neither delivered nor counted
    assert dropped == 12
    for c in channels:
        assert c.gate is not None
        c.gate.set()
    for worker in delivery._workers:
        worker.thread.join(2.0)
        assert not worker.thread.is_alive()
    # What the hung sends do after close is not counted a second time.
    assert sum(metrics.alerts_sent.values()) == 0
    assert sum(metrics.alerts_dropped.values()) == 12
    assert not metrics.alerts_failed


def test_send_after_close_is_dropped_and_counted() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    delivery = Delivery([channel], metrics, backoff=0.001)
    delivery.close(0.5)
    delivery.alerter().send(event("late"))
    delivery.send("late report", "warning")
    assert metrics.alerts_dropped[("a", "shutdown")] == 2
    assert not channel.sent
    delivery.close(0.5)  # closing twice is harmless


def test_an_alert_offered_to_a_worker_that_already_drained_is_counted() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    delivery = Delivery([channel], metrics, backoff=0.001)
    worker = delivery._workers[0]
    worker.stop(time.monotonic())
    worker.thread.join(2.0)  # drained and gone, while Delivery itself is not yet closed
    delivery.alerter().send(event("raced"))
    assert metrics.alerts_dropped[("a", "shutdown")] == 1
    assert delivery._last == {}  # the entry was released: a later repeat is not a duplicate


def test_close_drains_the_queue_and_counts_what_it_did_not_deliver() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.delay = 0.3
    delivery = Delivery([channel], metrics, backoff=0.001)
    for i in range(5):
        delivery.alerter().send(event(f"m{i}"))
    delivery.close(0.2)
    dropped = metrics.alerts_dropped[("a", "shutdown")]
    assert len(channel.sent) + dropped == 5
    assert dropped >= 3


def test_close_does_not_wait_for_a_retry_that_would_end_after_the_deadline() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("rate_limited", "HTTP 429", 30.0)]
    delivery = Delivery([channel], metrics, backoff=0.001)
    delivery.alerter().send(event("held"))
    wait_for(lambda: channel.calls == 1)
    started = time.monotonic()
    delivery.close(0.5)
    assert time.monotonic() - started < 1.5
    assert metrics.alerts_dropped[("a", "shutdown")] == 1
    assert channel.calls == 1  # no busy-sending into a flood-control block while stopping


def test_blocked_until_is_respected_during_stop_for_the_queued_alerts() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("rate_limited", "HTTP 429", 30.0)]
    delivery = Delivery([channel], metrics, backoff=0.001, retry_horizon=timedelta(seconds=60))
    for i in range(3):
        delivery.alerter().send(event(f"m{i}"))
    wait_for(lambda: channel.calls == 1)
    started = time.monotonic()
    delivery.close(0.5)
    assert time.monotonic() - started < 1.5
    assert channel.calls == 1 and not channel.sent
    assert metrics.alerts_dropped[("a", "shutdown")] == 3


def test_a_block_that_ends_before_the_deadline_is_waited_out_during_stop() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("rate_limited", "HTTP 429", 0.3)]
    delivery = Delivery([channel], metrics, backoff=0.001, retry_horizon=timedelta(seconds=60))
    delivery.alerter().send(event("one"))
    delivery.alerter().send(event("two"))
    wait_for(lambda: channel.calls == 1)
    delivery.close(3.0)
    assert [m.reason for m in channel.sent] == ["one", "two"]
    assert metrics.alerts_dropped[("a", "shutdown")] == 0


def test_retry_after_zero_does_not_make_an_unthrottled_loop() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("rate_limited", "HTTP 429", 0.0)] * 100_000
    delivery = Delivery([channel], metrics, retry_horizon=timedelta(seconds=2))  # backoff 1 s
    delivery.alerter().send(event("one"))
    wait_for(lambda: metrics.alerts_failed[("a", "rate_limited")] == 1, timeout=5.0)
    assert 1 <= channel.calls <= 4  # the backoff is the floor between attempts
    delivery.close()
