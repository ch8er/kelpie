import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Dependency, Owner, OwnerKind, Session, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.bottlenecks import Bottlenecks, build, critical_path, role_loads

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
M = timedelta(minutes=1)
H = timedelta(hours=1)


def task(
    tid: str,
    status: Status = Status.BACKLOG,
    role: str | None = "developer",
    owner: str | None = None,
    estimate: timedelta | None = None,
    since: timedelta = 60 * M,
) -> Task:
    return Task(tid, tid, status, NOW - since, role=role, owner_id=owner, estimate=estimate)


def agents(role: str, *ids: str) -> tuple[Owner, ...]:
    return tuple(Owner(i, role) for i in ids)


def chain(*ids: str) -> tuple[Dependency, ...]:
    return tuple(Dependency(b, a) for a, b in itertools.pairwise(ids))


def snap(
    tasks: tuple[Task, ...],
    owners: tuple[Owner, ...] = (),
    deps: tuple[Dependency, ...] = (),
    sessions: tuple[Session, ...] = (),
) -> Snapshot:
    return Snapshot(now=NOW, tasks=tasks, owners=owners, dependencies=deps, sessions=sessions)


def run(snapshot: Snapshot, rule: Bottlenecks | None = None) -> list[Action]:
    return list((rule or Bottlenecks()).evaluate(snapshot))


def sequential(n: int = 5) -> tuple[tuple[Task, ...], tuple[Dependency, ...]]:
    ids = [f"T-{i}" for i in range(n)]
    tasks = (task(ids[0], Status.IN_PROGRESS, owner="dev-1"), *tuple(task(i) for i in ids[1:]))
    return tasks, chain(*ids)


def test_registered() -> None:
    assert isinstance(REGISTRY["bottlenecks"]({}), Bottlenecks)


# --- critical path -----------------------------------------------------------------------


def test_critical_path_is_the_longest_chain_by_task_count() -> None:
    tasks = tuple(task(f"T-{i}") for i in range(1, 6))
    # T-1 -> T-2 -> T-3 (three), T-4 -> T-5 (two)
    path = critical_path(snap(tasks, deps=chain("T-1", "T-2", "T-3") + chain("T-4", "T-5")))
    assert path is not None
    assert [t.id for t in path.tasks] == ["T-1", "T-2", "T-3"] and path.length == 3


def test_critical_path_ties_go_to_the_larger_estimate() -> None:
    tasks = (task("A", estimate=H), task("B", estimate=2 * H), task("C", estimate=H))
    path = critical_path(snap(tasks, deps=(Dependency("C", "A"), Dependency("C", "B"))))
    assert path is not None and [t.id for t in path.tasks] == ["B", "C"]
    assert path.estimate == 3 * H


def test_critical_path_estimate_needs_every_estimate() -> None:
    path = critical_path(snap((task("A", estimate=H), task("B")), deps=chain("A", "B")))
    assert path is not None and path.estimate is None


def test_diamond_counts_each_task_once() -> None:
    tasks = tuple(task(i) for i in "ABCD")
    deps = (Dependency("B", "A"), Dependency("C", "A"), Dependency("D", "B"), Dependency("D", "C"))
    path = critical_path(snap(tasks, deps=deps))
    assert path is not None and path.length == 3 and path.tasks[0].id == "A"
    assert path.tasks[-1].id == "D"


def test_finished_and_unknown_dependencies_are_satisfied() -> None:
    tasks = (task("A", Status.DONE), task("B"), task("C"))
    deps = (*chain("A", "B", "C"), Dependency("B", "GHOST"))
    path = critical_path(snap(tasks, deps=deps))
    assert path is not None and [t.id for t in path.tasks] == ["B", "C"]


