"""Edge cases of rule 2 (wait_condition) that pin boundaries, loop scans and arguments."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import (
    Condition,
    ConditionKind,
    Dependency,
    HostSleep,
    Owner,
    OwnerKind,
    Session,
    SessionWait,
    Snapshot,
    Snooze,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.rules.needs_you import NeedsYou
from kelpie.rules.wait_condition import WaitCondition, build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
S = timedelta(seconds=1)
H = timedelta(hours=1)
CI = ConditionKind.CI_STATUS
NAME = "wait_condition"
PATTERNS = {"ci_status": ("gh run watch",), "pr_merged": ("gh pr view",)}


def waiting(tid: str = "T-101", since: datetime = NOW - 60 * M) -> Task:
    return Task(tid, "t", Status.WAITING, since, owner_id="agent-dev-1")


def ci(cid: str, satisfied: bool | None, checked: datetime | None = NOW - M) -> Condition:
    return Condition(cid, CI, task_id="T-101", satisfied=satisfied, checked_at=checked)


def session(
    sid: str = "S-1",
    task_id: str = "T-102",
    heartbeat: datetime = NOW - M,
    source: str | None = None,
) -> Session:
    return Session(
        sid, task_id, "agent-dev-1", NOW - 120 * M, last_heartbeat=heartbeat, source=source
    )


def tool(
    n: int,
    ago: timedelta,
    command: str,
    args: str | None = None,
    outcome: str = "ok",
    sid: str = "S-1",
    open_: bool = False,
) -> Span:
    start = NOW - ago
    return Span(
        "tr", f"sp{sid}{n}", "execute_tool bash", SpanKind.TOOL, start,
        end=None if open_ else start + 2 * S, session_id=sid, tool="bash", args_hash=args,
        outcome=outcome, attributes={"command": command},
    )  # fmt: skip


def comments(rule: WaitCondition, snap: Snapshot) -> list[Action]:
    return [a for a in rule.evaluate(snap) if a.kind is ActionKind.COMMENT]


# --- waiting tasks: the alerts' identity --------------------------------------------------


def test_no_condition_alert_carries_its_key_state_since_and_who_is_stuck() -> None:
    task = waiting()
    behind = Task("T-102", "t", Status.BLOCKED, NOW - 30 * M)
    snap = Snapshot(now=NOW, tasks=(task, behind), dependencies=(Dependency("T-102", "T-101"),))
    (alert,) = WaitCondition().evaluate(snap)
    since = task.status_since.isoformat()
    assert alert.key == f"no-condition:T-101:{since}"
    assert alert.state == f"{NAME}:T-101:{since}"
    assert alert.since == task.status_since
    assert alert.severity is Severity.WARNING
    assert alert.params["stuck_ids"] == ["T-102"]
    assert alert.reason.endswith(" 1 task(s) wait behind it: T-102.")


def test_stuck_tasks_use_the_rules_own_session_stale_after() -> None:
    """A task behind the wait whose session is live under ``session_stale_after`` is moving,
    not stuck: the graph reads liveness with the rule's setting, not the default."""
    behind = Task("T-102", "t", Status.BLOCKED, NOW - 30 * M)
    deps = (Dependency("T-102", "T-101"),)
    rule = WaitCondition(session_stale_after=60 * M)
    seen_40m = session(task_id="T-102", heartbeat=NOW - 40 * M)
    snap = Snapshot(now=NOW, tasks=(waiting(), behind), dependencies=deps, sessions=(seen_40m,))
    (alert,) = rule.evaluate(snap)
    assert alert.params["stuck_ids"] == []  # live for 60m: working, not stuck
    seen_70m = session(task_id="T-102", heartbeat=NOW - 70 * M)
    snap = Snapshot(now=NOW, tasks=(waiting(), behind), dependencies=deps, sessions=(seen_70m,))
    (alert,) = rule.evaluate(snap)
    assert alert.params["stuck_ids"] == ["T-102"]


