from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.model import Dependency, Owner, OwnerKind, Session, Snapshot, Status, Task
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
    sessions: tuple[Session, ...] = (),
    rule: OrphanStatus | None = None,
) -> list:
    snap = Snapshot(now=NOW, tasks=tasks, owners=owners, dependencies=deps, sessions=sessions)
    return list((rule or OrphanStatus()).evaluate(snap))


def kinds(actions: list) -> list[str]:
    return [a.kind for a in actions]


def test_registered() -> None:
    assert isinstance(REGISTRY["orphan_status"]({}), OrphanStatus)


def test_threshold_boundary() -> None:
    assert act(task(age=29)) == []
    assert kinds(act(task(age=30))) == [ActionKind.ALERT, ActionKind.REASSIGN]


@pytest.mark.parametrize("status", [Status.REVIEW, Status.ACCEPTED])
def test_queue_statuses_use_the_longer_threshold(status: Status) -> None:
    assert act(task(status=status, age=119)) == []
    assert kinds(act(task(status=status, age=120))) == [ActionKind.ALERT, ActionKind.REASSIGN]


@pytest.mark.parametrize(
    "status",
    [Status.BACKLOG, Status.READY, Status.BLOCKED, Status.WAITING, Status.DONE, Status.CANCELLED],
)
def test_statuses_left_alone(status: Status) -> None:
    assert act(task(status=status, age=600)) == []


def test_owned_task_is_fine_and_unknown_owner_only_alerts() -> None:
    assert act(task(owner_id="dev-1")) == []
    [alert] = act(task(owner_id="ghost"))
    assert "no longer known" in alert.reason and "human" in alert.reason
    rule = OrphanStatus(reassign_unknown_owner=True)
    assert kinds(act(task(owner_id="ghost"), rule=rule)) == [ActionKind.ALERT, ActionKind.REASSIGN]
    assert act(task(owner_id="ghost"), owners=()) == []  # no directory: cannot tell


def test_live_session_means_someone_is_on_it() -> None:
    live = Session("S-1", "T-1", "dev-2", NOW - 50 * M, last_heartbeat=NOW - 2 * M)
    assert act(task(), sessions=(live,)) == []
    stale = Session("S-1", "T-1", "dev-2", NOW - 300 * M, last_heartbeat=NOW - 200 * M,
                    final_status=Status.IN_PROGRESS)  # fmt: skip
    assert act(task(), sessions=(stale,)) != []  # silent for 3h20m: not live


def test_task_left_by_a_session_without_status_is_rule_1s() -> None:
    ended = Session("S-1", "T-1", "dev-2", NOW - 90 * M, ended_at=NOW - 45 * M)
    assert act(task(), sessions=(ended,)) == []
    declared = Session("S-1", "T-1", "dev-2", NOW - 90 * M, ended_at=NOW - 45 * M,
                       final_status=Status.REVIEW)  # fmt: skip
    assert act(task(), sessions=(declared,)) != []


def test_severity_transitive_dependents_and_cycles() -> None:
    deps = (
        Dependency("T-2", "T-1"),
        Dependency("T-3", "T-2"),
        Dependency("T-4", "T-3"),
        Dependency("T-5", "T-1"),
        Dependency("T-1", "T-3"),  # cycle back to the orphan
    )
    downstream = [
        task("T-2", Status.BACKLOG, owner_id="dev-1"),
        task("T-3", Status.BACKLOG, owner_id="dev-1"),
        task("T-4", Status.DONE, owner_id="dev-1"),
        task("T-5", Status.READY, owner_id="dev-1"),
    ]
    alert = act(task(), *downstream, deps=deps)[0]
    assert alert.params == {"dependents": 3, "dependent_ids": ["T-2", "T-3", "T-5"]}
    assert alert.severity is Severity.CRITICAL and alert.key.endswith(":critical")
    assert act(task(), *downstream[:1], deps=deps[:1])[0].severity is Severity.WARNING


def test_only_idle_agents_of_the_role_are_proposed() -> None:
    owners = (
        Owner("dev-a", "developer"),
        Owner("dev-b", "developer"),
        Owner("dev-c", "developer"),
        Owner("lead", "developer", OwnerKind.HUMAN),
        Owner("rev", "reviewer"),
    )
    busy_task = task("T-9", owner_id="dev-a", age=5)
    busy_session = Session("S-9", "T-8", "dev-b", NOW - 10 * M, last_heartbeat=NOW - M)
    alert, reassign = act(task(), busy_task, owners=owners, sessions=(busy_session,))
    assert reassign.params == {"owner_id": "dev-c"}
    assert "Proposed owner: dev-c" in alert.reason
    all_busy = (Owner("dev-a", "developer"),)
    [alert] = act(task(), busy_task, owners=all_busy)
    assert "No idle agent with role 'developer'" in alert.reason


def test_no_role_or_disabled_means_alert_only() -> None:
    no_role = Task("T-1", "t", Status.IN_PROGRESS, NOW - 60 * M)
    [alert] = act(no_role)
    assert "no role" in alert.reason
    [alert] = act(task(), rule=OrphanStatus(reassign=False))
    assert alert.reason.endswith("A human needs to pick it up.")


def test_build_from_config() -> None:
    rule = build(
        {
            "orphan_after": "45m",
            "queue_after": "3h",
            "critical_dependents": 5,
            "owned_statuses": ["in_progress", "accepted"],
            "queue_statuses": ["review"],
            "reassign": False,
            "reassign_unknown_owner": True,
            "session_stale_after": "1h",
        }
    )
    assert rule.orphan_after == 45 * M and rule.queue_after == 180 * M
    assert rule.owned_statuses == (Status.IN_PROGRESS, Status.ACCEPTED)
    assert rule.queue_statuses == (Status.REVIEW,)
    assert not rule.reassign and rule.reassign_unknown_owner


@pytest.mark.parametrize(
    "config",
    [
        {"orphan_after": [1]},
        {"orphan_after": "0s"},
        {"queue_after": -5},
        {"critical_dependents": 0},
        {"owned_statuses": "in_progress"},
        {"owned_statuses": ["done"]},
        {"owned_statuses": ["blocked"]},
        {"queue_statuses": ["nope"]},
        {"reassign": "yes"},
        {"after": "1m"},
    ],
)
def test_bad_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)