def test_cycles_are_survived_and_left_out() -> None:
    tasks = tuple(task(i) for i in ("A", "B", "C", "D", "E"))
    # A <-> B form a cycle with C behind it; D -> E is an independent, healthy chain.
    deps = (
        Dependency("B", "A"),
        Dependency("A", "B"),
        Dependency("C", "B"),
        Dependency("E", "D"),
    )
    path = critical_path(snap(tasks, deps=deps))
    assert path is not None and [t.id for t in path.tasks] == ["D", "E"]


def test_everything_on_a_cycle_has_no_path() -> None:
    tasks = (task("A"), task("B"), task("S"))
    deps = (Dependency("B", "A"), Dependency("A", "B"), Dependency("S", "S"))
    assert critical_path(snap(tasks, deps=deps)) is None


def test_no_open_tasks_no_path() -> None:
    assert critical_path(snap(())) is None
    assert critical_path(snap((task("A", Status.DONE),))) is None


def test_duplicate_dependencies_do_not_break_the_count() -> None:
    deps = (Dependency("B", "A"), Dependency("B", "A"))
    path = critical_path(snap((task("A"), task("B")), deps=deps))
    assert path is not None and path.length == 2


def test_critical_path_scales_linearly() -> None:
    n = 20000
    ids = [f"T-{i}" for i in range(n)]
    path = critical_path(snap(tuple(task(i) for i in ids), deps=chain(*ids)))
    assert path is not None and path.length == n


# --- role loads --------------------------------------------------------------------------


def test_busy_idle_and_queue_per_role() -> None:
    tasks = (
        task("W", Status.IN_PROGRESS, owner="dev-1"),
        task("Q", Status.REVIEW, role="reviewer", since=90 * M),
        task("A", Status.ACCEPTED, role="reviewer", since=30 * M),
        task("R", Status.READY),
        task("B", Status.BACKLOG),
        task("OWNED", Status.REVIEW, role="reviewer", owner="rev-1"),
    )
    owners = agents("developer", "dev-1", "dev-2") + agents("reviewer", "rev-1")
    loads = role_loads(snap(tasks, owners))
    dev, rev = loads["developer"], loads["reviewer"]
    assert (dev.busy, dev.idle, dev.queue) == (("dev-1",), ("dev-2",), ("R",))
    assert dev.spare == ()  # its own ready task absorbs the idle developer
    assert (rev.busy, rev.idle, rev.queue) == (("rev-1",), (), ("Q", "A"))
    assert rev.oldest_wait == 90 * M


def test_ready_task_with_open_dependency_is_not_queued() -> None:
    tasks = (task("A"), task("B", Status.READY), task("C", Status.READY))
    loads = role_loads(snap(tasks, agents("developer", "d"), chain("A", "B")))
    assert loads["developer"].queue == ("C",)


def test_humans_are_not_agents() -> None:
    owners = (Owner("h", "developer", OwnerKind.HUMAN),)
    assert role_loads(snap((), owners)) == {}


def test_live_session_makes_an_agent_busy_and_recent_exit_not_idle_yet() -> None:
    owners = agents("developer", "live", "recent", "old", "never")
    sessions = (
        Session("S1", "X", "live", NOW - H, last_heartbeat=NOW - M),
        Session("S2", "X", "recent", NOW - H, ended_at=NOW - 5 * M),
        Session("S3", "X", "old", NOW - 3 * H, ended_at=NOW - 2 * H),
    )
    loads = role_loads(snap((), owners, sessions=sessions), min_idle_time=10 * M)
    dev = loads["developer"]
    assert dev.busy == ("live",) and dev.idle == ("never", "old")
    assert role_loads(snap((), owners, sessions=sessions))["developer"].idle == (
        "never",
        "old",
        "recent",
    )


def test_role_with_queue_but_no_agents_is_listed() -> None:
    loads = role_loads(snap((task("Q", Status.REVIEW, role="reviewer"),)))
    assert loads["reviewer"].agents == () and loads["reviewer"].queue == ("Q",)


# --- chain alert -------------------------------------------------------------------------


