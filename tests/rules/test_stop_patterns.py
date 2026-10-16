"""Rule 3's stop patterns (orphan_status) and the blocker graph they share with `kelpie why`."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Severity
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules._chains import BlockerGraph
from kelpie.rules.orphan_status import OrphanStatus, _follow, build

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
M = timedelta(minutes=1)
DEV = Owner("dev-1", "developer")
REV = Owner("rev-1", "reviewer")


def task(tid: str = "T-1", status: Status = Status.REVIEW, age: int = 180, **kw: object) -> Task:
    kw.setdefault("role", "developer")
    return Task(tid, tid, status, NOW - age * M, **kw)  # type: ignore[arg-type]


def evaluate(
    *tasks: Task,
    deps: tuple[Dependency, ...] = (),
    owners: tuple[Owner, ...] = (DEV, REV),
    sessions: tuple[Session, ...] = (),
    conditions: tuple[Condition, ...] = (),
    rule: OrphanStatus | None = None,
) -> list:
    snap = Snapshot(
        now=NOW,
        tasks=tasks,
        owners=owners,
        dependencies=deps,
        sessions=sessions,
        conditions=conditions,
    )
    return list((rule or OrphanStatus()).evaluate(snap))


def patterns(actions: list) -> list[tuple[str, str | None]]:
    return [(a.params.get("pattern", "orphan"), a.task_id) for a in actions]


def ended(tid: str, at: int, final: Status | None, start: int | None = None) -> Session:
    started = NOW - (start if start is not None else at + 30) * M
    return Session(f"S-{tid}", tid, "dev-1", started, ended_at=NOW - at * M, final_status=final)


def live(tid: str) -> Session:
    return Session(f"L-{tid}", tid, "dev-1", NOW - 20 * M, last_heartbeat=NOW - M)


def cond(
    cid: str,
    kind: ConditionKind,
    tid: str,
    satisfied: bool | None = False,
    **params: object,
) -> Condition:
    return Condition(
        cid,
        kind,
        task_id=tid,
        params=dict(params),
        satisfied=satisfied,
        checked_at=NOW - M,
    )


# -- a) review with nothing driving it ---------------------------------------------------


def test_review_owned_by_a_non_reviewer_alerts_with_a_stable_state() -> None:
    [alert] = evaluate(task(owner_id="dev-1"))
    assert alert.params["pattern"] == "review-undriven" and alert.severity is Severity.WARNING
    assert alert.state == f"orphan_status:review-undriven:T-1:{(NOW - 180 * M).isoformat()}"
    assert alert.since == NOW - 180 * M


def test_review_threshold_counts_from_the_last_session_end() -> None:
    assert evaluate(task(owner_id="dev-1", age=119)) == []
    # In review for 3h, but a session looked at it 30 minutes ago.
    seen = ended("T-1", 30, Status.REVIEW, start=200)
    assert evaluate(task(owner_id="dev-1"), sessions=(seen,)) == []
    assert evaluate(task(owner_id="dev-1"), rule=OrphanStatus(review_idle_after=None)) == []


@pytest.mark.parametrize(
    ("owner_id", "role", "owners"),
    [
        ("rev-1", "developer", (DEV, REV)),  # a reviewer has it
        ("dev-1", "reviewer", (DEV, REV)),  # in the reviewer queue
        ("lead", "developer", (Owner("lead", "developer", OwnerKind.HUMAN),)),  # a person
        ("dev-1", "developer", ()),  # no owner directory: cannot tell
    ],
)
def test_review_with_someone_to_drive_it(owner_id: str, role: str, owners: tuple) -> None:
    assert evaluate(task(owner_id=owner_id, role=role), owners=owners) == []


def test_review_with_a_session_or_an_open_condition_is_driven() -> None:
    reviewing = task(owner_id="dev-1")
    assert evaluate(reviewing, sessions=(live("T-1"),)) == []
    ci = cond("ci", ConditionKind.CI_STATUS, "T-1")
    assert evaluate(reviewing, conditions=(ci,)) == []
    # Conditions that cannot move it do not count.
    closed = cond("pr", ConditionKind.PR_MERGED, "T-1", terminal=True)
    unknown = cond("t", ConditionKind.TASK_STATUS, "T-1", None, task="T-9", error="not_found")
    met = cond("ok", ConditionKind.CI_STATUS, "T-1", True)
    assert patterns(evaluate(reviewing, conditions=(closed, unknown, met))) == [
        ("review-undriven", "T-1")
    ]


def test_review_left_by_a_session_without_status_is_rule_1s() -> None:
    silent = ended("T-1", 150, None, start=170)  # a run after the hand-off, no status
    assert evaluate(task(owner_id="dev-1"), sessions=(silent,)) == []


def test_review_alert_is_critical_with_many_dependents() -> None:
    deps = tuple(Dependency(f"D-{i}", "T-1") for i in range(3))
    downstream = [task(f"D-{i}", Status.BACKLOG) for i in range(3)]
    [alert] = evaluate(task(owner_id="dev-1"), *downstream, deps=deps)
    assert alert.severity is Severity.CRITICAL and alert.key.endswith(":critical")


# -- b) a successful run with no next action ---------------------------------------------


@pytest.mark.parametrize("final", [Status.DONE, Status.REVIEW, Status.ACCEPTED])
def test_success_not_applied_alerts(final: Status) -> None:
    t = task(status=Status.IN_PROGRESS, owner_id="dev-1")
    [alert] = evaluate(t, sessions=(ended("T-1", 45, final),))
    assert alert.params["pattern"] == "no-next-action" and alert.params["declared"] == final
    assert alert.since == NOW - 45 * M


def test_success_not_applied_boundaries() -> None:
    t = task(status=Status.IN_PROGRESS, owner_id="dev-1")
    assert evaluate(t, sessions=(ended("T-1", 29, Status.DONE),)) == []
    assert evaluate(t, sessions=(ended("T-1", 45, Status.FAILED),)) == []
    # Moved after the session ended: someone acted on it.
    moved = task(status=Status.IN_PROGRESS, owner_id="dev-1", age=10)
    assert evaluate(moved, sessions=(ended("T-1", 45, Status.DONE),)) == []
    # A new session is on it.
    assert evaluate(t, sessions=(ended("T-1", 45, Status.DONE), live("T-1"))) == []
    # Parked tasks are their conditions' business.
    parked = task(status=Status.BLOCKED, owner_id="dev-1", block=Block("guard", NOW - 300 * M))
    assert evaluate(parked, sessions=(ended("T-1", 45, Status.DONE),)) == []
    off = OrphanStatus(next_action_after=None)
    assert evaluate(t, sessions=(ended("T-1", 45, Status.DONE),), rule=off) == []


def test_silent_session_is_not_a_successful_run() -> None:
    t = task(status=Status.IN_PROGRESS, owner_id="dev-1")
    silent = Session("S-1", "T-1", "dev-1", NOW - 300 * M, last_heartbeat=NOW - 200 * M,
                     final_status=Status.DONE)  # fmt: skip
    assert evaluate(t, sessions=(silent,)) == []


def test_ownerless_task_gets_the_orphan_alert_only() -> None:
    t = task(status=Status.IN_PROGRESS)
    assert patterns(evaluate(t, sessions=(ended("T-1", 45, Status.DONE),))) == [
        ("orphan", "T-1"),
        ("orphan", "T-1"),  # alert and reassign
    ]


def test_dependents_not_woken() -> None:
    a = task("A", Status.DONE, age=60, owner_id="dev-1")
    b = task("B", Status.CANCELLED, age=300)
    c = task("C", Status.BACKLOG, age=600)
    d = task("D", Status.BACKLOG, age=600)
    deps = (Dependency("C", "A"), Dependency("C", "B"), Dependency("D", "A"), Dependency("C", "X"))
    [alert] = evaluate(a, b, c, d, deps=deps)
    assert alert.task_id == "A" and alert.params["dependent_ids"] == ["C", "D"]
    assert alert.params["pattern"] == "dependents-not-woken"


def test_dependents_not_woken_boundaries() -> None:
    done = task("A", Status.DONE, age=20)
    child = task("C", Status.BACKLOG, age=600)
    deps = (Dependency("C", "A"),)
    assert evaluate(done, child, deps=deps) == []  # 20 minutes: not yet
    old = task("A", Status.DONE, age=60)
    assert evaluate(old, task("C", Status.BACKLOG, age=30), deps=deps) == []  # put back
    assert evaluate(old, task("C", Status.READY, age=30), deps=deps) == []  # woken
    assert evaluate(old, child, deps=deps, sessions=(live("C"),)) == []
    cancelled = task("A", Status.CANCELLED, age=60)
    assert evaluate(cancelled, child, deps=deps) == []  # no parent was done
    open_parent = task("B", Status.IN_PROGRESS, age=5, owner_id="dev-1")
    both = (Dependency("C", "A"), Dependency("C", "B"))
    assert evaluate(old, open_parent, child, deps=both) == []
    off = OrphanStatus(next_action_after=None)
    assert evaluate(old, child, deps=deps, rule=off) == []


# -- c) blocker chains -------------------------------------------------------------------


def blocked(tid: str, condition_id: str | None = None, age: int = 180) -> Task:
    return task(tid, Status.BLOCKED, age, block=Block("waiting", NOW - age * M, condition_id))


def test_chain_through_a_missing_condition_and_a_closed_pr() -> None:
    front = blocked("F", "gone")
    pr = blocked("P")
    closed = cond("pr", ConditionKind.PR_MERGED, "P", terminal=True)
    deps = (Dependency("W", "F"), Dependency("V", "P"))
    waiters = (task("W", Status.BACKLOG), task("V", Status.BACKLOG))
    actions = evaluate(front, pr, *waiters, deps=deps, conditions=(closed,))
    by_task = {a.task_id: a for a in actions}
    assert "cannot see" in by_task["F"].reason and by_task["F"].params["dead_end"] == "gone"
    assert "closed without merging" in by_task["P"].reason
    assert by_task["P"].params["chain"] == ["V", "P", "pr"]
    assert evaluate(front, *waiters, deps=deps, rule=OrphanStatus(dead_end_chains=False)) == []


def test_chain_ignores_live_moving_and_unchecked_links() -> None:
    dead = task("X", Status.CANCELLED)
    human = cond("h", ConditionKind.HUMAN_TASK, "F", task="X", terminal=True)
    front = blocked("F", "h")
    deps = (Dependency("W", "F"),)
    waiter = task("W", Status.BACKLOG)
    assert patterns(evaluate(dead, front, waiter, deps=deps, conditions=(human,))) == [
        ("dead-end-chain", "F")
    ]
    # Someone is working on the front task: nothing to report.
    assert evaluate(dead, front, waiter, deps=deps, conditions=(human,),
                    sessions=(live("F"),)) == []  # fmt: skip
    # The terminal flag was seen before the block began: stale, not a dead end yet.
    old = Condition("h", ConditionKind.HUMAN_TASK, "F", {"task": "X", "terminal": True},
                    satisfied=False, checked_at=NOW - 200 * M)  # fmt: skip
    assert evaluate(dead, front, waiter, deps=deps, conditions=(old,)) == []


def test_graph_chains_for_one_task() -> None:
    snap = Snapshot(
        now=NOW,
        tasks=(
            task("A", Status.BACKLOG),
            task("B", Status.WAITING),
            task("C", Status.IN_PROGRESS, owner_id="dev-1"),
            task("D", Status.DONE),
            task("E", Status.BACKLOG),
        ),
        dependencies=(
            Dependency("A", "B"),
            Dependency("A", "C"),
            Dependency("A", "D"),  # finished: satisfied
            Dependency("A", "Z"),  # not on the board: left out
            Dependency("A", "A"),  # self-link: ignored
            Dependency("A", "E"),
            Dependency("E", "A"),  # a cycle
        ),
        conditions=(
            cond("ci", ConditionKind.CI_STATUS, "B"),
            cond("met", ConditionKind.CI_STATUS, "B", True),
            cond("t", ConditionKind.TASK_STATUS, "B", task="Q"),  # unknown, no error
            cond("gone", ConditionKind.TASK_STATUS, "B", None, task="Y", error="not_found"),
        ),
    )
    graph = BlockerGraph(snap)
    chains = graph.chains("A")
    ends = [(c.path, c.end.dead.ref if c.end.dead else c.end.external or c.end.task)
            for c in chains]  # fmt: skip
    assert ends[0] == (("A", "B"), "Y")  # dead ends first
    assert (("A", "C"), "C") in ends  # stops at a moving task
    assert (("A", "B"), "ci") in ends and (("A", "B"), "Q (unknown)") in ends
    assert graph.behind("B") == {"A": ("A", "B"), "E": ("E", "A", "B")}
    assert graph.chains("D") == []


# -- d) recovery loops ---------------------------------------------------------------------


def rec(tid: str, status: Status = Status.BLOCKED, title: str | None = None) -> Task:
    return Task(tid, title or tid, status, NOW - 60 * M, labels=("Recovery",))


def test_recovery_chain_by_dependencies_and_conditions() -> None:
    original = task("T", Status.BLOCKED, block=Block("stuck", NOW - 300 * M))
    deps = (Dependency("T", "R1"), Dependency("R1", "R2"))  # T waits for R1, R1 for R2
    via_condition = cond("w", ConditionKind.TASK_STATUS, "R2", task="R3")
    actions = evaluate(original, rec("R1"), rec("R2"), rec("R3"), deps=deps,
                       conditions=(via_condition,))  # fmt: skip
    [alert] = [a for a in actions if a.params.get("pattern") == "recovery-loop"]
    assert alert.task_id == "R3" and alert.params["chain"] == ["R3", "R2", "R1"]
    assert alert.params["origin"] == "T" and alert.params["cycle"] is False
    assert alert.severity is Severity.WARNING
    assert alert.state == "orphan_status:recovery-loop:T"  # stable as the loop grows


def test_recovery_depth_limit_finished_tops_and_off() -> None:
    deps = (Dependency("T", "R1"), Dependency("R1", "R2"))
    original = task("T", Status.READY, age=10)
    assert evaluate(original, rec("R1"), rec("R2", Status.DONE), deps=deps) == []
    assert evaluate(original, rec("R1"), rec("R2"), deps=deps,
                    rule=OrphanStatus(max_recovery_depth=2)) == []  # fmt: skip
    assert evaluate(original, rec("R1"), rec("R2"), deps=deps,
                    rule=OrphanStatus(max_recovery_depth=None)) == []  # fmt: skip
    assert evaluate(original, rec("R1"), deps=deps) == []  # one recovery: normal


def test_recovery_that_recovers_itself() -> None:
    rule = OrphanStatus(recovery_titles=(re.compile(r"^fix (?P<target>R-\d+)", re.I),
                                         re.compile(r"^unstick\b", re.I)))  # fmt: skip
    me = Task("R-1", "Fix R-1 again", Status.READY, NOW - 60 * M)
    [alert] = evaluate(me, rule=rule)
    assert alert.params["cycle"] is True and alert.params["chain"] == ["R-1", "R-1"]
    # A title pattern without a target group marks a recovery task, with no target.
    lone = Task("R-2", "Unstick the queue", Status.READY, NOW - 60 * M)
    assert evaluate(lone, rule=rule) == []


def test_follow_reports_the_origin() -> None:
    assert _follow("A", {"A": ["B"], "B": []}) == (["A", "B"], "B", False)
    assert _follow("A", {"A": ["X"]}) == (["A"], "X", False)
    assert _follow("B", {"A": ["B"], "B": ["A"]}) == (["B", "A", "B"], "A", True)


# -- config ------------------------------------------------------------------------------


def test_build_stop_pattern_config() -> None:
    rule = build(
        {
            "review_idle_after": "1h",
            "next_action_after": False,
            "reviewer_roles": ["reviewer", "qa"],
            "dead_end_chains": False,
            "max_recovery_depth": 2,
            "recovery_labels": ["recovery", "fixup"],
            "recovery_titles": ["^recover (?P<target>T-\\d+)"],
        }
    )
    assert rule.review_idle_after == 60 * M and rule.next_action_after is None
    assert rule.reviewer_roles == ("reviewer", "qa") and not rule.dead_end_chains
    assert rule.max_recovery_depth == 2 and rule.recovery_labels == ("recovery", "fixup")
    assert rule.recovery_titles[0].search("RECOVER T-1")
    assert build({"max_recovery_depth": False}).max_recovery_depth is None
    assert build({"review_idle_after": False}).review_idle_after is None


@pytest.mark.parametrize(
    "config",
    [
        {"review_idle_after": "0s"},
        {"next_action_after": True},
        {"reviewer_roles": "reviewer"},
        {"reviewer_roles": [""]},
        {"recovery_labels": [1]},
        {"recovery_titles": ["("]},
        {"max_recovery_depth": 0},
        {"max_recovery_depth": True},
        {"dead_end_chains": "yes"},
    ],
)
def test_bad_stop_pattern_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)
