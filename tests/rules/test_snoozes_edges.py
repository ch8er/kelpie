"""Edge cases of :mod:`kelpie.rules._snoozes`: the behind count and the snooze check."""

from __future__ import annotations

import signal
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import FrameType, MappingProxyType

import pytest

from kelpie.core.alerts import SnoozeState
from kelpie.core.model import (
    Condition,
    ConditionKind,
    Dependency,
    Snapshot,
    Snooze,
    Status,
    Task,
)
from kelpie.rules._snoozes import behind_count, check, describe

NOW = datetime(2026, 10, 19, 10, tzinfo=UTC)
H = timedelta(hours=1)


def task(tid: str, status: Status = Status.BLOCKED) -> Task:
    return Task(tid, f"title {tid}", status, NOW - H, "developer")


def snooze(
    grown_at: datetime | None = None, grown_by: int | None = None, behind: int | None = 0
) -> Snooze:
    return Snooze(
        "T-101", NOW + 24 * H, "deferred", "ceo", NOW - 24 * H, "cli", behind, grown_at, grown_by
    )


def snap(
    *tasks: Task,
    conditions: tuple[Condition, ...] = (),
    deps: tuple[Dependency, ...] = (),
    snoozes: tuple[Snooze, ...] = (),
) -> Snapshot:
    return Snapshot(
        NOW,
        tasks=tasks,
        dependencies=deps,
        conditions=conditions,
        snoozes=MappingProxyType({s.task_id: s for s in snoozes}),
    )


def ref(cid: str, waiter: str | None, kind: ConditionKind = ConditionKind.HUMAN_TASK) -> Condition:
    return Condition(cid, kind, waiter, {"task": "T-101"})


def test_a_condition_of_another_kind_naming_the_task_is_no_waiter() -> None:
    other = Condition("C-1", ConditionKind.PR_MERGED, "T-102", {"task": "T-101"})
    assert behind_count(snap(task("T-101"), task("T-102"), conditions=(other,)), "T-101") == 0


def test_a_skipped_condition_does_not_end_the_scan() -> None:
    skipped = Condition("C-0", ConditionKind.HUMAN_TASK, "T-102", {})  # names no task
    conditions = (skipped, ref("C-1", "T-103"))
    s = snap(task("T-101"), task("T-102"), task("T-103"), conditions=conditions)
    assert behind_count(s, "T-101") == 1


def test_a_finished_waiter_is_not_counted() -> None:
    s = snap(task("T-101"), task("T-102", Status.DONE), conditions=(ref("C-1", "T-102"),))
    assert behind_count(s, "T-101") == 0


@contextmanager
def deadline(seconds: float) -> Iterator[None]:
    def expired(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError("behind_count did not finish")

    old = signal.signal(signal.SIGALRM, expired)
    try:
        outer = signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    if outer[0]:  # an outer timer (pytest --timeout) keeps running
        signal.setitimer(signal.ITIMER_REAL, *outer)


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="needs SIGALRM")
def test_a_cycle_behind_the_task_is_counted_once_and_ends() -> None:
    deps = (
        Dependency("T-102", "T-101"),
        Dependency("T-103", "T-102"),
        Dependency("T-102", "T-103"),
    )
    s = snap(task("T-101"), task("T-102"), task("T-103"), deps=deps)
    with deadline(2.0):
        assert behind_count(s, "T-101") == 2


def test_growth_latched_at_exactly_now_is_grown() -> None:
    s = snooze(grown_at=NOW, grown_by=3)
    result = check(snap(snoozes=(s,)), "T-101", lambda: 0)
    assert result is not None and result.state is SnoozeState.GROWN
    assert result.snooze is s and result.grown == 3


def test_latched_growth_without_a_count_is_zero() -> None:
    s = snooze(grown_at=NOW - H, grown_by=None)
    result = check(snap(snoozes=(s,)), "T-101", lambda: 0)
    assert result is not None and result.state is SnoozeState.GROWN and result.grown == 0
    assert result.params()["grown"] == 0


def test_describe_without_a_title_names_the_task_only() -> None:
    assert describe(snooze()).startswith("T-101 is snoozed until ")
