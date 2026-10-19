import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import (
    Condition,
    ConditionKind,
    Dependency,
    LabelEvent,
    Owner,
    OwnerKind,
    PullRequest,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules.bottlenecks import (
    Bottlenecks,
    Hold,
    build,
    critical_path,
    human_gates,
    link_pull_requests,
    role_loads,
    with_pr_labels,
)

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


GATES = Bottlenecks(escalate_human_gates=True)  # per-gate escalation; off by default


def of(actions: list[Action], kind: str) -> list[Action]:
    """Actions of one check, by key: chain, overloaded, idle-ready, hold, human-gate(d)."""
    return [a for a in actions if (a.key or "").startswith(f"bottlenecks:{kind}")]


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
    actions = run(snap(tasks, owners, deps))
    assert of(actions, "chain") == []
    # The work has waited an hour while they idle: that is its own alert.
    [idle] = actions
    assert idle.key == "bottlenecks:idle-ready" and idle.params["ready_tasks"] == ["X1", "X2"]


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
    [action] = of(run(snap(tasks, owners, chain("A", "B", "C")), rule), "chain")
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
    actions = run(snap(review_queue(4), owners))
    assert of(actions, "overloaded") == []
    [idle] = of(actions, "idle-ready")  # nobody has taken the reviews for 30 minutes
    assert idle.params["idle_agents"] == ["rev-1", "rev-2"]
    assert idle.params["likely_cause"] == "dispatch"


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
    tasks = (task("A", Status.IN_PROGRESS, owner="dev-1"), task("B", Status.READY, since=10 * M))
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
        {"escalate_human_gates": "yes"},
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
    assert of(run(snapshot), "chain")  # without the hold label it is a real 100-step chain


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


# --- idle with ready work (incident 14) ---------------------------------------------------


def labelled(tid: str, status: Status, *labels: str, since: timedelta = H, **kw: Any) -> Task:
    return Task(tid, tid, status, NOW - since, kw.pop("role", "developer"), labels=labels, **kw)


def live(owner: str, tid: str = "X") -> Session:
    return Session(f"S-{owner}", tid, owner, NOW - H, last_heartbeat=NOW - M)


def test_idle_agents_with_old_ready_work_alert_dispatch() -> None:
    tasks = (task("A", Status.READY, since=45 * M), task("B", Status.ACCEPTED, since=40 * M))
    [action] = run(snap(tasks, agents("developer", "d1")))
    assert action.key == action.state == "bottlenecks:idle-ready"
    assert action.severity is Severity.WARNING and action.since == NOW - 45 * M
    assert action.params == {
        "idle_agents": ["d1"],
        "idle_roles": ["developer"],
        "ready_tasks": ["A", "B"],
        "ready_count": 2,
        "oldest_wait_minutes": 45,
        "likely_cause": "dispatch",
    }
    assert "dispatcher" in action.reason


@pytest.mark.parametrize("since", [10 * M, 29 * M])
def test_recent_ready_work_is_not_idle_capacity_yet(since: timedelta) -> None:
    assert run(snap((task("A", Status.READY, since=since),), agents("developer", "d1"))) == []


def test_work_of_another_role_or_waiting_on_dependencies_does_not_count() -> None:
    tasks = (
        task("R", Status.REVIEW, role="reviewer", since=2 * H),
        task("B", Status.READY, since=2 * H),
        task("P", Status.IN_PROGRESS, role="reviewer", owner="rev-1"),
    )
    owners = agents("developer", "d1") + agents("reviewer", "rev-1")
    deps = (Dependency("B", "P"),)  # B waits on unfinished work
    assert of(run(snap(tasks, owners, deps)), "idle-ready") == []


def test_roleless_work_suits_any_idle_agent() -> None:
    tasks = (task("A", Status.READY, role=None, since=H),)
    owners = agents("developer", "d1") + agents("reviewer", "r1")
    [action] = run(snap(tasks, owners))
    assert action.params["idle_roles"] == ["developer", "reviewer"]
    assert action.params["idle_agents"] == ["d1", "r1"]


def test_held_work_does_not_page_while_its_hold_is_fresh() -> None:
    rule = Bottlenecks(holds=(Hold("freeze", "lead", 4 * H),))
    tasks = (labelled("A", Status.READY, "freeze"), labelled("B", Status.REVIEW, "freeze"))
    assert run(snap(tasks, agents("developer", "d1")), rule) == []


def test_full_slots_mean_no_free_capacity() -> None:
    tasks = (task("W", Status.IN_PROGRESS, owner="d1"), task("A", Status.READY, since=H))
    owners = agents("developer", "d1", "d2")
    sessions = (live("d1", "W"),)
    assert of(run(snap(tasks, owners, sessions=sessions)), "idle-ready")
    assert run(snap(tasks, owners, sessions=sessions), Bottlenecks(slots=1)) == []
    assert of(run(snap(tasks, owners, sessions=sessions), Bottlenecks(slots=2)), "idle-ready")


def test_slots_count_live_sessions_not_parked_owners() -> None:
    # d2 owns a blocked task with no session: it holds no slot.
    tasks = (
        task("W", Status.IN_PROGRESS, owner="d1"),
        task("P", Status.BLOCKED, owner="d2"),
        task("A", Status.READY, since=2 * H),
    )
    owners = agents("developer", "d1", "d2", "d3")
    [idle] = run(snap(tasks, owners, sessions=(live("d1", "W"),)), Bottlenecks(slots=2))
    assert idle.params["idle_agents"] == ["d3"]


def test_full_slots_silence_the_chain_and_turn_overload_into_priority() -> None:
    tasks, deps = sequential(4)
    owners = agents("developer", "dev-1", "dev-2", "dev-3")
    sessions = (live("dev-1", "T-0"),)
    assert of(run(snap(tasks, owners, deps, sessions)), "chain")
    assert run(snap(tasks, owners, deps, sessions), Bottlenecks(slots=1)) == []
    review = (*review_queue(3), task("W", Status.REVIEW, role="reviewer", owner="rev-1"))
    on = (live("rev-1", "W"),)
    [free] = of(run(snap(review, overload_owners(), sessions=on)), "overloaded")
    assert free.params["recommendation"] == "rebalance"
    # Every slot running: still the bottleneck, but the advice is priority, not idle agents.
    [full] = run(snap(review, overload_owners(), sessions=on), Bottlenecks(slots=1))
    assert full.key == free.key and full.params["recommendation"] == "prioritize"
    assert "give 'reviewer' work priority" in full.reason


# --- holds with an owner and an expiry (incident 14) --------------------------------------


def test_hold_past_its_expiry_alerts_with_owner_and_idle_cost() -> None:
    rule = Bottlenecks(holds=(Hold("merge-hold", "lead", 2 * H),))
    sessions = (Session("S", "Z", "d2", NOW - 5 * H, ended_at=NOW - 30 * M),)
    tasks = (
        labelled("A", Status.ACCEPTED, "merge-hold", since=3 * H),
        labelled("B", Status.READY, "merge-hold", since=H),
        labelled("C", Status.BACKLOG, "merge-hold", since=9 * H),  # backlog: not its age
        labelled("D", Status.DONE, "merge-hold", since=9 * H),  # finished: not held any more
    )
    owners = agents("developer", "d1", "d2")
    [action] = run(snap(tasks, owners, sessions=sessions), rule)
    assert action.state == "bottlenecks:hold:merge-hold" and action.since == NOW - H
    assert action.params == {
        "hold": "merge-hold",
        "owner": "lead",
        "held_tasks": ["A", "B"],
        "held_count": 2,
        "held_minutes": 180,
        "expires_after_minutes": 120,
        "idle_agents": ["d1", "d2"],
        "idle_agent_minutes": 60 + 30,  # d1 idle since the expiry, d2 since its session ended
    }
    assert "Owner: lead." in action.reason and "past its expected 2h" in action.reason
    assert "Meanwhile 2 agent(s) sit idle (d1, d2)" in action.reason


@pytest.mark.parametrize(("age", "alerts"), [(3 * H - M, False), (3 * H, True)])
def test_hold_expiry_threshold(age: timedelta, alerts: bool) -> None:
    tasks = (labelled("A", Status.READY, "park", since=age, owner_id="d1"),)
    rule = Bottlenecks(hold_labels=frozenset({"park"}), hold_expiry=3 * H)
    held = of(run(snap(tasks, agents("developer", "d1")), rule), "hold")
    assert bool(held) is alerts
    if alerts:
        assert held[0].params["owner"] is None and "No owner" in held[0].reason
        assert held[0].params["idle_agents"] == []  # d1 owns it: nobody idle
        assert "Meanwhile" not in held[0].reason


def test_bare_hold_labels_never_expire_without_hold_expiry() -> None:
    tasks = (labelled("A", Status.READY, "park", since=48 * H, owner_id="d1"),)
    rule = Bottlenecks(hold_labels=frozenset({"park"}))
    assert run(snap(tasks, agents("developer", "d1")), rule) == []


def test_listed_holds_default_to_four_hours() -> None:
    rule = Bottlenecks(holds=(Hold("h"),))
    young = (labelled("A", Status.READY, "h", since=4 * H - M, owner_id="d1"),)
    old = (labelled("A", Status.READY, "h", since=4 * H, owner_id="d1"),)
    assert run(snap(young), rule) == [] and of(run(snap(old), rule), "hold")


def test_unheld_board_has_no_hold_alerts() -> None:
    rule = Bottlenecks(holds=(Hold("merge-hold", "lead", H),))
    assert (
        run(snap((labelled("A", Status.READY, "other", since=9 * H, owner_id="d1"),)), rule) == []
    )


# --- human tasks on the critical path (incident 16) ---------------------------------------


def human(tid: str, status: Status = Status.READY, since: timedelta = 2 * H) -> Task:
    return Task(tid, f"title {tid}", status, NOW - since, "owner", owner_id="boss")


HUMANS = (Owner("boss", "owner", OwnerKind.HUMAN),)


def test_human_gates_follow_dependencies_and_current_waits_transitively() -> None:
    tasks = (
        human("H"),
        task("A", Status.BLOCKED, estimate=H),
        task("B", estimate=2 * H),
        task("C", Status.WAITING),
        human("H2", Status.BACKLOG),
        task("D"),
        task("E", Status.DONE),
        task("F", Status.IN_PROGRESS),  # a leftover condition from an earlier wait
        task("G", Status.WAITING, since=10 * M),
    )
    deps = (
        Dependency("B", "A"),
        Dependency("H2", "B"),
        Dependency("D", "H2"),
        Dependency("E", "H"),
    )
    conds = (
        Condition("C1", ConditionKind.HUMAN_TASK, "A", {"task": "H"}, satisfied=False),
        Condition("C2", ConditionKind.HUMAN_TASK, "C", {"task": "H"}, satisfied=True),  # met
        Condition("C3", ConditionKind.HUMAN_TASK, "F", {"task": "H"}, satisfied=False),
        Condition(
            "C4", ConditionKind.HUMAN_TASK, "G", {"task": "H"}, registered_at=NOW - H
        ),  # registered before G's current wait began
    )
    gates = human_gates(Snapshot(NOW, tasks, HUMANS, deps, conds))
    assert [(g.task.id, g.behind, g.actionable) for g in gates] == [
        ("H", ("A", "B", "D"), True),
        ("H2", ("D",), False),  # waits on agent work itself
    ]
    assert gates[0].estimate == 3 * H and gates[1].estimate is None


def test_gate_with_enough_work_behind_is_escalated_to_its_owner() -> None:
    tasks = (human("H"), task("A", estimate=H), task("B", estimate=30 * M), task("C"))
    snapshot = Snapshot(NOW, tasks, HUMANS, chain("H", "A", "B", "C"))
    assert [a for a in run(snapshot) if a.kind is ActionKind.ESCALATE] == []  # rule 13's
    [esc] = [a for a in run(snapshot, GATES) if a.kind is ActionKind.ESCALATE]
    assert esc.task_id == "H" and esc.state == "bottlenecks:human-gate:H"
    assert esc.since == NOW - 2 * H
    assert esc.params == {
        "owner": "boss",
        "blocked_agent_tasks": 3,
        "blocked_tasks": ["A", "B", "C"],
        "agent_hours": 1.5,
    }
    assert "does not change dependencies" in esc.reason and "1h 30m of agent work" in esc.reason
    rule = Bottlenecks(human_gate_min_tasks=4, escalate_human_gates=True)
    assert [a for a in run(snapshot, rule) if a.kind is ActionKind.ESCALATE] == []


@pytest.mark.parametrize(
    ("status", "since", "escalates"),
    [
        (Status.READY, 59 * M, False),  # just created: give the human time
        (Status.READY, H, True),
        (Status.IN_PROGRESS, H, True),
        (Status.BACKLOG, 5 * H, False),  # not released to the human yet
        (Status.BLOCKED, 5 * H, False),
    ],
)
def test_only_human_tasks_the_human_can_do_now_and_has_had_time_for_escalate(
    status: Status, since: timedelta, escalates: bool
) -> None:
    tasks = (human("H", status, since), task("A"), task("B"), task("C"))
    actions = run(Snapshot(NOW, tasks, HUMANS, chain("H", "A", "B", "C")), GATES)
    assert any(a.kind is ActionKind.ESCALATE for a in actions) is escalates


def test_human_task_waiting_on_agent_work_is_not_escalated() -> None:
    tasks = (task("P", Status.IN_PROGRESS, owner="d1"), human("H"), task("A"), task("B"), task("C"))
    snapshot = Snapshot(
        NOW, tasks, HUMANS + agents("developer", "d1"), chain("P", "H", "A", "B", "C")
    )
    assert [a for a in run(snapshot, GATES) if a.kind is ActionKind.ESCALATE] == []


def test_idle_agents_and_only_human_tasks_ready_alert() -> None:
    tasks = (
        human("H"),
        task("A"),
        task("B"),
        task("W", Status.IN_PROGRESS, owner="d1"),
        human("LONE"),  # nothing behind it
        task("R", Status.REVIEW, role="reviewer", since=2 * H),  # nobody idle could take it
    )
    owners = HUMANS + agents("developer", "d1", "d2", "d3") + agents("reviewer", "r1")
    sessions = (live("d1", "W"), live("r1", "Y"))
    [alert] = of(run(snap(tasks, owners, chain("H", "A", "B"), sessions)), "human-gated")
    assert alert.params == {
        "idle_agents": ["d2", "d3"],
        "busy_slots": 2,
        "slots": 4,
        "human_tasks": ["H"],
        "blocked_agent_tasks": 2,
        "ready_human_tasks": ["H", "LONE"],
        "recommendation": "unblock_human_tasks",
    }
    assert "2 of 4 agent slot(s) running" in alert.reason


def test_no_human_gated_alert_when_agent_work_is_ready_or_nobody_is_idle() -> None:
    owners = HUMANS + agents("developer", "d1")
    gated = (human("H"), task("A"))
    deps = chain("H", "A")
    ready = (*gated, task("R", Status.READY, since=5 * M))
    assert of(run(snap(ready, owners, deps)), "human-gated") == []
    busy = (*gated, task("W", Status.IN_PROGRESS, owner="d1"))
    assert of(run(snap(busy, owners, deps)), "human-gated") == []
    lone = (human("H"),)  # gates nothing
    assert run(snap(lone, owners)) == []


def test_chain_through_a_human_task_gets_no_parallelization_advice() -> None:
    ids = ["A0", "A1", "H", *(f"B{i}" for i in range(4))]
    tasks = tuple(human(i) if i == "H" else task(i) for i in ids[1:])
    tasks = (head("A0"), *tasks)
    owners = HUMANS + agents("developer", "d1", "d2", "d3")
    actions = run(snap(tasks, owners, chain(*ids)))
    assert of(actions, "chain") == []
    head_human = (human("H"), *(task(f"A{i}") for i in range(5)))
    gated = run(snap(head_human, owners, chain("H", *(f"A{i}" for i in range(5)))))
    assert of(gated, "chain") == [] and of(gated, "human-gated")


# --- config: holds, slots, idle and gate thresholds ----------------------------------------


def test_build_parses_holds_and_new_thresholds() -> None:
    rule = build(
        {
            "holds": [
                {"label": "merge-hold", "owner": "lead", "expires_after": "2h30m"},
                {"label": "freeze"},
            ],
            "hold_labels": ["park"],
            "hold_expiry": "6h",
            "idle_alert_after": "45m",
            "human_gate_min_tasks": 5,
            "human_gate_after": "2h",
            "slots": 4,
        }
    )
    assert rule.holds == (Hold("merge-hold", "lead", 2 * H + 30 * M), Hold("freeze"))
    assert rule.held_labels == {"merge-hold", "freeze", "park"}
    assert (rule.hold_expiry, rule.idle_alert_after) == (6 * H, 45 * M)
    assert (rule.human_gate_min_tasks, rule.human_gate_after, rule.slots) == (5, 2 * H, 4)
    defaults = build({})
    assert (defaults.hold_expiry, defaults.idle_alert_after, defaults.slots) == (None, 30 * M, None)
    assert defaults.human_gate_after == H


@pytest.mark.parametrize(
    "config",
    [
        {"holds": {"label": "x"}},
        {"holds": ["x"]},
        {"holds": [{"owner": "lead"}]},
        {"holds": [{"label": ""}]},
        {"holds": [{"label": "x", "owner": 3}]},
        {"holds": [{"label": "x", "expires_after": "0s"}]},
        {"holds": [{"label": "x", "expires_after": "later"}]},
        {"holds": [{"label": "x", "until": "5pm"}]},
        {"holds": [{"label": "x"}, {"label": "x"}]},
        {"hold_expiry": "0s"},
        {"idle_alert_after": "-1m"},
        {"human_gate_after": "soon"},
        {"slots": 0},
        {"human_gate_min_tasks": 0},
    ],
)
def test_build_rejects_bad_hold_config(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build(config)


def test_malformed_human_task_params_do_not_break_the_rule() -> None:
    tasks = (human("H"), task("A", Status.BLOCKED), task("B", Status.WAITING))
    conds = (
        Condition("C1", ConditionKind.HUMAN_TASK, "A", {"task": ["H"]}, satisfied=False),
        Condition("C2", ConditionKind.HUMAN_TASK, "B", {"task": {"id": "H"}}, satisfied=False),
    )
    snapshot = Snapshot(NOW, tasks, HUMANS, (), conds)
    assert human_gates(snapshot) == []
    assert run(snapshot) == []


def test_held_human_task_is_not_escalated() -> None:
    held = Task("H", "h", Status.READY, NOW - 2 * H, "owner", owner_id="boss", labels=("park",))
    tasks = (held, task("A"), task("B"), task("C"))
    owners = HUMANS + agents("developer", "d1")
    snapshot = snap(tasks, owners, chain("H", "A", "B", "C"))
    rule = Bottlenecks(hold_labels=frozenset({"park"}), escalate_human_gates=True)
    assert [a for a in run(snapshot, rule) if a.kind is ActionKind.ESCALATE] == []
    assert of(run(snapshot, rule), "human-gated") == []
    assert [a for a in run(snapshot, GATES) if a.kind is ActionKind.ESCALATE]  # unheld


def test_chain_ending_in_a_human_task_still_gets_parallelization_advice() -> None:
    ids = [*(f"A{i}" for i in range(4)), "H"]
    tasks = (head("A0"), *(task(i) for i in ids[1:-1]), human("H", Status.BACKLOG))
    owners = HUMANS + agents("developer", "d1", "d2", "d3")
    assert of(run(snap(tasks, owners, chain(*ids))), "chain")


# --- ready_is_queued (Hermes assigns before dispatch) -------------------------------------


def test_assigned_ready_work_is_queued_with_ready_is_queued() -> None:
    tasks = (task("A", Status.READY, owner="dev-1", since=H),)
    owners = agents("developer", "dev-1", "dev-2")
    assert run(snap(tasks, owners)) == []  # default: dev-1 is busy with A, nothing queued
    rule = Bottlenecks(ready_is_queued=True)
    [alert] = run(snap(tasks, owners), rule)
    assert alert.key == "bottlenecks:idle-ready" and alert.params["ready_tasks"] == ["A"]
    assert alert.params["likely_cause"] == "dispatch"  # its own owner is idle
    load = role_loads(snap(tasks, owners), ready_is_queued=True)["developer"]
    assert load.queue == ("A",) and load.busy == ()


def test_assigned_ready_work_of_a_busy_owner_suggests_reassigning() -> None:
    tasks = (
        task("A", Status.READY, owner="dev-1", since=H),
        task("W", Status.IN_PROGRESS, owner="dev-1"),
    )
    sessions = (live("dev-1", "W"),)
    [alert] = run(
        snap(tasks, agents("developer", "dev-1", "dev-2"), sessions=sessions),
        Bottlenecks(ready_is_queued=True),
    )
    assert alert.params["likely_cause"] == "assigned_busy" and alert.params["idle_agents"] == [
        "dev-2"
    ]
    assert "reassign" in alert.reason


def test_ready_task_with_a_live_session_is_not_queued() -> None:
    tasks = (task("A", Status.READY, owner="dev-1", since=H),)
    sessions = (live("dev-1", "A"),)  # claimed: the session has started it
    assert (
        run(
            snap(tasks, agents("developer", "dev-1", "dev-2"), sessions=sessions),
            Bottlenecks(ready_is_queued=True),
        )
        == []
    )


def test_build_parses_ready_is_queued() -> None:
    assert build({"ready_is_queued": True}).ready_is_queued is True
    with pytest.raises(ValueError):
        build({"ready_is_queued": "yes"})


ASSIGNED = Bottlenecks(ready_is_queued=True)


def test_human_owned_ready_work_is_never_queued() -> None:
    owners = (Owner("alice", "alice", OwnerKind.HUMAN), *agents("developer", "dev-1", "dev-2"))
    tasks = tuple(
        task(f"H{i}", Status.READY, role="alice", owner="alice", since=H) for i in range(3)
    )
    assert run(snap(tasks, owners), ASSIGNED) == []
    assert role_loads(snap(tasks, owners), ready_is_queued=True).get("alice") is None


def test_assigned_backlog_or_blocked_work_does_not_make_an_owner_busy() -> None:
    tasks = (
        task("A", Status.READY, owner="dev-1", since=2 * H),
        task("B", Status.BACKLOG, owner="dev-1"),
        task("C", Status.BLOCKED, owner="dev-1"),
    )
    owners = agents("developer", "dev-1", "dev-2")
    load = role_loads(snap(tasks, owners), ready_is_queued=True)["developer"]
    assert load.busy == () and load.queue == ("A",)
    [alert] = run(snap(tasks, owners), ASSIGNED)
    assert alert.params["likely_cause"] == "dispatch" and alert.params["assigned_busy_tasks"] == []
    assert "1 queued task(s)" in alert.reason


def test_assigned_review_lane_counts_as_queued() -> None:
    reviews = tuple(
        task(f"R{i}", Status.REVIEW, role="reviewer", owner="rev-1", since=(40 + i) * M)
        for i in range(4)
    )
    busy_dev = (task("W", Status.IN_PROGRESS, owner="dev-1"),)
    owners = agents("reviewer", "rev-1", "rev-2") + agents("developer", "dev-1", "dev-2")
    sessions = (live("rev-2", "X"), live("dev-1", "W"))
    actions = run(snap(reviews + busy_dev, owners, sessions=sessions), ASSIGNED)
    load = role_loads(snap(reviews, owners, sessions=sessions), ready_is_queued=True)["reviewer"]
    assert load.queue == ("R3", "R2", "R1", "R0")  # oldest first
    [idle] = of(actions, "idle-ready")  # rev-1 owns them all but is not running any
    assert idle.params["idle_agents"] == ["rev-1"] and idle.params["likely_cause"] == "dispatch"


def test_slots_count_running_tasks_not_agents() -> None:
    tasks = (
        *(task(f"W{i}", Status.IN_PROGRESS, owner="dev-1") for i in range(3)),
        task("A", Status.READY, owner="dev-1", since=H),
    )
    owners = agents("developer", "dev-1", "dev-2")
    sessions = tuple(live("dev-1", f"W{i}") for i in range(3))
    # Three running tasks fill three slots, though only one agent runs them.
    assert (
        run(snap(tasks, owners, sessions=sessions), Bottlenecks(ready_is_queued=True, slots=3))
        == []
    )
    [alert] = run(
        snap(tasks, owners, sessions=sessions), Bottlenecks(ready_is_queued=True, slots=4)
    )
    assert alert.params["likely_cause"] == "assigned_busy"
    assert alert.params["assigned_busy_tasks"] == ["A"]


def test_recently_active_owner_is_not_called_busy() -> None:
    sessions = (Session("S", "Z", "dev-1", NOW - H, ended_at=NOW - 10 * M),)  # not idle yet
    tasks = (task("A", Status.READY, owner="dev-1", since=H),)
    [alert] = run(snap(tasks, agents("developer", "dev-1", "dev-2"), sessions=sessions), ASSIGNED)
    assert alert.params["likely_cause"] == "dispatch"


def test_mixed_work_lists_the_tasks_behind_busy_agents() -> None:
    tasks = (
        task("A", Status.READY, owner="dev-1", since=H),
        task("W", Status.IN_PROGRESS, owner="dev-1"),
        task("B", Status.READY, since=H),
    )
    sessions = (live("dev-1", "W"),)
    [alert] = run(snap(tasks, agents("developer", "dev-1", "dev-2"), sessions=sessions), ASSIGNED)
    assert alert.params["likely_cause"] == "dispatch"
    assert alert.params["assigned_busy_tasks"] == ["A"]


def test_assigned_queued_work_suppresses_the_human_gated_alert_only_when_takeable() -> None:
    owners = HUMANS + agents("developer", "dev-1", "dev-2")
    gated = (human("H"), task("X"), task("Y"))
    deps = chain("H", "X", "Y")
    assigned = (task("A", Status.READY, owner="dev-1", since=5 * M),)
    assert of(run(snap(gated + assigned, owners, deps), ASSIGNED), "human-gated") == []
    assert of(run(snap(gated + assigned, owners, deps)), "human-gated")  # default: dev-1 busy


def test_expired_hold_counts_idle_agents_for_assigned_held_work() -> None:
    rule = Bottlenecks(ready_is_queued=True, holds=(Hold("merge-hold", "lead", H),))
    held = labelled("A", Status.READY, "merge-hold", since=2 * H, owner_id="dev-1")
    [alert] = of(run(snap((held,), agents("developer", "dev-1", "dev-2")), rule), "hold")
    assert alert.params["idle_agents"] == ["dev-1", "dev-2"]


def test_human_gated_slot_counts_never_exceed_the_total() -> None:
    tasks = (
        human("H"),
        task("X"),
        *(task(f"W{i}", Status.IN_PROGRESS, owner="d1") for i in range(3)),
    )
    owners = HUMANS + agents("developer", "d1", "d2")
    sessions = (*(live("d1", f"W{i}") for i in range(3)), live("d1", "W0"), live("boss", "H"))
    snapshot = snap(tasks, owners, chain("H", "X"), sessions)
    [alert] = of(run(snapshot), "human-gated")
    assert (alert.params["busy_slots"], alert.params["slots"]) == (1, 2)  # agents, no cap
    [capped] = of(run(snapshot, Bottlenecks(slots=5)), "human-gated")
    assert (capped.params["busy_slots"], capped.params["slots"]) == (3, 5)  # tasks; humans none


def test_build_escalate_human_gates() -> None:
    assert build({}).escalate_human_gates is False
    assert build({"escalate_human_gates": True}).escalate_human_gates is True


# --- open pull requests (GitHub pull requests step) ---------------------------------------


def pr(
    number: int,
    branch: str | None = None,
    *labels: str,
    since: timedelta = H,
    mergeable: bool | None = True,
    **kw: Any,
) -> PullRequest:
    return PullRequest(
        kw.pop("repo", "example/repo"),
        number,
        f"PR {number}",
        branch,
        labels,
        mergeable=mergeable,
        **{"updated_at": NOW - since, **kw},
    )


def on_branch(tid: str, status: Status, branch: str, since: timedelta = H, **kw: Any) -> Task:
    return Task(tid, tid, status, NOW - since, kw.pop("role", "developer"), branch=branch, **kw)


def with_prs(snapshot: Snapshot, *prs: PullRequest) -> Snapshot:
    return Snapshot(
        now=snapshot.now,
        tasks=snapshot.tasks,
        owners=snapshot.owners,
        dependencies=snapshot.dependencies,
        sessions=snapshot.sessions,
        pull_requests=prs,
    )


MERGERS = Bottlenecks(ready_prs=True)  # agents merge PRs on this farm


def test_tasks_link_to_the_open_prs_that_name_them() -> None:
    tasks = (
        on_branch("A", Status.REVIEW, "feat/a"),
        on_branch("B", Status.READY, "shared"),
        on_branch("C", Status.DONE, "feat/c"),  # finished: its PR stands on its own
        task("D", Status.READY),
    )
    prs = (
        pr(1, "feat/a", "merge-hold", task_ids=("A",)),
        pr(2, "shared", "x", task_ids=("B",)),
        pr(3, "other", "y", repo="example/other", task_ids=("B", "Z")),  # Z: not on the board
        pr(4, "feat/c", task_ids=("C",)),
        pr(5, "feat/d"),  # names no task: a branch alone never links (incident 17)
    )
    linked, loose = link_pull_requests(with_prs(snap(tasks), *prs))
    assert {t: [p.number for p in ps] for t, ps in linked.items()} == {"A": [1], "B": [2, 3]}
    assert [p.number for p in loose] == [4, 5]
    labelled_snapshot = with_pr_labels(with_prs(snap(tasks), *prs), linked)
    assert labelled_snapshot.tasks[0].labels == ("merge-hold",)
    assert labelled_snapshot.tasks[1].labels == ("x", "y")
    assert labelled_snapshot.tasks[2:] == tasks[2:]
    unchanged = with_prs(snap(tasks), *prs)
    assert with_pr_labels(unchanged, {}) is unchanged


def test_a_hold_label_on_a_pr_holds_its_task() -> None:
    rule = Bottlenecks(holds=(Hold("merge-first", "lead", 2 * H),))
    tasks = (on_branch("A", Status.REVIEW, "feat/a", since=3 * H),)
    owners = agents("developer", "d1")
    base = snap(tasks, owners)
    # Without the PR the review task is plain queued work.
    assert of(run(base, rule), "idle-ready")
    actions = run(
        with_prs(base, pr(7, "feat/a", "merge-first", mergeable=False, task_ids=("A",))), rule
    )
    assert of(actions, "idle-ready") == []
    [hold] = of(actions, "hold")
    assert hold.params["held_tasks"] == ["A"] and hold.params["held_count"] == 1
    assert "held_prs" not in hold.params  # the PR is its task's, not more held work


def test_a_hold_on_any_pr_of_the_task_holds_it() -> None:
    rule = Bottlenecks(holds=(Hold("merge-hold", "lead", 2 * H),), ready_prs=True)
    tasks = (on_branch("T1", Status.REVIEW, "fix/ci", since=3 * H),)
    prs = (
        pr(1, "fix/ci", "merge-hold", task_ids=("T1",)),
        pr(2, "fix/ci", repo="example/other", task_ids=("T1",)),
    )
    actions = run(with_prs(snap(tasks, agents("developer", "d1")), *prs), rule)
    assert of(actions, "idle-ready") == []  # neither the task nor the other PR is ready work
    [hold] = of(actions, "hold")
    assert hold.params["held_tasks"] == ["T1"]


def test_pr_label_time_ages_the_hold() -> None:
    rule = Bottlenecks(holds=(Hold("merge-first", "lead", 2 * H),))
    tasks = (on_branch("A", Status.REVIEW, "feat/a", since=5 * H),)
    owners = agents("developer", "d1")
    fresh = pr(
        7,
        "feat/a",
        "merge-first",
        task_ids=("A",),
        labeled=(LabelEvent("merge-first", NOW - 90 * M),),
    )
    # The task has waited 5h, but the hold went on 90 minutes ago: not expired yet.
    assert of(run(with_prs(snap(tasks, owners), fresh), rule), "hold") == []
    old = pr(
        7,
        "feat/a",
        "merge-first",
        task_ids=("A",),
        labeled=(LabelEvent("merge-first", NOW - 3 * H),),
    )
    [hold] = of(run(with_prs(snap(tasks, owners), old), rule), "hold")
    assert hold.params["held_minutes"] == 180
    # The task moved after the label went on: the hold is as old as the move.
    moved = (on_branch("A", Status.REVIEW, "feat/a", since=150 * M),)
    [hold] = of(run(with_prs(snap(moved, owners), old), rule), "hold")
    assert hold.params["held_minutes"] == 150


def test_held_prs_without_a_task_are_held_work() -> None:
    rule = Bottlenecks(holds=(Hold("merge-first", "lead", 2 * H),))
    prs = (
        pr(
            1,
            "x",
            "merge-first",
            mergeable=False,
            labeled=(LabelEvent("merge-first", NOW - 4 * H),),
        ),
        pr(2, "y", "merge-first", since=3 * H),  # label time unknown: its last change
        pr(3, "z", "merge-first", since=9 * H, draft=True),  # drafts are not held work
        pr(4, "w", "merge-first", mergeable=None, updated_at=None, created_at=None),  # no age
        pr(5, None, "merge-first", since=9 * H, fork=True),  # a fork's PR is not ours
    )
    [hold] = run(with_prs(snap((), agents("developer", "d1")), *prs), rule)
    assert hold.params["held_tasks"] == []
    assert hold.params["held_prs"] == ["example/repo#1", "example/repo#2"]
    assert hold.params["held_count"] == 2 and hold.params["held_minutes"] == 240
    assert hold.params["idle_agents"] == []  # people merge (no ready_prs): no idle cost
    mergers = Bottlenecks(holds=rule.holds, ready_prs=True)
    [hold] = run(with_prs(snap((), agents("developer", "d1")), *prs), mergers)
    assert hold.params["idle_agents"] == ["d1"]
    assert "its 2 PR(s) (example/repo#1, example/repo#2)" in hold.reason


def test_mergeable_prs_are_ready_work_only_where_agents_merge() -> None:
    owners = agents("developer", "d1") + agents("reviewer", "r1")
    assert run(with_prs(snap((), owners), pr(1, "a", since=2 * H))) == []  # off by default
    prs = (
        pr(1, "a", since=2 * H),
        pr(2, "b", mergeable=False),  # not mergeable
        pr(3, "c", mergeable=None),  # unknown
        pr(4, "d", draft=True),
        pr(5, "e", since=10 * M),  # changed recently
        pr(6, "f", "park"),  # held
        pr(7, None, fork=True),  # a fork's PR
    )
    rule = Bottlenecks(hold_labels=frozenset({"park"}), ready_prs=True)
    [action] = run(with_prs(snap((), owners), *prs), rule)
    assert action.key == "bottlenecks:idle-ready"
    assert action.params["ready_prs"] == ["example/repo#1"]
    assert action.params["ready_tasks"] == ["example/repo#1"]
    assert action.params["ready_count"] == 1 and action.params["oldest_wait_minutes"] == 120
    assert action.params["likely_cause"] == "merge"
    assert action.params["idle_agents"] == ["d1", "r1"]  # no pr_role: any idle agent
    assert "1 mergeable PR(s) wait" in action.reason and "any agent" in action.reason


def test_pr_role_limits_who_could_merge() -> None:
    owners = agents("developer", "d1") + agents("reviewer", "r1")
    rule = Bottlenecks(ready_prs=True, pr_role="reviewer")
    [action] = run(with_prs(snap((), owners), pr(1, "a")), rule)
    assert action.params["idle_agents"] == ["r1"] and "the reviewer role" in action.reason
    busy = (task("W", Status.IN_PROGRESS, role="reviewer", owner="r1"),)
    assert run(with_prs(snap(busy, owners), pr(1, "a")), rule) == []


def test_prs_add_to_the_tasks_cause() -> None:
    tasks = (task("A", Status.READY, since=3 * H),)
    [action] = run(with_prs(snap(tasks, agents("developer", "d1")), pr(1, "a")), MERGERS)
    assert action.params["ready_tasks"] == ["A", "example/repo#1"]
    assert action.params["ready_count"] == 2 and action.params["likely_cause"] == "dispatch"
    assert "1 queued task(s) and 1 mergeable PR(s) wait" in action.reason
    assert "dispatcher" in action.reason and "nobody merges the PRs" in action.reason


def test_a_mergeable_pr_keeps_the_assigned_busy_cause() -> None:
    owners = agents("developer", "a1", "a2")
    tasks = (
        task("W", Status.IN_PROGRESS, owner="a1"),
        task("T2", Status.READY, owner="a1", since=2 * H),
    )
    rule = Bottlenecks(ready_prs=True, ready_is_queued=True)
    sessions = (live("a1", "W"),)
    [action] = run(with_prs(snap(tasks, owners, sessions=sessions), pr(1, "a")), rule)
    assert action.params["likely_cause"] == "assigned_busy"
    assert action.params["assigned_busy_tasks"] == ["T2"]


def test_a_pr_whose_task_is_open_is_not_counted_twice() -> None:
    tasks = (on_branch("A", Status.REVIEW, "feat/a", since=3 * H),)
    linked = pr(1, "feat/a", task_ids=("A",))
    [action] = run(with_prs(snap(tasks, agents("developer", "d1")), linked), MERGERS)
    assert action.params["ready_tasks"] == ["A"] and "ready_prs" not in action.params


def test_mergeable_prs_mean_idle_agents_are_not_gated_by_humans() -> None:
    owners = (*agents("developer", "d1"), Owner("alice", "owner", OwnerKind.HUMAN))
    tasks = (
        Task("H", "Sign", Status.READY, NOW - 3 * H, "owner", owner_id="alice"),
        *(task(f"T{i}", Status.BACKLOG) for i in range(3)),
    )
    deps = tuple(Dependency(f"T{i}", "H") for i in range(3))
    base = snap(tasks, owners, deps)
    assert of(run(base), "human-gated")
    assert of(run(with_prs(base, pr(1, "a"))), "human-gated")  # PRs are not ready work by default
    actions = run(with_prs(base, pr(1, "a")), MERGERS)
    assert of(actions, "human-gated") == [] and of(actions, "idle-ready")


def test_build_pull_request_params() -> None:
    rule = build({"ready_prs": True, "pr_role": "reviewer"})
    assert rule.ready_prs is True and rule.pr_role == "reviewer"
    assert build({}).ready_prs is False
    with pytest.raises(ValueError, match="ready_prs"):
        build({"ready_prs": "no"})
    with pytest.raises(ValueError, match="pr_role"):
        build({"pr_role": ""})


def test_work_routed_to_a_busy_agent_suggests_reassigning() -> None:
    """Hermes: ready work routed to a busy profile (``assignee``) and claimed by nobody
    waits for that profile while others idle."""
    tasks = (
        Task("A", "A", Status.READY, NOW - H, "developer", assignee="dev-1"),
        task("W", Status.IN_PROGRESS, owner="dev-1"),
    )
    sessions = (live("dev-1", "W"),)
    [alert] = run(snap(tasks, agents("developer", "dev-1", "dev-2"), sessions=sessions))
    assert alert.key == "bottlenecks:idle-ready"
    assert alert.params["likely_cause"] == "assigned_busy" and "dev-1" in alert.reason
    assert alert.params["assigned_busy_tasks"] == ["A"]


# --- planned waits are not waiting (incident 20) ----------------------------------------


def test_planned_tasks_are_not_behind_a_human_task() -> None:
    from dataclasses import replace

    later = replace(task("P"), labels=("later",))
    tasks = (human("H"), later, task("C"), task("D"), replace(task("Q"), prework=True))
    deps = (Dependency("P", "H"), Dependency("C", "P"), Dependency("D", "H"))
    deps += (Dependency("D", "P"), Dependency("Q", "H"))
    [gate] = human_gates(Snapshot(NOW, tasks, HUMANS, deps), frozenset({"later"}))
    assert gate.behind == ("D",)  # P planned, C only behind it, D also directly behind H
    [gate] = human_gates(Snapshot(NOW, tasks, HUMANS, deps))  # without the label
    assert gate.behind == ("C", "D", "P")  # Q, in pre-work, is planned anyway
    # A task parked on the human task was started: it waits on it, labels or not.
    parked = replace(task("W", Status.WAITING), labels=("later",))
    cond = Condition("C1", ConditionKind.HUMAN_TASK, "W", {"task": "H"}, satisfied=False)
    snapshot = Snapshot(NOW, (human("H"), parked), HUMANS, (), (cond,))
    assert [g.behind for g in human_gates(snapshot, {"later"})] == [("W",)]
    # A ready one labelled later is planned.
    ready = replace(task("R", Status.READY), labels=("later",))
    deps = (Dependency("R", "H"),)
    assert human_gates(Snapshot(NOW, (human("H"), ready), HUMANS, deps), {"later"}) == []


def test_a_planned_human_task_gates_only_what_a_condition_parks_on_it() -> None:
    from dataclasses import replace

    gate = replace(human("H"), labels=("later",))
    deps = (Dependency("A", "H"), Dependency("B", "W"))
    assert human_gates(Snapshot(NOW, (gate, task("A")), HUMANS, deps[:1]), {"later"}) == []
    assert human_gates(Snapshot(NOW, (gate, task("A")), HUMANS, deps[:1]))
    # A started task parked on it by a condition waits on it, and so does what depends on it.
    parked = task("W", Status.WAITING)
    cond = Condition("C1", ConditionKind.HUMAN_TASK, "W", {"task": "H"}, satisfied=False)
    tasks = (gate, task("A"), parked, task("B"))
    [g] = human_gates(Snapshot(NOW, tasks, HUMANS, deps, (cond,)), {"later"})
    assert g.behind == ("B", "W")  # not A, which only depends on it


def test_rule_6_reads_held_dependents_and_pr_labels_as_planned() -> None:
    from dataclasses import replace

    tasks = (human("H", since=3 * H), task("A"), task("B"))
    deps = (Dependency("A", "H"), Dependency("B", "H"))
    rule = Bottlenecks(
        escalate_human_gates=True, human_gate_min_tasks=2, hold_labels=frozenset({"hold"})
    )
    [esc] = of(run(Snapshot(NOW, tasks, HUMANS, deps), rule), "human-gate:")
    assert esc.params["blocked_agent_tasks"] == 2
    held = (tasks[0], replace(tasks[1], labels=("hold",)), tasks[2])
    assert of(run(Snapshot(NOW, held, HUMANS, deps), rule), "human-gate:") == []  # 1 left
    pr = PullRequest("org/repo", 12, "B: thing", branch="b", labels=("hold",), task_ids=("B",))
    on_pr = Snapshot(NOW, held, HUMANS, deps, pull_requests=(pr,))
    assert human_gates(with_pr_labels(on_pr, link_pull_requests(on_pr)[0]), {"hold"}) == []


def test_build_planned_labels() -> None:
    assert build({"planned_labels": ["later"]}).planned_labels == {"later"}
    with pytest.raises(ValueError, match="planned_labels must be a list"):
        build({"planned_labels": "later"})