def test_dead_end_alert_carries_its_key_state_and_since() -> None:
    task = waiting()
    dead = Condition(
        "C-1", CI, task_id="T-101", params={"terminal": True}, satisfied=False, checked_at=NOW - M
    )
    (alert,) = WaitCondition().evaluate(Snapshot(now=NOW, tasks=(task,), conditions=(dead,)))
    since = task.status_since.isoformat()
    assert alert.key == f"dead-end:T-101:{since}"
    assert alert.state == f"{NAME}:T-101:{since}"
    assert alert.since == task.status_since
    assert alert.severity is Severity.WARNING


def test_wake_is_this_rules_action_with_a_reason() -> None:
    snap = Snapshot(now=NOW, tasks=(waiting(),), conditions=(ci("C-1", True),))
    (wake,) = WaitCondition().evaluate(snap)
    assert wake.kind is ActionKind.WAKE and wake.rule == NAME
    assert wake.reason.startswith("Every condition the task was waiting for now holds: C-1")


def test_wake_reads_freshness_with_the_rules_max_condition_age() -> None:
    rule = WaitCondition(max_condition_age=60 * M)
    snap = Snapshot(now=NOW, tasks=(waiting(),), conditions=(ci("C-1", True, NOW - 30 * M),))
    assert [a.kind for a in rule.evaluate(snap)] == [ActionKind.WAKE]


def test_overdue_alert_carries_since_and_severity() -> None:
    task = waiting(since=NOW - 30 * H)
    snap = Snapshot(now=NOW, tasks=(task,), conditions=(ci("C-1", False),))
    (alert,) = WaitCondition().evaluate(snap)
    assert alert.key == f"overdue:T-101:{task.status_since.isoformat()}"
    assert alert.since == task.status_since
    assert alert.severity is Severity.WARNING


def human(cid: str, kind: ConditionKind, checked: datetime) -> Condition:
    return Condition(
        cid, kind, task_id="T-101", params={"task": "T-201"}, satisfied=False, checked_at=checked
    )


def test_known_unmet_reads_the_rules_max_condition_age() -> None:
    """A human task checked 30m ago is known unmet under a 60m ``max_condition_age``: the
    overdue alert offers to snooze it."""
    rule = WaitCondition(max_condition_age=60 * M)
    cond = human("C-1", ConditionKind.HUMAN_TASK, NOW - 30 * M)
    snap = Snapshot(now=NOW, tasks=(waiting(since=NOW - 30 * H),), conditions=(cond,))
    (alert,) = rule.evaluate(snap)
    assert alert.params.get("snooze_task") == "T-201"


def test_a_snoozed_task_held_by_a_non_human_condition_still_alerts() -> None:
    """Only ``human_task`` conditions are a person's deferral: a ``task_status`` condition
    on a snoozed task keeps the overdue reminder."""
    target = Task("T-201", "sign", Status.READY, NOW - 40 * H, owner_id="person-1")
    snooze = Snooze("T-201", NOW + 7 * 24 * H, "later", "person-1", NOW - 24 * H)
    snoozes = MappingProxyType({"T-201": snooze})
    tasks = (target, waiting(since=NOW - 30 * H))

    def snap(cond: Condition) -> Snapshot:
        return Snapshot(now=NOW, tasks=tasks, conditions=(cond,), snoozes=snoozes)

    person = human("C-1", ConditionKind.HUMAN_TASK, NOW - M)
    assert WaitCondition().evaluate(snap(person)) == []  # held: the person deferred it
    status = human("C-1", ConditionKind.TASK_STATUS, NOW - M)
    assert len(WaitCondition().evaluate(snap(status))) == 1


# --- resolved -----------------------------------------------------------------------------


OWNERS = (Owner("person-1", "owner", OwnerKind.HUMAN), Owner("agent-dev-1", "dev"))
INBOX = NeedsYou(default_approver="person-1")


