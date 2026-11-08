"""Edges of the blocker graph (``kelpie.rules._chains``) pinned by mutation testing."""

from datetime import UTC, datetime, timedelta

from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    HostSleep,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules._chains import (
    MISSING,
    NOT_FOUND,
    BlockerGraph,
    Chain,
    DeadEnd,
    Edge,
    behind_lookup,
    behind_text,
)

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
SINCE = NOW - 60 * M


def task(
    tid: str,
    status: Status = Status.BACKLOG,
    since: datetime = SINCE,
    block: Block | None = None,
) -> Task:
    return Task(tid, "t", status, since, block=block)


def graph(
    *tasks: Task,
    deps: tuple[tuple[str, str], ...] = (),
    conditions: tuple[Condition, ...] = (),
    sessions: tuple[Session, ...] = (),
    sleeps: tuple[HostSleep, ...] = (),
) -> BlockerGraph:
    return BlockerGraph(
        Snapshot(
            now=NOW,
            tasks=tasks,
            dependencies=tuple(Dependency(a, b) for a, b in deps),
            conditions=conditions,
            sessions=sessions,
            host_sleeps=sleeps,
        )
    )


def cond(
    cid: str,
    kind: ConditionKind = ConditionKind.TASK_STATUS,
    checked: datetime | None = NOW,
    **params: object,
) -> Condition:
    return Condition(cid, kind, task_id="T-101", params=dict(params), checked_at=checked)


def edges_of(
    *conditions: Condition, status: Status = Status.WAITING, extra: tuple[Task, ...] = ()
) -> list[Edge]:
    g = graph(task("T-101", status), *extra, conditions=conditions)
    return g.edges["T-101"]


# --- liveness ------------------------------------------------------------------------------


def test_a_session_whose_host_slept_through_the_silence_is_live() -> None:
    # Heard from 50m ago, but the host slept 40m of that: 10m awake, under the 30m bar.
    session = Session("S-1", "T-101", "agent-dev-1", NOW - 120 * M, last_heartbeat=NOW - 50 * M)
    sleep = HostSleep(NOW - 45 * M, NOW - 5 * M)
    g = graph(task("T-101"), task("T-102"), deps=(("T-101", "T-102"),), sessions=(session,))
    assert "T-101" in g.edges  # without the sleep: stale, so the task waits
    g = graph(
        task("T-101"),
        task("T-102"),
        deps=(("T-101", "T-102"),),
        sessions=(session,),
        sleeps=(sleep,),
    )
    assert "T-101" not in g.edges


def test_behind_lookup_uses_its_stale_after() -> None:
    session = Session("S-1", "T-101", "agent-dev-1", NOW - 60 * M, last_heartbeat=NOW - 10 * M)
    snap = Snapshot(
        now=NOW,
        tasks=(task("T-101"), task("T-102")),
        dependencies=(Dependency("T-101", "T-102"),),
        sessions=(session,),
    )
    assert behind_lookup(snap)("T-102") == []  # live under the default 30m
    assert behind_lookup(snap, 5 * M)("T-102") == ["T-101"]  # stale under 5m


# --- dependencies --------------------------------------------------------------------------


def test_dependency_edge() -> None:
    g = graph(task("T-101"), task("T-102"), deps=(("T-101", "T-102"),))
    assert g.edges["T-101"] == [Edge("depends on", task="T-102")]


def test_finished_or_unknown_parents_are_left_out() -> None:
    g = graph(
        task("T-101"),
        task("T-102", Status.DONE),
        task("T-103", Status.CANCELLED),
        deps=(("T-101", "T-102"), ("T-101", "T-103"), ("T-101", "T-999")),
    )
    assert g.edges["T-101"] == []


# --- conditions ----------------------------------------------------------------------------


def test_verb_follows_the_status() -> None:
    c = cond("C-1", ConditionKind.CI_STATUS)
    assert edges_of(c, status=Status.WAITING) == [
        Edge("waiting on ci_status condition C-1", external="C-1")
    ]
    assert edges_of(c, status=Status.BLOCKED) == [
        Edge("blocked on ci_status condition C-1", external="C-1")
    ]


def test_block_naming_an_unseen_condition_is_a_missing_dead_end() -> None:
    for status, verb in ((Status.BLOCKED, "blocked on"), (Status.WAITING, "waiting on")):
        blocked = task("T-101", status, block=Block("ci", SINCE, condition_id="C-9"))
        g = graph(blocked, task("T-102"), deps=(("T-101", "T-102"),))
        dead = DeadEnd("C-9", "is a condition Kelpie cannot see", kind=MISSING)
        assert g.edges["T-101"] == [
            Edge("depends on", task="T-102"),
            Edge(f"{verb} condition", dead=dead),
        ]


