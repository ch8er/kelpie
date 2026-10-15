from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.model import Dependency, Owner, OwnerKind, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.orphan_status import OrphanStatus, build

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
M = timedelta(minutes=1)
DEV = Owner("dev-1", "developer")


def task(
    tid: str = "T-1", status: Status = Status.IN_PROGRESS, age: int = 60, **kw: object
) -> Task:
    return Task(tid, tid, status, NOW - age * M, role="developer", **kw)  # type: ignore[arg-type]


def act(
    *tasks: Task,
    deps: tuple[Dependency, ...] = (),
    owners: tuple[Owner, ...] = (DEV,),
    rule: OrphanStatus | None = None,
) -> list:
    snap = Snapshot(now=NOW, tasks=tasks, owners=owners, dependencies=deps)
    return list((rule or OrphanStatus()).evaluate(snap))


def test_registered() -> None:
    assert isinstance(REGISTRY["orphan_status"]({}), OrphanStatus)


def test_threshold_boundary() -> None:
    assert act(task(age=29)) == []
    assert [a.kind for a in act(task(age=30))] == [ActionKind.ALERT, ActionKind.REASSIGN]


@pytest.mark.parametrize("status", [Status.BACKLOG, Status.READY, Status.DONE, Status.CANCELLED])
def test_unowned_by_design_or_terminal(status: Status) -> None:
    assert act(task(status=status)) == []


def test_owned_task_is_fine_and_unknown_owner_is_an_orphan() -> None:
    assert act(task(owner_id="dev-1")) == []
    [alert, _] = act(task(owner_id="ghost"))
    assert "no longer known" in alert.reason
    # Without an owner directory nothing can be called unknown.
    assert act(task(owner_id="ghost"), owners=()) == []


def test_severity_and_transitive_dependents() -> None:
    deps = (
        Dependency("T-2", "T-1"),
        Dependency("T-3", "T-2"),
        Dependency("T-4", "T-3"),
        Dependency("T-5", "T-1"),
    )
    downstream = [
        task("T-2", Status.BACKLOG, owner_id="dev-1"),
        task("T-3", Status.BACKLOG, owner_id="dev-1"),
        task("T-4", Status.DONE, owner_id="dev-1"),  # terminal: not waiting
        task("T-5", Status.READY, owner_id="dev-1"),
    ]
    alert = act(task(), *downstream, deps=deps)[0]
    assert alert.params == {"dependents": 3, "dependent_ids": ["T-2", "T-3", "T-5"]}
    assert alert.severity is Severity.CRITICAL
    assert act(task(), *downstream[:1], deps=deps[:1])[0].severity is Severity.WARNING


def test_reassign_picks_least_loaded_agent_of_role() -> None:
    owners = (
        Owner("dev-a", "developer"),
        Owner("dev-b", "developer"),
        Owner("lead", "developer", OwnerKind.HUMAN),
        Owner("rev", "reviewer"),
    )
    busy = task("T-9", owner_id="dev-a")
    [_, reassign] = act(task(), busy, owners=owners)
    assert reassign.params == {"owner_id": "dev-b"}


def test_no_candidate_or_disabled_means_alert_only() -> None:
    no_role = Task("T-1", "t", Status.BLOCKED, NOW - 60 * M)
    [alert] = act(no_role)
    assert "unknown role" in alert.reason
    [alert] = act(task(), rule=OrphanStatus(reassign=False))
    assert "human" not in alert.reason


def test_build_from_config() -> None:
    rule = build(
        {
            "orphan_after": "45m",
            "critical_dependents": 5,
            "owned_statuses": ["accepted"],
            "reassign": False,
        }
    )
    assert rule.orphan_after == 45 * M and rule.critical_dependents == 5
    assert rule.owned_statuses == (Status.ACCEPTED,) and not rule.reassign


@pytest.mark.parametrize(
    "config",
    [
        {"orphan_after": [1]},
        {"critical_dependents": 0},
        {"owned_statuses": []},
        {"owned_statuses": ["done"]},
        {"owned_statuses": ["nope"]},
        {"reassign": "yes"},
        {"after": "1m"},
    ],
)
def test_bad_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)
