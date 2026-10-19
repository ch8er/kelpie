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
from kelpie.rules._chains import BlockerGraph, behind_text
from kelpie.rules.orphan_status import OrphanStatus, _cycle, _follow, build
from kelpie.rules.stale_blocks import StaleBlocks
from kelpie.rules.wait_condition import WaitCondition

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
M = timedelta(minutes=1)
DEV = Owner("dev-1", "developer")
REV = Owner("rev-1", "reviewer")
# The opt-in patterns switched on; tests of the defaults pass their own rule.
RULE = OrphanStatus(reviewer_roles=("reviewer",), dependents_not_woken=True)


def task(tid: str = "T-1", status: Status = Status.REVIEW, age: int = 180, **kw: object) -> Task:
    kw.setdefault("role", "developer")
    return Task(tid, tid, status, NOW - age * M, **kw)  # type: ignore[arg-type]


def snap(
    *tasks: Task,
    deps: tuple[Dependency, ...] = (),
    owners: tuple[Owner, ...] = (DEV, REV),
    sessions: tuple[Session, ...] = (),
    conditions: tuple[Condition, ...] = (),
) -> Snapshot:
    return Snapshot(
        now=NOW,
        tasks=tasks,
        owners=owners,
        dependencies=deps,
        sessions=sessions,
        conditions=conditions,
    )


def evaluate(*tasks: Task, rule: OrphanStatus = RULE, **kw: object) -> list:
    return list(rule.evaluate(snap(*tasks, **kw)))  # type: ignore[arg-type]


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


# -- defaults and holds --------------------------------------------------------------------


def test_opt_in_patterns_are_off_by_default() -> None:
    default = OrphanStatus()
    assert evaluate(task(owner_id="dev-1"), rule=default) == []  # no reviewer_roles
    deps = (Dependency("C", "A"),)
    parked = (task("A", Status.DONE, age=60), task("C", Status.BACKLOG, age=600))
    assert evaluate(*parked, deps=deps, rule=default) == []  # dependents_not_woken off


def test_held_and_ignored_tasks_are_skipped() -> None:
    held = task(owner_id="dev-1", labels=("merge-first",))
    rule = OrphanStatus(reviewer_roles=("reviewer",), hold_labels=frozenset({"merge-first"}))
    assert evaluate(held, rule=rule) == []
    ignoring = OrphanStatus(reviewer_roles=("reviewer",), ignore_tasks=("T-*",))
    assert evaluate(task(owner_id="dev-1"), rule=ignoring) == []
    assert evaluate(task(status=Status.IN_PROGRESS), rule=ignoring) == []  # ownerless too
    assert patterns(evaluate(task(owner_id="dev-1"), rule=rule)) == [("review-undriven", "T-1")]


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
    off = OrphanStatus(reviewer_roles=("reviewer",), review_idle_after=None)
    assert evaluate(task(owner_id="dev-1"), rule=off) == []


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


@pytest.mark.parametrize(
    ("status", "final"),
    [
        (Status.REVIEW, Status.DONE),
        (Status.REVIEW, Status.ACCEPTED),
        (Status.ACCEPTED, Status.DONE),
        (Status.READY, Status.REVIEW),
    ],
)
def test_success_not_applied_alerts(status: Status, final: Status) -> None:
    t = task(status=status, owner_id="rev-1", age=200)
    [alert] = evaluate(t, sessions=(ended("T-1", 45, final),))
    assert alert.params["pattern"] == "no-next-action" and alert.params["declared"] == final
    assert alert.since == NOW - 45 * M


def test_success_not_applied_boundaries() -> None:
    t = task(status=Status.ACCEPTED, owner_id="rev-1", age=200)
    assert evaluate(t, sessions=(ended("T-1", 29, Status.DONE),)) == []
    assert evaluate(t, sessions=(ended("T-1", 45, Status.FAILED),)) == []
    # Moved after the session ended: someone acted on it.
    moved = task(status=Status.ACCEPTED, owner_id="rev-1", age=10)
    assert evaluate(moved, sessions=(ended("T-1", 45, Status.DONE),)) == []
    # A new session is on it.
    assert evaluate(t, sessions=(ended("T-1", 45, Status.DONE), live("T-1"))) == []
    # In progress: rule 1 reports the unapplied status. Parked: their conditions' business.
    running = task(status=Status.IN_PROGRESS, owner_id="dev-1")
    assert evaluate(running, sessions=(ended("T-1", 45, Status.DONE),)) == []
    parked = task(status=Status.BLOCKED, owner_id="dev-1", block=Block("guard", NOW - 300 * M))
    assert evaluate(parked, sessions=(ended("T-1", 45, Status.DONE),)) == []
    off = OrphanStatus(next_action_after=None)
    assert evaluate(t, sessions=(ended("T-1", 45, Status.DONE),), rule=off) == []


