"""Edge cases of the dispatch gate: host sleeps, scan order, boundaries, action fields."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import (
    Condition,
    ConditionKind,
    Dependency,
    HostSleep,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules._tasks import DEFAULT_MAX_CONDITION_AGE, DEFAULT_STALE_AFTER
from kelpie.rules.dispatch_gate import DispatchGate

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
PARENTS = DispatchGate(gate_on_parents=True)


def task(tid: str, status: Status = Status.READY, since: timedelta = 60 * M) -> Task:
    return Task(tid, f"title {tid}", status, NOW - since, "developer")


def unmet(tid: str = "T-101", checked: timedelta = M) -> Condition:
    return Condition(
        f"C-{tid}",
        ConditionKind.PR_MERGED,
        tid,
        {"repo": "example/repo", "pr": 7},
        satisfied=False,
        checked_at=NOW - checked,
        registered_at=NOW - 60 * M,
    )


def gate_conditions(tid: str, *parents: str) -> tuple[Condition, ...]:
    return tuple(
        Condition(
            f"dispatch_gate:{tid}:{p}",
            ConditionKind.TASK_STATUS,
            tid,
            {"task": p, "status": "done", "source": "dispatch_gate", "terminal_ok": True},
            registered_at=NOW - 20 * M,
        )
        for p in parents
    )


def run(
    *tasks: Task,
    conditions: tuple[Condition, ...] = (),
    deps: tuple[Dependency, ...] = (),
    sessions: tuple[Session, ...] = (),
    sleeps: tuple[HostSleep, ...] = (),
    rule: DispatchGate | None = None,
) -> list[Action]:
    snapshot = Snapshot(
        NOW,
        tasks=tasks,
        conditions=conditions,
        dependencies=deps,
        sessions=sessions,
        host_sleeps=sleeps,
    )
    return list((rule or DispatchGate()).evaluate(snapshot))


def quiet_session(source: str | None) -> Session:
    """Last heard from past the stale bar of wall time; the host slept most of it."""
    seen = NOW - DEFAULT_STALE_AFTER - 20 * M
    return Session("S-1", "T-101", "agent-dev-1", seen - M, last_heartbeat=seen, source=source)


def test_a_session_whose_server_host_slept_is_still_live() -> None:
    sleep = HostSleep(NOW - DEFAULT_STALE_AFTER, NOW - 5 * M)  # agent None: the server host
    session = quiet_session(None)
    assert run(task("T-101"), conditions=(unmet(),), sessions=(session,), sleeps=(sleep,)) == []
    # Without the sleep it is stale, so the task is not picked up and is gated.
    assert run(task("T-101"), conditions=(unmet(),), sessions=(session,))


def test_a_session_whose_agent_host_slept_is_still_live() -> None:
    sleep = HostSleep(NOW - DEFAULT_STALE_AFTER, NOW - 5 * M, agent="laptop")
    session = quiet_session("laptop/hermes")
    assert run(task("T-101"), conditions=(unmet(),), sessions=(session,), sleeps=(sleep,)) == []
    other = HostSleep(sleep.start, sleep.end, agent="desktop")  # another host's sleep
    assert run(task("T-101"), conditions=(unmet(),), sessions=(session,), sleeps=(other,))


def test_a_condition_checked_exactly_max_condition_age_ago_still_gates() -> None:
    """ "Only an unmet condition checked within ``max_condition_age`` counts": inclusive."""
    [action] = run(task("T-101"), conditions=(unmet(checked=DEFAULT_MAX_CONDITION_AGE),))
    assert action.kind is ActionKind.WAIT


def test_park_action_names_the_rule() -> None:
    [action] = run(task("T-101"), conditions=(unmet(),))
    assert action.rule == "dispatch_gate"


def test_a_parent_on_a_longer_cycle_never_gates() -> None:
    deps = (
        Dependency("T-101", "T-102"),
        Dependency("T-102", "T-103"),
        Dependency("T-103", "T-101"),
    )
    tasks = (task("T-101"), task("T-102", Status.IN_PROGRESS), task("T-103", Status.IN_PROGRESS))
    assert run(*tasks, deps=deps, rule=PARENTS) == []


def wakes(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a.kind is ActionKind.WAKE]


def releasable(tid: str = "T-109") -> tuple[Task, tuple[Condition, ...]]:
    """A task parked on a parent that was cancelled: this rule wakes it."""
    return task(tid, Status.WAITING, since=20 * M), gate_conditions(tid, "P-cancelled")


def test_release_action_fields() -> None:
    t, conditions = releasable()
    [wake] = wakes(run(t, task("P-cancelled", Status.CANCELLED), conditions=conditions))
    assert wake.rule == "dispatch_gate" and wake.task_id == "T-109"
    assert wake.key == f"dispatch_gate:release:T-109:{(NOW - 20 * M).isoformat()}"
    assert wake.params == {"waited_for": ["P-cancelled"]}


def test_release_scans_past_tasks_it_skips() -> None:
    t, conditions = releasable()
    cancelled = task("P-cancelled", Status.CANCELLED)
    skipped: list[tuple[tuple[Task, ...], tuple[Condition, ...], tuple[Dependency, ...]]] = [
        ((task("T-101", Status.IN_PROGRESS),), (), ()),  # not waiting
        ((task("T-102", Status.WAITING),), (unmet("T-102"),), ()),  # not only parent waits
        (  # a parent still gates it
            (task("T-103", Status.WAITING, since=20 * M), task("P-open", Status.IN_PROGRESS)),
            gate_conditions("T-103", "P-open"),
            (Dependency("T-103", "P-open"),),
        ),
        (  # every parent done: rule 2's
            (task("T-104", Status.WAITING, since=20 * M), task("P-done", Status.DONE)),
            gate_conditions("T-104", "P-done"),
            (Dependency("T-104", "P-done"),),
        ),
    ]
    for first, first_conditions, deps in skipped:
        actions = run(
            *first,
            t,
            cancelled,
            conditions=(*first_conditions, *conditions),
            deps=deps,
        )
        assert [a.task_id for a in wakes(actions)] == ["T-109"], first[0].id