def handed_off(*extra: Task, sessions: tuple[Session, ...] = ()) -> tuple[str, Snapshot]:
    target = Task("T-201", "sign", Status.READY, NOW - 40 * H, role="owner", owner_id="person-1")
    wait = Task("T-101", "w", Status.WAITING, NOW - 30 * H, role="dev", owner_id="agent-dev-1")
    cond = Condition(
        "C-1", ConditionKind.HUMAN_TASK, "T-101", {"task": "T-201"}, False, checked_at=NOW
    )
    snap = Snapshot(
        NOW, (*extra, target, wait), OWNERS, conditions=(cond,), sessions=sessions
    )  # fmt: skip
    return f"{NAME}:T-101:{wait.status_since.isoformat()}", snap


def test_resolved_finds_the_task_the_state_names_among_other_waits() -> None:
    other = Task("T-100", "o", Status.WAITING, NOW - 30 * H, role="dev", owner_id="agent-dev-1")
    state, snap = handed_off(other)
    notice = WaitCondition(inbox=INBOX).resolved(state, snap)
    assert notice is not None and notice.startswith("Task T-101 is in the person's needs-you")


def test_resolved_reads_liveness_with_the_rules_setting() -> None:
    live = session(task_id="T-300", heartbeat=NOW - M)
    state, snap = handed_off(sessions=(live,))
    notice = WaitCondition(inbox=INBOX).resolved(state, snap)
    assert notice is not None and "needs-you" in notice


# --- polling inside a session ---------------------------------------------------------------


def test_a_session_on_a_waiting_task_does_not_end_the_scan() -> None:
    first = session("S-0", task_id="T-101")
    second = session("S-1", task_id="T-102")
    watch = tool(1, 40 * M, "gh run watch 12", open_=True)
    snap = Snapshot(
        now=NOW, tasks=(waiting(),), conditions=(ci("C-1", False),), sessions=(first, second),
        spans=(watch,),
    )  # fmt: skip
    (comment,) = comments(WaitCondition(poll_patterns=PATTERNS), snap)
    assert comment.key == "poll:S-1" and comment.rule == NAME


def test_a_session_whose_host_slept_is_still_live() -> None:
    """A laptop session silent for 60m, 50m of which its host slept, is live (10m awake);
    a sleep of the server's own host (agent None) does not count for it."""
    laptop = session(heartbeat=NOW - 60 * M, source="laptop/hermes")
    watch = tool(1, 65 * M, "gh run watch 12", open_=True)
    rule = WaitCondition(poll_patterns=PATTERNS)

    def snap(agent: str | None) -> Snapshot:
        slept = HostSleep(NOW - 58 * M, NOW - 8 * M, agent=agent)
        return Snapshot(now=NOW, sessions=(laptop,), spans=(watch,), host_sleeps=(slept,))

    assert [a.key for a in comments(rule, snap("laptop"))] == ["poll:S-1"]
    assert comments(rule, snap(None)) == []


def test_repeated_check_picks_the_largest_group_and_the_latest_calls_kind() -> None:
    rule = WaitCondition(poll_min_repeats=8, poll_patterns=PATTERNS)
    small = [tool(i, (9 - i) * M, "gh pr view 7", args="h-a") for i in range(2)]
    # One check (same arguments), its command rewritten on the last call: the latest wins.
    big = [tool(10 + i, (8 - i) * 50 * S, "gh pr view 7", args="h-b") for i in range(7)]
    big.append(tool(20, 10 * S, "gh run watch 7", args="h-b"))
    snap = Snapshot(now=NOW, sessions=(session(),), spans=(*small, *big))
    (comment,) = comments(rule, snap)
    assert comment.params["condition_kind"] == "ci_status"
    assert "ran the same check 8 times" in comment.reason