def test_silent_session_is_not_a_successful_run() -> None:
    t = task(status=Status.ACCEPTED, owner_id="rev-1")
    silent = Session("S-1", "T-1", "dev-1", NOW - 300 * M, last_heartbeat=NOW - 200 * M,
                     final_status=Status.DONE)  # fmt: skip
    assert evaluate(t, sessions=(silent,)) == []


def test_ownerless_task_gets_the_orphan_alert_only() -> None:
    t = task(status=Status.REVIEW)
    assert patterns(evaluate(t, sessions=(ended("T-1", 45, Status.DONE),))) == [
        ("orphan", "T-1"),
        ("orphan", "T-1"),  # alert and reassign
    ]


def test_dependents_not_woken() -> None:
    a = task("A", Status.DONE, age=60, owner_id="dev-1")
    b = task("B", Status.CANCELLED, age=300)
    c = task("C", Status.BACKLOG, age=600)
    d = task("D", Status.BACKLOG, age=600)
    held = task("H", Status.BACKLOG, age=600, labels=("hold",))
    deps = (
        Dependency("C", "A"),
        Dependency("C", "B"),
        Dependency("D", "A"),
        Dependency("C", "X"),
        Dependency("H", "A"),
    )
    rule = OrphanStatus(dependents_not_woken=True, hold_labels=frozenset({"hold"}))
    [alert] = evaluate(a, b, c, d, held, deps=deps, rule=rule)
    assert alert.task_id == "A" and alert.params["dependent_ids"] == ["C", "D"]
    assert alert.params["pattern"] == "dependents-not-woken"
    held_parent = task("A", Status.DONE, age=60, labels=("hold",))
    assert evaluate(held_parent, c, deps=deps[:1], rule=rule) == []


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
    off = OrphanStatus(dependents_not_woken=True, next_action_after=None)
    assert evaluate(old, child, deps=deps, rule=off) == []


# -- c) blocker chains -------------------------------------------------------------------


def blocked(tid: str, condition_id: str | None = None, age: int = 180) -> Task:
    return task(tid, Status.BLOCKED, age, block=Block("waiting", NOW - age * M, condition_id))


def gone_wait(tid: str, target: str = "Z") -> Condition:
    return cond(f"w-{tid}", ConditionKind.TASK_STATUS, tid, None, task=target, error="not_found")


def test_chain_to_a_task_off_the_board() -> None:
    front = task("F", Status.WAITING)
    deps = (Dependency("W", "F"), Dependency("V", "W"))
    waiters = (task("W", Status.BACKLOG), task("V", Status.READY))
    [alert] = evaluate(front, *waiters, deps=deps, conditions=(gone_wait("F"),))
    assert alert.params["pattern"] == "dead-end-chain" and alert.params["dead_end"] == "Z"
    assert alert.params["chain"] == ["V", "W", "F", "Z"] and alert.params["stuck_ids"] == ["V", "W"]
    assert "is not on the board" in alert.reason
    off = OrphanStatus(dead_end_chains=False)
    assert evaluate(front, *waiters, deps=deps, conditions=(gone_wait("F"),), rule=off) == []
    # Held tasks behind it do not count; a held front is parked on purpose.
    holds = OrphanStatus(hold_labels=frozenset({"hold"}))
    held_waiter = task("W", Status.BACKLOG, labels=("hold",))
    assert evaluate(front, held_waiter, deps=deps[:1], conditions=(gone_wait("F"),),
                    rule=holds) == []  # fmt: skip
    held_front = task("F", Status.WAITING, labels=("hold",))
    assert evaluate(held_front, *waiters, deps=deps, conditions=(gone_wait("F"),),
                    rule=holds) == []  # fmt: skip


def test_dead_ends_rules_2_and_4_report_are_left_to_them() -> None:
    dead = task("X", Status.CANCELLED)
    human = cond("h", ConditionKind.HUMAN_TASK, "F", task="X", terminal=True)
    front = blocked("F", "h")
    missing = blocked("G", "nowhere")
    deps = (Dependency("W", "F"), Dependency("V", "G"))
    waiters = (task("W", Status.BACKLOG), task("V", Status.BACKLOG))
    snapshot = snap(dead, front, missing, *waiters, deps=deps, conditions=(human,))
    assert list(RULE.evaluate(snapshot)) == []
    # Rule 4 alerts on both fronts and lists who waits behind them.
    alerts = {a.task_id: a for a in StaleBlocks(unlinked_alert_after=M).evaluate(snapshot)}
    assert alerts["F"].params["stuck_ids"] == ["W"] and "1 task(s) wait behind it: W" in (
        alerts["F"].reason
    )
    assert alerts["G"].params["stuck_ids"] == ["V"]