def test_sequential_chain_with_idle_agents_alerts() -> None:
    tasks, deps = sequential(5)
    owners = agents("developer", "dev-1", "dev-2", "dev-3", "dev-4")
    [action] = run(snap(tasks, owners, deps))
    assert action.kind is ActionKind.ALERT and action.severity is Severity.INFO
    assert "parallel" in action.reason and action.task_id is None
    assert action.params["critical_path_length"] == 5
    assert action.params["critical_path"] == [f"T-{i}" for i in range(5)]
    assert action.params["idle_agents"] == ["dev-2", "dev-3", "dev-4"]
    assert action.params["idle_roles"] == ["developer"]
    assert action.params["critical_path_minutes"] is None


def test_chain_message_mentions_estimate_when_known() -> None:
    tasks = (
        task("A", Status.IN_PROGRESS, owner="d1", estimate=H),
        task("B", estimate=H),
        task("C", estimate=H),
        task("D", estimate=30 * M),
    )
    [action] = run(
        snap(tasks, agents("developer", "d1", "d2", "d3"), chain("A", "B", "C", "D")),
        Bottlenecks(min_chain_length=4),
    )
    assert "3h 30m of sequential work" in action.reason
    assert action.params["critical_path_minutes"] == 210


@pytest.mark.parametrize(
    ("chain_length", "idle", "alerts"),
    [(3, 3, False), (4, 3, True), (4, 1, False), (4, 2, True)],
)
def test_chain_thresholds(chain_length: int, idle: int, alerts: bool) -> None:
    tasks, deps = sequential(chain_length)
    owners = agents("developer", "dev-1", *[f"idle-{i}" for i in range(idle)])
    rule = Bottlenecks(min_idle_agents=2, min_chain_length=4)
    assert bool(run(snap(tasks, owners, deps), rule)) is alerts


def test_idle_agents_are_those_of_the_chain_roles_only() -> None:
    tasks, deps = sequential(4)
    owners = agents("developer", "dev-1") + agents("reviewer", "rev-1", "rev-2", "rev-3")
    assert run(snap(tasks, owners, deps)) == []


def test_idle_agents_absorbed_by_ready_work_are_not_spare() -> None:
    tasks, deps = sequential(4)
    tasks += (task("X1", Status.READY), task("X2", Status.READY))
    owners = agents("developer", "dev-1", "dev-2", "dev-3")
    assert run(snap(tasks, owners, deps)) == []


def test_chain_state_key_is_stable_while_the_chain_is_worked() -> None:
    tasks, deps = sequential(5)
    owners = agents("developer", "dev-1", "dev-2", "dev-3")
    [first] = run(snap(tasks, owners, deps))
    shifted = (
        task("T-0", Status.DONE),
        task("T-1", Status.IN_PROGRESS, owner="dev-1"),
        *tasks[2:],
    )
    [second] = run(snap(shifted, owners, deps), Bottlenecks(min_chain_length=4))
    assert first.state == second.state == "bottlenecks:chain:T-4"
    assert first.key == second.key and first.since is None


def test_chain_without_roles_counts_every_agent() -> None:
    tasks = (task("A", role=None), task("B", role=None), task("C", role=None))
    owners = agents("developer", "d1") + agents("reviewer", "r1")
    rule = Bottlenecks(min_chain_length=3)
    [action] = run(snap(tasks, owners, chain("A", "B", "C")), rule)
    assert action.params["idle_roles"] == ["developer", "reviewer"]


# --- overloaded role ---------------------------------------------------------------------


def review_queue(n: int) -> tuple[Task, ...]:
    return tuple(
        task(f"R-{i}", Status.REVIEW, role="reviewer", since=(30 + i) * M) for i in range(n)
    )


def overload_owners() -> tuple[Owner, ...]:
    return agents("reviewer", "rev-1") + agents("developer", "dev-1", "dev-2")


