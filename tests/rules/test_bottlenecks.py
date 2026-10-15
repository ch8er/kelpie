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


def head(tid: str, **kw: Any) -> Task:
    """A chain head: a ready task, so the chain can actually start."""
    return task(tid, Status.READY, **kw)


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
    tasks = (head("T-1"), task("T-2"), task("T-3"), head("T-4"), task("T-5"))
    # T-1 -> T-2 -> T-3 (three), T-4 -> T-5 (two)
    path = critical_path(snap(tasks, deps=chain("T-1", "T-2", "T-3") + chain("T-4", "T-5")))
    assert path is not None
    assert [t.id for t in path.tasks] == ["T-1", "T-2", "T-3"] and path.length == 3


def test_critical_path_ties_go_to_the_larger_estimate() -> None:
    tasks = (head("A", estimate=H), head("B", estimate=2 * H), task("C", estimate=H))
    path = critical_path(snap(tasks, deps=(Dependency("C", "A"), Dependency("C", "B"))))
    assert path is not None and [t.id for t in path.tasks] == ["B", "C"]
    assert path.estimate == 3 * H


def test_critical_path_estimate_needs_every_estimate() -> None:
    path = critical_path(snap((head("A", estimate=H), task("B")), deps=chain("A", "B")))
    assert path is not None and path.estimate is None


def test_diamond_counts_each_task_once() -> None:
    tasks = (head("A"), task("B"), task("C"), task("D"))
    deps = (Dependency("B", "A"), Dependency("C", "A"), Dependency("D", "B"), Dependency("D", "C"))
    path = critical_path(snap(tasks, deps=deps))
    assert path is not None and path.length == 3 and path.tasks[0].id == "A"
    assert path.tasks[-1].id == "D"


def test_finished_and_unknown_dependencies_are_satisfied() -> None:
    tasks = (task("A", Status.DONE), head("B"), task("C"))
    deps = (*chain("A", "B", "C"), Dependency("B", "GHOST"))
    path = critical_path(snap(tasks, deps=deps))
    assert path is not None and [t.id for t in path.tasks] == ["B", "C"]


def test_cycles_are_survived_and_left_out() -> None:
    tasks = (task("A"), task("B"), task("C"), head("D"), task("E"))
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
    path = critical_path(snap((head("A"), task("B")), deps=deps))
    assert path is not None and path.length == 2


def test_critical_path_scales_linearly() -> None:
    n = 20000
    ids = [f"T-{i}" for i in range(n)]
    path = critical_path(snap((head(ids[0]), *(task(i) for i in ids[1:])), deps=chain(*ids)))
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
    assert action.params["critical_path_head"] == [f"T-{i}" for i in range(5)]
    assert action.params["critical_path_tail"] == [f"T-{i}" for i in range(5)]
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
    tasks = (head("A", role=None), task("B", role=None), task("C", role=None))
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


# --- review fixes: assigned owners, runnable chains, hold labels, flapping ---------------


@pytest.mark.parametrize(
    "status", [Status.READY, Status.ACCEPTED, Status.WAITING, Status.BLOCKED, Status.REVIEW]
)
def test_owner_of_an_open_task_is_not_idle(status: Status) -> None:
    # Assigned but not started (or parked): the agent is taken, not spare.
    owned = task("O", status, owner="dev-2")
    dev = role_loads(snap((owned,), agents("developer", "dev-2", "dev-3")))["developer"]
    assert dev.busy == ("dev-2",) and dev.idle == ("dev-3",)


def test_owner_of_a_finished_task_is_idle() -> None:
    done = task("O", Status.DONE, owner="dev-2")
    assert role_loads(snap((done,), agents("developer", "dev-2")))["developer"].idle == ("dev-2",)


def test_assigned_agents_do_not_make_a_chain_look_starved() -> None:
    tasks, deps = sequential(4)
    tasks += (task("X1", Status.READY, owner="dev-2"), task("X2", Status.ACCEPTED, owner="dev-3"))
    owners = agents("developer", "dev-1", "dev-2", "dev-3")
    assert run(snap(tasks, owners, deps)) == []


def test_assigned_agents_are_not_rebalancing_helpers() -> None:
    tasks = (
        *review_queue(3),
        task("A", Status.READY, owner="dev-1"),
        task("W", Status.REVIEW, role="reviewer", owner="rev-1"),
    )
    owners = agents("reviewer", "rev-1") + agents("developer", "dev-1")
    assert run(snap(tasks, owners)) == []


@pytest.mark.parametrize("head_status", [Status.BLOCKED, Status.WAITING, Status.BACKLOG])
def test_chain_whose_head_cannot_run_is_skipped(head_status: Status) -> None:
    ids = ["A", "B", "C", "D"]
    tasks = (task("A", head_status), *(task(i) for i in ids[1:]))
    assert critical_path(snap(tasks, deps=chain(*ids))) is None
    assert run(snap(tasks, agents("developer", "d1", "d2", "d3"), chain(*ids))) == []


@pytest.mark.parametrize(
    "status", [Status.READY, Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED]
)
def test_any_runnable_or_in_flight_head_starts_a_chain(status: Status) -> None:
    path = critical_path(snap((task("A", status), task("B")), deps=chain("A", "B")))
    assert path is not None and path.length == 2


def test_blocked_step_freezes_everything_behind_it() -> None:
    tasks = (head("A"), task("B"), task("C", Status.BLOCKED), task("D"), task("E"))
    path = critical_path(snap(tasks, deps=chain("A", "B", "C", "D", "E")))
    assert path is not None and [t.id for t in path.tasks] == ["A", "B"]