def test_rule_2_lists_the_stuck_tasks() -> None:
    dead = task("X", Status.CANCELLED)
    front = task("F", Status.WAITING)
    human = cond("h", ConditionKind.HUMAN_TASK, "F", task="X", terminal=True)
    deps = (Dependency("W", "F"),)
    snapshot = snap(dead, front, task("W", Status.BACKLOG), deps=deps, conditions=(human,))
    [alert] = WaitCondition().evaluate(snapshot)
    assert alert.params["stuck_ids"] == ["W"] and "wait behind it" in alert.reason
    nothing = snap(task("F", Status.WAITING), task("W", Status.BACKLOG), deps=deps)
    [alert] = WaitCondition().evaluate(nothing)
    assert alert.params == {
        "stuck_ids": ["W"],
        "headline": "Nothing will wake F: no condition registered",
        "needed": "Register a condition for the task, or move it on",
        "blocking": 1,
    }


def test_chain_ignores_live_and_unchecked_links() -> None:
    front = task("F", Status.WAITING)
    deps = (Dependency("W", "F"),)
    waiter = task("W", Status.BACKLOG)
    # Someone is working on the front task: nothing to report.
    assert evaluate(front, waiter, deps=deps, conditions=(gone_wait("F"),),
                    sessions=(live("F"),)) == []  # fmt: skip
    # The missing-task flag was seen before the wait began: not evidence yet.
    old = Condition("w", ConditionKind.TASK_STATUS, "F", {"task": "Z", "error": "not_found"},
                    checked_at=NOW - 200 * M)  # fmt: skip
    assert evaluate(front, waiter, deps=deps, conditions=(old,)) == []