def test_overloaded_role_with_idle_roles_elsewhere_alerts() -> None:
    tasks = (*review_queue(3), task("W", Status.REVIEW, role="reviewer", owner="rev-1"))
    [action] = run(snap(tasks, overload_owners()))
    assert action.kind is ActionKind.ALERT and action.severity is Severity.WARNING
    assert action.params == {
        "overloaded_role": "reviewer",
        "queue_depth": 3,
        "queued_tasks": ["R-2", "R-1", "R-0"],
        "oldest_wait_minutes": 32,
        "idle_roles": ["developer"],
        "idle_agents": ["dev-1", "dev-2"],
        "recommendation": "rebalance",
    }
    assert "'reviewer' is the bottleneck" in action.reason and "rebalanc" in action.reason
    assert action.state == action.key == "bottlenecks:overloaded:reviewer"


@pytest.mark.parametrize(("queued", "alerts"), [(2, False), (3, True), (5, True)])
def test_queue_depth_boundary(queued: int, alerts: bool) -> None:
    tasks = (*review_queue(queued), task("W", Status.REVIEW, role="reviewer", owner="rev-1"))
    assert bool(run(snap(tasks, overload_owners()))) is alerts


def test_no_alert_when_the_role_has_an_idle_agent() -> None:
    owners = overload_owners() + agents("reviewer", "rev-2")
    assert run(snap(review_queue(4), owners)) == []


def test_no_alert_without_idle_agents_elsewhere() -> None:
    tasks = (*review_queue(4), task("W", Status.IN_PROGRESS, owner="dev-1"))
    owners = agents("reviewer", "rev-1") + agents("developer", "dev-1")
    tasks += (task("V", Status.REVIEW, role="reviewer", owner="rev-1"),)
    assert run(snap(tasks, owners)) == []


def test_role_without_agents_is_overloaded_too() -> None:
    [action] = run(snap(review_queue(3), agents("developer", "dev-1")))
    assert "no agents" in action.reason


def test_recently_active_agents_are_not_idle_helpers() -> None:
    sessions = (Session("S", "X", "dev-1", NOW - H, ended_at=NOW - 2 * M),)
    owners = agents("developer", "dev-1")
    assert run(snap(review_queue(3), owners, sessions=sessions)) == []
    zero = Bottlenecks(min_idle_time=timedelta(0))
    assert run(snap(review_queue(3), owners, sessions=sessions), zero)


def test_both_alerts_can_fire_together() -> None:
    tasks, deps = sequential(4)
    tasks += (*review_queue(3), task("W", Status.REVIEW, role="reviewer", owner="rev-1"))
    owners = agents("developer", "dev-1", "dev-2", "dev-3") + agents("reviewer", "rev-1")
    kinds = sorted(a.params["recommendation"] for a in run(snap(tasks, owners, deps)))
    assert kinds == ["parallelize", "rebalance"]


def test_healthy_farm_is_quiet() -> None:
    tasks = (task("A", Status.IN_PROGRESS, owner="dev-1"), task("B", Status.READY))
    assert run(snap(tasks, agents("developer", "dev-1", "dev-2"))) == []
    assert run(snap(())) == []


# --- config ------------------------------------------------------------------------------


def test_build_accepts_valid_config() -> None:
    rule = build(
        {"min_idle_agents": 3, "min_chain_length": 6, "min_queue_depth": 4, "min_idle_time": "0s"}
    )
    assert rule == Bottlenecks(3, 6, 4, timedelta(0))
    assert build({"min_idle_time": "1h"}).min_idle_time == H


@pytest.mark.parametrize(
    "config",
    [
        {"min_idle_agents": 0},
        {"min_chain_length": -1},
        {"min_queue_depth": True},
        {"min_queue_depth": "3"},
        {"min_idle_agents": 1.5},
        {"min_idle_time": "-5m"},
        {"min_idle_time": "soon"},
        {"min_idle_time": None},
        {"typo": 1},
    ],
)
def test_build_rejects_bad_config(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build(config)