def test_terminal_flag_is_judged_against_the_block_start() -> None:
    # Checked after the task went blocked but before the block began: not yet seen.
    block = Block("ci", NOW - 10 * M, condition_id="C-1")
    blocked = task("T-101", Status.BLOCKED, since=NOW - 60 * M, block=block)
    c = Condition(
        "C-1", ConditionKind.CI_STATUS, params={"terminal": True}, checked_at=NOW - 30 * M
    )
    g = graph(blocked, conditions=(c,))
    assert g.edges["T-101"] == [Edge("blocked on ci_status condition C-1", external="C-1")]


def test_terminal_non_task_condition_is_a_dead_end() -> None:
    c = cond("C-1", ConditionKind.CI_STATUS, terminal=True)
    dead = DeadEnd("C-1", "C-1 (ci_status) can no longer be met")
    assert edges_of(c) == [Edge("waiting on ci_status condition C-1", dead=dead)]


def test_non_task_condition_with_a_task_param_is_external() -> None:
    c = cond("C-1", ConditionKind.PR_MERGED, task="T-102")
    assert edges_of(c, extra=(task("T-102"),)) == [
        Edge("waiting on pr_merged condition C-1", external="C-1")
    ]


def test_task_condition_leads_to_its_task() -> None:
    c = cond("C-1", task="T-102")
    assert edges_of(c, extra=(task("T-102"),)) == [
        Edge("waiting on task_status condition C-1", task="T-102")
    ]


def test_task_condition_to_an_unknown_task_without_error_is_external() -> None:
    c = cond("C-1", ConditionKind.HUMAN_TASK, task="T-102")
    assert edges_of(c) == [Edge("waiting on human_task condition C-1", external="T-102 (unknown)")]


def test_task_condition_not_found_is_a_dead_end() -> None:
    c = cond("C-1", task="T-102", error="not_found")
    why = "is not on the board (archived or out of reach)"
    assert edges_of(c) == [
        Edge("waiting on task_status condition C-1", dead=DeadEnd("T-102", why, kind=NOT_FOUND))
    ]


def test_ended_task_on_the_board_names_its_status_and_time() -> None:
    target = task("T-102", Status.CANCELLED, since=NOW - 5 * M)
    c = cond("C-1", task="T-102", terminal=True)
    assert edges_of(c, extra=(target,)) == [
        Edge(
            "waiting on task_status condition C-1",
            dead=DeadEnd("T-102", "ended as cancelled", NOW - 5 * M),
        )
    ]


def test_a_human_task_off_the_board_names_the_status_it_ended_in() -> None:
    """For a human task the connector's ``status`` is the status it ended in (for a
    ``task_status`` condition it is the wanted one, and the connector never flags a target
    off the board as terminal, so that case is not pinned)."""
    ended = cond("C-1", ConditionKind.HUMAN_TASK, task="T-102", terminal=True, status="failed")
    assert edges_of(ended) == [
        Edge("waiting on human_task condition C-1", dead=DeadEnd("T-102", "ended as failed"))
    ]


# --- chains --------------------------------------------------------------------------------


def test_a_waiting_task_with_nothing_to_check_names_its_own_status() -> None:
    # T-101 -> T-102 (backlog) -> T-103 (ready, waits for nothing).
    g = graph(
        task("T-101"),
        task("T-102", Status.BACKLOG),
        task("T-103", Status.READY),
        deps=(("T-101", "T-102"), ("T-102", "T-103")),
    )
    assert g.chains("T-101") == [
        Chain(("T-101", "T-102", "T-103"), Edge("is ready with nothing Kelpie can check"))
    ]


def test_a_diamond_reaches_the_shared_parent_once() -> None:
    # T-101 waits for T-102 and T-103, which both wait for T-104 (in progress). A DAG, so a
    # broken visited set yields a duplicate chain, not an endless walk.
    g = graph(
        task("T-101"),
        task("T-102"),
        task("T-103"),
        task("T-104", Status.IN_PROGRESS),
        deps=(("T-101", "T-102"), ("T-101", "T-103"), ("T-102", "T-104"), ("T-103", "T-104")),
    )
    assert g.chains("T-101") == [
        Chain(("T-101", "T-102", "T-104"), Edge("depends on", task="T-104"))
    ]


# --- behind_text ---------------------------------------------------------------------------


def test_behind_text_lists_exactly_shown_without_more() -> None:
    ids = [f"T-10{i}" for i in range(5)]
    assert behind_text(ids) == " 5 task(s) wait behind it: T-100, T-101, T-102, T-103, T-104."
    assert behind_text([*ids, "T-105"]).endswith("T-104 and 1 more.")