def test_graph_chains_for_one_task() -> None:
    snapshot = snap(
        task("A", Status.BACKLOG),
        task("B", Status.WAITING),
        task("C", Status.IN_PROGRESS, owner_id="dev-1"),
        task("D", Status.DONE),
        task("E", Status.BACKLOG),
        deps=(
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
    graph = BlockerGraph(snapshot)
    chains = graph.chains("A")
    ends = [(c.path, c.end.dead.ref if c.end.dead else c.end.external or c.end.task)
            for c in chains]  # fmt: skip
    assert ends[0] == (("A", "B"), "Y")  # dead ends first
    assert (("A", "C"), "C") in ends  # stops at a moving task
    assert (("A", "B"), "ci") in ends and (("A", "B"), "Q (unknown)") in ends
    assert graph.behind("B") == {"A": ("A", "B"), "E": ("E", "A", "B")}
    assert graph.chains("D") == []


def test_graph_chain_ends_at_a_parked_task_with_nothing_to_check() -> None:
    snapshot = snap(task("A", Status.BACKLOG), blocked("B"), deps=(Dependency("A", "B"),))
    [chain] = BlockerGraph(snapshot).chains("A")
    assert chain.path == ("A", "B") and chain.end.via == "is blocked with nothing Kelpie can check"
    assert chain.end.task is None and chain.end.dead is None and chain.end.external is None


def test_behind_text() -> None:
    assert behind_text([]) == ""
    many = [f"T-{i}" for i in range(7)]
    assert behind_text(many).endswith("T-4 and 2 more.")


# -- d) recovery loops ---------------------------------------------------------------------

TITLES = OrphanStatus(recovery_titles=(re.compile(r"^recover (?P<target>\S+)", re.I),))


def rec(tid: str, status: Status = Status.BLOCKED, target: str | None = None) -> Task:
    title = f"Recover {target}" if target else tid
    return Task(tid, title, status, NOW - 60 * M, labels=("Recovery",))


def test_recovery_chain_by_title_targets() -> None:
    original = task("T", Status.BLOCKED, block=Block("stuck", NOW - 300 * M))
    tasks = (original, rec("R1", Status.FAILED, "T"), rec("R2", target="R1"),
             rec("R3", target="R2"))  # fmt: skip
    [alert] = evaluate(*tasks, rule=TITLES)
    assert alert.task_id == "R3" and alert.params["chain"] == ["R3", "R2", "R1"]
    assert alert.params["origin"] == "T" and alert.params["cycle"] is False
    assert alert.severity is Severity.WARNING
    assert alert.state == "orphan_status:recovery-loop:T"  # stable as the loop grows


def test_a_staged_plan_of_recovery_tasks_is_not_a_loop() -> None:
    # T waits for R1, R1 for R2, R2 for R3: steps of one plan, all labelled recovery.
    deps = (Dependency("T", "R1"), Dependency("R1", "R2"))
    via_condition = cond("w", ConditionKind.TASK_STATUS, "R2", task="R3")
    original = task("T", Status.BLOCKED, block=Block("stuck", NOW - 300 * M))
    tasks = (original, rec("R1"), rec("R2"), rec("R3"))
    assert evaluate(*tasks, deps=deps, conditions=(via_condition,)) == []
    # ... unless it cycles.
    cycle = (*deps, Dependency("R2", "R1"))
    [alert] = evaluate(*tasks, deps=cycle, conditions=(via_condition,))
    assert alert.params["cycle"] is True and alert.params["chain"] == ["R1", "R2", "R1"]


def test_recovery_depth_limit_done_targets_and_off() -> None:
    original = task("T", Status.READY, age=10)
    two = (original, rec("R1", target="T"), rec("R2", target="R1"))
    assert evaluate(*two, rule=OrphanStatus(recovery_titles=TITLES.recovery_titles,
                                            max_recovery_depth=2)) == []  # fmt: skip
    off = OrphanStatus(recovery_titles=TITLES.recovery_titles, max_recovery_depth=None)
    assert evaluate(*two, rule=off) == []
    # R1 finished done: it recovered T, so R2 recovering it is no loop.
    assert evaluate(original, rec("R1", Status.DONE, "T"), rec("R2", target="R1"),
                    rule=TITLES) == []  # fmt: skip
    assert evaluate(original, rec("R1", target="T"), rule=TITLES) == []  # one: normal
    assert patterns(evaluate(*two, rule=TITLES)) == [("recovery-loop", "R2")]


def test_recovery_that_recovers_itself() -> None:
    rule = OrphanStatus(recovery_titles=(re.compile(r"^fix (?P<target>R-\d+)", re.I),
                                         re.compile(r"^unstick\b", re.I)))  # fmt: skip
    me = Task("R-1", "Fix R-1 again", Status.READY, NOW - 60 * M)
    [alert] = evaluate(me, rule=rule)
    assert alert.params["cycle"] is True and alert.params["chain"] == ["R-1", "R-1"]
    # A title pattern without a target group marks a recovery task, with no target.
    lone = Task("R-2", "Unstick the queue", Status.READY, NOW - 60 * M)
    assert evaluate(lone, rule=rule) == []


def test_follow_and_cycle() -> None:
    assert _follow("A", {"A": ["B"], "B": []}) == ["A", "B"]
    assert _follow("A", {"A": ["B"], "B": ["A"]}) == ["A", "B"]
    assert _cycle("B", {"A": ["B"], "B": ["A"]}) == ["B", "A", "B"]
    assert _cycle("A", {"A": ["B"], "B": []}) is None


# -- config ------------------------------------------------------------------------------


def test_build_stop_pattern_config() -> None:
    rule = build(
        {
            "review_idle_after": "1h",
            "next_action_after": False,
            "reviewer_roles": ["reviewer", "qa"],
            "dead_end_chains": False,
            "dependents_not_woken": True,
            "max_recovery_depth": 2,
            "recovery_labels": ["recovery", "fixup"],
            "recovery_titles": ["^recover (?P<target>T-\\d+)"],
            "hold_labels": ["merge-hold"],
            "ignore_tasks": ["T-9*"],
        }
    )
    assert rule.review_idle_after == 60 * M and rule.next_action_after is None
    assert rule.reviewer_roles == ("reviewer", "qa") and not rule.dead_end_chains
    assert rule.max_recovery_depth == 2 and rule.recovery_labels == ("recovery", "fixup")
    assert rule.recovery_titles[0].search("RECOVER T-1") and rule.dependents_not_woken
    assert rule.hold_labels == frozenset({"merge-hold"}) and rule.ignore_tasks == ("T-9*",)
    assert build({"max_recovery_depth": False}).max_recovery_depth is None
    assert build({"review_idle_after": False}).review_idle_after is None
    assert build({}).reviewer_roles == () and not build({}).dependents_not_woken


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
        {"dependents_not_woken": 1},
        {"hold_labels": "hold"},
        {"ignore_tasks": [None]},
    ],
)
def test_bad_stop_pattern_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)
