"""Edges of rule 5 (ping_pong) pinned by mutation testing."""

from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action, Severity
from kelpie.core.model import Owner, OwnerKind, Return, Snapshot, Status, Task
from kelpie.rules.ping_pong import PingPong, build

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
H = timedelta(hours=1)


def reviews(n: int) -> tuple[Return, ...]:
    return tuple(Return(NOW - (n - i) * H, f"reason {i}", "review") for i in range(n))


def task(
    tid: str = "T-101",
    returns: tuple[Return, ...] = (),
    rounds: int = 0,
    status: Status = Status.IN_PROGRESS,
    host: str | None = None,
) -> Task:
    return Task(tid, "t", status, NOW - 10 * H, review_rounds=rounds, returns=returns, host=host)


def run(*tasks: Task, owners: tuple[Owner, ...] = (), rule: PingPong | None = None) -> list[Action]:
    snap = Snapshot(now=NOW, tasks=tasks, owners=owners)
    return list((rule or PingPong()).evaluate(snap))


def test_a_terminal_task_does_not_hide_the_next() -> None:
    [a] = run(task("T-100", rounds=9, status=Status.DONE), task("T-101", rounds=3))
    assert a.task_id == "T-101"


def test_since_is_the_return_that_reached_the_limit_exactly() -> None:
    history = reviews(3)
    [a] = run(task(returns=history))
    assert a.since == history[2].at


def test_escalation_is_a_warning_with_headline_and_needed() -> None:
    [a] = run(task(rounds=3))
    assert a.severity is Severity.WARNING
    assert a.params["headline"] == "Review ping-pong on T-101: 3 rounds (limit 3)"
    assert a.params["needed"] == "Stop the loop and decide: accept, rescope or reassign the task"


def test_default_target_is_a_human_on_the_task_host() -> None:
    owners = (
        Owner("human-a", "lead", OwnerKind.HUMAN, host="host-1"),
        Owner("human-b", "lead", OwnerKind.HUMAN, host="host-2"),
    )
    [a] = run(task(rounds=3, host="host-2"), owners=owners)
    assert a.params["escalate_to"] == "human-b"


def test_a_limit_of_one_round_is_valid() -> None:
    rule = build({"max_review_rounds": 1})
    assert rule.max_review_rounds == 1
    assert run(task(rounds=1), rule=rule)
