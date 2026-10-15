"""The shared delivery layer: fan-out, severity filters, dedup, queue, retries."""

import logging
import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest

from kelpie.alerts import Delivery, PermanentError, TransientError
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertMessage, Did, Transition, parse_alert
from kelpie.metrics import Metrics


class FakeChannel:
    def __init__(self, name: str, level: Severity = Severity.INFO) -> None:
        self.name = name
        self.min_severity = level
        self.sent: list[AlertMessage] = []
        self.errors: list[Exception] = []  # raised one per call, in order
        self.gate: threading.Event | None = None  # when set, send blocks until opened
        self.calls = 0
        self.delay = 0.0

    def send(self, message: AlertMessage) -> None:
        self.calls += 1
        time.sleep(self.delay)
        if self.gate is not None:
            self.gate.wait(5)
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(message)


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.005)


def test_fan_out_respects_each_channels_min_severity() -> None:
    both, only_critical = FakeChannel("a", Severity.WARNING), FakeChannel("b", Severity.CRITICAL)
    delivery = Delivery([both, only_critical], backoff=0.001)
    delivery.alerter().send("[r] alert", "disk is 91% full", "info")
    delivery.alerter().send("[r] alert", "disk is 95% full", "warning")
    delivery.alerter().send("[r] alert", "disk is 99% full", "critical")
    wait_for(lambda: len(both.sent) == 2 and len(only_critical.sent) == 1)
    assert [m.severity for m in both.sent] == [Severity.WARNING, Severity.CRITICAL]
    assert [m.body for m in only_critical.sent] == ["disk is 99% full"]
    delivery.close()


def test_identical_alerts_in_the_window_are_sent_once_per_channel() -> None:
    now = [0.0]
    a, b = FakeChannel("a"), FakeChannel("b")
    metrics = Metrics()
    delivery = Delivery(
        [a, b], metrics, window=timedelta(minutes=10), clock=lambda: now[0], backoff=0.001
    )
    for _ in range(3):
        delivery.alerter().send("[r] alert t1", "stuck", "warning")
    delivery.alerter().send(
        "[r] alert t1", "stuck", "critical"
    )  # another severity is another alert
    wait_for(lambda: len(a.sent) == 2 and len(b.sent) == 2)
    assert metrics.alerts_dropped[("a", "duplicate")] == 2
    now[0] = 601.0  # the window is over
    delivery.alerter().send("[r] alert t1", "stuck", "warning")
    wait_for(lambda: len(a.sent) == 3 and len(b.sent) == 3)
    delivery.close()


def test_transitions_and_the_outcome_are_read_from_the_engine_wording() -> None:
    m = parse_alert("[orphans] alert task-1", "Resolved after 2.0h: task-1 had no owner.", "info")
    assert (m.rule, m.subject, m.transition, m.did) == (
        "orphans",
        "alert task-1",
        Transition.RESOLVED,
        Did.NOTIFIED,
    )
    esc = parse_alert("[orphans] alert task-1", "Still open after 4.0h: no owner", "warning")
    assert esc.transition is Transition.ESCALATED
    changed = parse_alert("[r] alert", "Changed: now critical", "critical")
    assert changed.transition is Transition.CHANGED
    suggested = parse_alert("[r] suggest requeue task-2", "Requeue it.", "warning")
    assert (suggested.did, suggested.subject) == (Did.SUGGESTED, "requeue task-2")
    assert parse_alert("[r] failed requeue task-2", "boom", "warning").did is Did.FAILED
    held = parse_alert("[r] requeue task-2 (not executed, needs a human)", "x", "warning")
    assert (held.did, held.subject) == (Did.NOT_EXECUTED, "requeue task-2")
    plain = parse_alert("something else", "text", "bogus")
    assert (plain.rule, plain.subject, plain.severity) == ("", "something else", Severity.WARNING)


def test_a_full_queue_drops_and_counts_without_blocking() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.gate = threading.Event()
    delivery = Delivery([channel], metrics, queue_size=2, backoff=0.001)
    delivery.alerter().send("[r] alert", "m0", "warning")
    wait_for(lambda: channel.calls == 1)  # the worker holds m0
    started = time.monotonic()
    for i in range(1, 6):
        delivery.alerter().send("[r] alert", f"m{i}", "warning")
    assert time.monotonic() - started < 1.0  # send never waits for the channel
    assert metrics.alerts_dropped[("a", "queue_full")] == 3  # m1, m2 queued; m3-m5 dropped
    channel.gate.set()
    wait_for(lambda: len(channel.sent) == 3)
    delivery.alerter().send("[r] alert", "m3", "warning")  # a dropped alert may be sent again later
    wait_for(lambda: len(channel.sent) == 4)
    delivery.close()