def test_failing_check_kind_is_the_last_errors() -> None:
    rule = WaitCondition(poll_min_repeats=50, poll_error_streak=5, poll_patterns=PATTERNS)
    errors = [tool(i, (6 - i) * M, "gh pr view 7", args="h-c", outcome="error") for i in range(4)]
    errors.append(tool(9, 30 * S, "gh run watch 7", args="h-c", outcome="error"))
    snap = Snapshot(now=NOW, sessions=(session(),), spans=tuple(errors))
    (comment,) = comments(rule, snap)
    assert comment.params["condition_kind"] == "ci_status"


def test_failing_check_whose_last_call_is_no_known_check_is_silent() -> None:
    rule = WaitCondition(poll_min_repeats=50, poll_error_streak=5, poll_patterns=PATTERNS)
    errors = [tool(i, (6 - i) * M, "gh pr view 7", args="h-c", outcome="error") for i in range(4)]
    errors.append(tool(9, 30 * S, "make test", args="h-c", outcome="error"))
    snap = Snapshot(now=NOW, sessions=(session(),), spans=tuple(errors))
    assert comments(rule, snap) == []


def test_blocking_check_scan_skips_other_spans() -> None:
    rule = WaitCondition(poll_patterns=PATTERNS)
    llm = Span("tr", "l", "chat", SpanKind.LLM, NOW - 50 * M, session_id="S-1")
    done = tool(2, 45 * M, "gh run watch 11")
    watch = tool(3, 40 * M, "gh run watch 12", open_=True)
    snap = Snapshot(now=NOW, sessions=(session(),), spans=(llm, done, watch))
    assert [a.key for a in comments(rule, snap)] == ["poll:S-1"]


def test_blocking_check_for_exactly_one_window_counts() -> None:
    rule = WaitCondition(poll_window=10 * M, poll_patterns=PATTERNS)
    watch = tool(1, 10 * M, "gh run watch 12", open_=True)
    snap = Snapshot(now=NOW, sessions=(session(),), spans=(watch,))
    assert len(comments(rule, snap)) == 1


# --- waits inside sessions ------------------------------------------------------------------


def sw(sid: str, at: datetime, waited: timedelta | None) -> SessionWait:
    return SessionWait(sid, at, "sleep", waited=waited, task_id="T-101", role="reviewer")


def wait_actions(*waits: SessionWait, rule: WaitCondition | None = None) -> list[Action]:
    task = Task("T-101", "t", Status.BLOCKED, NOW - 5 * M)
    return list((rule or WaitCondition()).evaluate(Snapshot(now=NOW, tasks=(task,), waits=waits)))


def test_a_wait_of_exactly_min_wait_counts() -> None:
    (act,) = wait_actions(*(sw("S-1", NOW - m * M, 30 * S) for m in (30, 20, 10)))
    assert act.kind is ActionKind.COMMENT and act.rule == NAME and act.params["waits"] == 3


def test_a_short_wait_does_not_end_the_scan() -> None:
    short = sw("S-1", NOW - 40 * M, 5 * S)
    (act,) = wait_actions(short, *(sw("S-1", NOW - m * M, M) for m in (30, 20, 10)))
    assert act.params["waits"] == 3


def test_a_wait_recorded_now_counts() -> None:
    (act,) = wait_actions(sw("S-1", NOW - 20 * M, M), sw("S-1", NOW - 10 * M, M), sw("S-1", NOW, M))
    assert act.params["waits"] == 3


def test_a_session_under_the_threshold_does_not_end_the_scan() -> None:
    few = sw("S-1", NOW - 40 * M, M)
    many = [sw("S-2", NOW - m * M, M) for m in (30, 20, 10)]
    (act,) = wait_actions(few, *many)
    assert act.key == "poll:S-2"


# --- config ---------------------------------------------------------------------------------


def test_one_is_a_positive_count() -> None:
    rule = build({"poll_min_repeats": 1, "poll_error_streak": 1})
    assert (rule.poll_min_repeats, rule.poll_error_streak) == (1, 1)
