"""Expiry in the delivery layer (#176): a channel whose messages carry controls (Telegram's
snooze buttons) hears of every resolve, also one it does not show, and the local part of an
expiry is never lost: not on a full queue, at shutdown or under flood control."""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from kelpie.alerts import Delivery
from kelpie.alerts.delivery import Expiring, _Expire
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, Transition

WAIT = 30.0  # only guards against a hang


def resolved(subject: str = "alert T-1") -> AlertEvent:
    return AlertEvent(
        "stale_blocks", subject, "no longer reported", Severity.INFO, Transition.RESOLVED
    )


def opened(subject: str = "alert T-1") -> AlertEvent:
    return AlertEvent("stale_blocks", subject, "T-1 is stuck.", Severity.WARNING)


@dataclass
class Buttons:
    """A channel with controls: records every send and expiry, in order."""

    name: str = "telegram"
    min_severity: Severity = Severity.WARNING
    expiring: bool = True
    log: list[tuple[str, str, bool]] = field(default_factory=list)  # (what, subject, remote)
    changed: threading.Condition = field(default_factory=threading.Condition)
    hold: threading.Event | None = None  # expire waits for it (the first time)
    fail: bool = False

    def send(self, message: AlertEvent) -> None:
        self._record("send", message.subject, True)

    def expire(self, message: AlertEvent, remote: bool = True) -> None:
        if self.hold is not None:
            hold, self.hold = self.hold, None
            self._record("holding", message.subject, remote)
            hold.wait(WAIT)
        if self.fail:
            raise RuntimeError("store locked")
        self._record("expire", message.subject, remote)

    def _record(self, what: str, subject: str, remote: bool) -> None:
        with self.changed:
            self.log.append((what, subject, remote))
            self.changed.notify_all()

    def wait(self, n: int) -> None:
        with self.changed:
            assert self.changed.wait_for(lambda: len(self.log) >= n, WAIT), self.log


@dataclass
class Plain:
    """A channel without controls (Slack)."""

    name: str = "slack"
    min_severity: Severity = Severity.WARNING
    sent: list[AlertEvent] = field(default_factory=list)

    def send(self, message: AlertEvent) -> None:
        self.sent.append(message)


def offers(worker: Any) -> list[object]:
    """What ``Delivery`` puts on ``worker``'s queue, in order (sends and expiries)."""
    seen: list[object] = []
    offer = worker.offer

    def recording(message: object) -> str | None:
        seen.append(message)
        return offer(message)

    worker.offer = recording
    return seen


def test_only_a_channel_with_controls_hears_of_an_unshown_resolve() -> None:
    on, off, plain = Buttons(), Buttons(name="tg-off", expiring=False), Plain()
    assert isinstance(on, Expiring) and not isinstance(plain, Expiring)
    delivery = Delivery([on, off, plain])
    offered = {worker.channel.name: offers(worker) for worker in delivery._workers}
    delivery.deliver(opened())
    delivery.deliver(resolved())  # info: below every min_severity here
    # Each channel has its own worker: close delivers every queue in order and joins them
    # all, so no channel's log is read while its worker may still write it.
    delivery.close(WAIT)
    assert on.log == [("send", "alert T-1", True), ("expire", "alert T-1", True)]
    assert off.log == [("send", "alert T-1", True)]
    assert [m.subject for m in plain.sent] == ["alert T-1"]
    # buttons off and no controls: the resolve took no queue slot
    assert {name: [isinstance(m, _Expire) for m in seen] for name, seen in offered.items()} == {
        "telegram": [False, True],
        "tg-off": [False],
        "slack": [False],
    }