def test_transient_errors_are_retried_with_retry_after_then_counted_as_failed() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [TransientError("rate_limited", "HTTP 429", 0.01), TransientError("x")]
    delivery = Delivery([channel], metrics, backoff=0.001)
    delivery.alerter().send("[r] alert", "one", "warning")
    wait_for(lambda: len(channel.sent) == 1)
    assert channel.calls == 3
    assert metrics.alerts_sent["a"] == 1

    channel.errors = [TransientError("server_error")] * 10
    delivery.alerter().send("[r] alert", "two", "warning")
    wait_for(lambda: metrics.alerts_failed[("a", "server_error")] == 1)
    delivery.close()


def test_permanent_and_unexpected_errors_are_not_retried(caplog: pytest.LogCaptureFixture) -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.errors = [PermanentError("rejected", "HTTP 401"), RuntimeError("secret-in-message")]
    delivery = Delivery([channel], metrics, backoff=0.001)
    with caplog.at_level(logging.DEBUG):
        delivery.alerter().send("[r] alert", "one", "warning")
        delivery.alerter().send("[r] alert", "two", "warning")
        wait_for(lambda: metrics.alerts_failed[("a", "error")] == 1)
    assert channel.calls == 2
    assert metrics.alerts_failed[("a", "rejected")] == 1
    assert "secret-in-message" not in caplog.text  # an unexpected error is logged by type only
    delivery.close()


def test_no_channels_and_a_broken_observer_never_raise() -> None:
    Delivery([]).alerter().send("[r] alert", "x", "warning")

    class Broken:
        def alert_sent(self, channel: str) -> None:
            raise RuntimeError

        def alert_failed(self, channel: str, cause: str) -> None:
            raise RuntimeError

        def alert_dropped(self, channel: str, reason: str) -> None:
            raise RuntimeError

    channel = FakeChannel("a")
    delivery = Delivery([channel], Broken(), backoff=0.001)
    delivery.alerter().send("[r] alert", "x", "warning")
    delivery.alerter().send(
        "[r] alert", "x", "warning"
    )  # a duplicate: the observer fails inside send
    delivery.alerter().send("[r] alert", "y", "warning")
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
    opened = ("[orphans] alert task-1", "task-1 has no owner.", "warning")
    resolved = ("[orphans] alert task-1", "Resolved after 5m: task-1 has no owner.", "info")
    alerter.send(*opened)
    alerter.send(*opened)  # identical to the last one sent: a duplicate
    alerter.send(*resolved)  # a different alert for the subject resets the dedup
    alerter.send(*opened)  # opened again within the window: delivered
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
    delivery = Delivery([channel], metrics, window=timedelta(hours=1), backoff=0.001)
    alerter = delivery.alerter()
    alerter.send("[r] alert", "one", "warning")
    wait_for(lambda: metrics.alerts_failed[("a", "rejected")] == 1)
    alerter.send("[r] alert", "one", "warning")  # retried by the caller later: not a duplicate
    wait_for(lambda: metrics.alerts_failed[("a", "x")] == 1)
    assert channel.calls == 2  # a retry_after beyond the cap fails at once, no capped retries
    alerter.send("[r] alert", "one", "warning")
    wait_for(lambda: len(channel.sent) == 1)
    delivery.close()


def test_close_delivers_the_queue_within_the_timeout() -> None:
    channel, metrics = FakeChannel("a"), Metrics()
    channel.delay = 0.02
    delivery = Delivery([channel], metrics, backoff=0.001)
    for i in range(5):
        delivery.alerter().send("[r] alert", f"m{i}", "warning")
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
        delivery.alerter().send("[r] alert", f"m{i}", "warning")
    with caplog.at_level(logging.WARNING):
        delivery.close(timeout=0.2)
    dropped = metrics.alerts_dropped[("a", "shutdown")]
    assert dropped >= 3
    assert len(channel.sent) + dropped == 6
    assert len([r for r in caplog.records if "at shutdown" in r.getMessage()]) == 1