def test_backlog_steps_behind_a_runnable_head_still_count() -> None:
    path = critical_path(snap((head("A"), task("B"), task("C")), deps=chain("A", "B", "C")))
    assert path is not None and path.length == 3


def test_held_tasks_are_excluded_from_the_chain() -> None:
    held = Task("C", "C", Status.BACKLOG, NOW - H, labels=("merge-first",))
    tasks = (head("A"), task("B"), held, task("D"))
    deps = chain("A", "B", "C", "D")
    path = critical_path(snap(tasks, deps=deps), frozenset({"merge-first"}))
    assert path is not None and [t.id for t in path.tasks] == ["A", "B"]
    unheld = critical_path(snap(tasks, deps=deps))
    assert unheld is not None and unheld.length == 4


def test_a_hundred_held_tasks_in_a_chain_raise_no_alert() -> None:
    ids = [f"H-{i}" for i in range(100)]
    status = [Status.READY] + [Status.BACKLOG] * 99
    held = tuple(
        Task(i, i, s, NOW - H, "developer", labels=("merge-first",))
        for i, s in zip(ids, status, strict=True)
    )
    snapshot = snap(held, agents("developer", "d1", "d2", "d3"), chain(*ids))
    assert run(snapshot, Bottlenecks(hold_labels=frozenset({"merge-first"}))) == []
    assert run(snapshot)  # without the hold label it is a real 100-step chain


def test_held_tasks_do_not_count_toward_queue_depth() -> None:
    parked = tuple(
        Task(f"P-{i}", "p", Status.REVIEW, NOW - H, "reviewer", labels=("merge-first",))
        for i in range(3)
    )
    owners = overload_owners()
    busy = (task("W", Status.REVIEW, role="reviewer", owner="rev-1"),)
    hold = Bottlenecks(hold_labels=frozenset({"merge-first"}))
    assert run(snap(parked + busy, owners)) != []
    assert run(snap(parked + busy, owners), hold) == []
    loads = role_loads(snap(parked, owners), hold_labels=frozenset({"merge-first"}))
    assert loads["reviewer"].queue == ()


def test_default_min_idle_time_is_thirty_minutes() -> None:
    assert Bottlenecks().min_idle_time == 30 * M and build({}).min_idle_time == 30 * M
    owners = agents("developer", "d1")
    gap = (Session("S", "X", "d1", NOW - H, ended_at=NOW - 20 * M),)
    assert role_loads(snap((), owners, sessions=gap), 30 * M)["developer"].idle == ()
    long_gap = (Session("S", "X", "d1", NOW - 2 * H, ended_at=NOW - 40 * M),)
    assert role_loads(snap((), owners, sessions=long_gap), 30 * M)["developer"].idle == ("d1",)


def test_long_chain_is_summarized_by_head_tail_and_count() -> None:
    n = 30
    ids = [f"T-{i:02d}" for i in range(n)]
    tasks = (task(ids[0], Status.IN_PROGRESS, owner="dev-1"), *(task(i) for i in ids[1:]))
    [action] = run(snap(tasks, agents("developer", "dev-1", "dev-2", "dev-3"), chain(*ids)))
    assert action.params["critical_path_length"] == n
    assert action.params["critical_path_head"] == ids[:5]
    assert action.params["critical_path_tail"] == ids[-5:]
    assert "critical_path" not in action.params
    assert "chain of 30 tasks (T-00 -> ... -> T-29)" in action.reason
    assert "T-15" not in action.reason


def test_long_idle_lists_are_shortened_in_the_message_only() -> None:
    tasks, deps = sequential(4)
    names = [f"dev-{i:02d}" for i in range(2, 12)]
    [action] = run(snap(tasks, agents("developer", "dev-1", *names), deps))
    assert action.params["idle_agents"] == names
    assert "+5 more" in action.reason and "dev-11" not in action.reason


def test_overload_state_began_when_the_oldest_task_started_waiting() -> None:
    tasks = (*review_queue(3), task("W", Status.REVIEW, role="reviewer", owner="rev-1"))
    [action] = run(snap(tasks, overload_owners()))
    assert action.since == NOW - 32 * M  # R-2 was queued first, 32 minutes ago


def test_overload_text_counts_only_busy_agents() -> None:
    sessions = (Session("S", "X", "rev-2", NOW - H, ended_at=NOW - 5 * M),)  # not idle yet
    owners = overload_owners() + agents("reviewer", "rev-2")
    tasks = (*review_queue(3), task("W", Status.REVIEW, role="reviewer", owner="rev-1"))
    [action] = run(snap(tasks, owners, sessions=sessions))
    assert "1 of 2 agent(s) busy, none idle" in action.reason


def test_a_reviewer_owning_a_review_task_is_busy() -> None:
    tasks = (task("W", Status.REVIEW, role="reviewer", owner="rev-1"),)
    loads = role_loads(snap(tasks, agents("reviewer", "rev-1")))
    assert loads["reviewer"].busy == ("rev-1",) and loads["reviewer"].idle == ()


def test_build_hold_labels() -> None:
    assert build({"hold_labels": ["merge-first", "parked"]}).hold_labels == frozenset(
        {"merge-first", "parked"}
    )
    assert build({}).hold_labels == frozenset()


@pytest.mark.parametrize("labels", ["merge-first", [""], [3], None])
def test_build_rejects_bad_hold_labels(labels: Any) -> None:
    with pytest.raises(ValueError):
        build({"hold_labels": labels})