def test_a_close_that_comes_as_the_worker_wakes_still_delivers_what_was_queued() -> None:
    channel = Buttons()
    delivery = Delivery([channel])
    (worker,) = delivery._workers
    get, waiting = worker.queue.get, threading.Event()

    def get_that_gave_up(timeout: float | None = None) -> object:
        # the worker's get timed out on an empty queue; alerts and close come before it
        # looks at the stopping flag
        worker.queue.get = get  # type: ignore[method-assign]
        waiting.set()
        assert worker.stopping.wait(WAIT)
        raise queue.Empty

    worker.queue.get = get_that_gave_up  # type: ignore[method-assign]
    assert waiting.wait(WAIT)
    delivery.deliver(opened())
    delivery.deliver(resolved())
    delivery.close(WAIT)
    assert channel.log == [("send", "alert T-1", True), ("expire", "alert T-1", True)]


def test_an_expiry_that_cannot_be_queued_is_done_locally_at_once() -> None:
    channel = Buttons()
    delivery = Delivery([channel])
    (worker,) = delivery._workers
    worker.offer = lambda message: "queue_full"  # type: ignore[method-assign]
    try:
        delivery.deliver(resolved())
        assert channel.log == [("expire", "alert T-1", False)]  # in the caller: store only
        channel.log.clear()
        delivery.deliver(opened())  # not a resolve: dropped, nothing to expire
        assert channel.log == []
    finally:
        delivery.close(1.0)


def test_a_shown_resolve_that_is_not_queued_still_expires_locally() -> None:
    channel = Buttons(min_severity=Severity.INFO)
    delivery = Delivery([channel])
    (worker,) = delivery._workers
    try:
        worker.offer = lambda message: "queue_full"  # type: ignore[method-assign]
        delivery.deliver(resolved())
        assert channel.log == [("expire", "alert T-1", False)]
        del worker.offer
        delivery._duplicate = lambda name, message: True  # type: ignore[method-assign]
        delivery.deliver(resolved("alert T-2"))
        assert channel.log[-1] == ("expire", "alert T-2", False)
    finally:
        delivery.close(1.0)


def test_a_local_expiry_that_fails_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    channel = Buttons(fail=True)
    delivery = Delivery([channel])
    (worker,) = delivery._workers
    worker.offer = lambda message: "queue_full"  # type: ignore[method-assign]
    try:
        delivery.deliver(resolved())  # never raises into the engine
        assert "could not expire a resolved alert: RuntimeError" in caplog.text
    finally:
        delivery.close(1.0)


def test_after_close_a_resolve_expires_nothing() -> None:
    channel = Buttons()
    delivery = Delivery([channel])
    delivery.close(1.0)
    delivery.deliver(resolved())  # the store may be closed already: left alone
    assert channel.log == []


def test_an_expiry_that_fails_never_stops_the_worker() -> None:
    channel = Buttons(fail=True)
    delivery = Delivery([channel])
    try:
        delivery.deliver(resolved())
        delivery.deliver(opened())
        channel.wait(1)
        assert channel.log == [("send", "alert T-1", True)]
    finally:
        delivery.close(1.0)


def test_under_flood_control_an_expiry_is_done_locally_only() -> None:
    channel = Buttons()
    delivery = Delivery([channel])
    (worker,) = delivery._workers
    worker.blocked_until = time.monotonic() + 3600  # Telegram asked to wait
    try:
        delivery.deliver(resolved())
        channel.wait(1)
        assert channel.log == [("expire", "alert T-1", False)]  # no request while blocked
    finally:
        delivery.close(0.1)


def test_expiries_still_queued_at_the_deadline_are_done_locally() -> None:
    gate = threading.Event()
    channel = Buttons(hold=gate)
    delivery = Delivery([channel])
    (worker,) = delivery._workers
    delivery.deliver(resolved("alert T-1"))  # the worker holds this one...
    channel.wait(1)
    delivery.deliver(resolved("alert T-2"))  # ...while these wait in the queue
    delivery.deliver(resolved("alert T-3"))
    worker.stop(time.monotonic() - 1)  # the shutdown deadline has passed
    gate.set()
    worker.thread.join(WAIT)
    assert not worker.thread.is_alive()
    assert channel.log == [
        ("holding", "alert T-1", True),
        ("expire", "alert T-1", True),
        ("expire", "alert T-2", False),  # drained: the local part only
        ("expire", "alert T-3", False),
    ]
    delivery.close(0.1)
