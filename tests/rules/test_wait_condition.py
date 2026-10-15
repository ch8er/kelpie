from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules._calls import call_key, error_streak, identical_streak, tool_calls
from kelpie.rules.wait_condition import WaitCondition, build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
CI = ConditionKind.CI_STATUS


def test_registered() -> None:
    assert isinstance(REGISTRY["wait_condition"]({}), WaitCondition)


# --- waiting tasks ----------------------------------------------------------------------


def waiting(since: datetime = NOW - 60 * M, block: Block | None = None) -> Task:
    return Task("T-1", "t", Status.WAITING, since, block=block)


def cond(
    cid: str,
    satisfied: bool | None,
    task_id: str | None = "T-1",
    checked: datetime | None = NOW - M,
) -> Condition:
    return Condition(cid, CI, task_id=task_id, satisfied=satisfied, checked_at=checked)


def test_wake_needs_fresh_evidence() -> None:
    rule = WaitCondition(max_condition_age=15 * M)
    assert kinds(rule, waiting(), cond("a", True, checked=None)) == []
    assert kinds(rule, waiting(NOW - 60 * M), cond("a", True, checked=NOW - 61 * M)) == []
    assert kinds(rule, waiting(), cond("a", True, checked=NOW - 16 * M)) == []
    assert kinds(rule, waiting(), cond("a", True, checked=NOW - 15 * M)) == [ActionKind.WAKE]


def test_no_polling_comment_for_a_task_already_waiting() -> None:
    rule = WaitCondition(poll_min_repeats=2, poll_patterns={"ci_status": ("gh pr checks",)})
    spans = tuple(
        Span("tr", f"s{i}", "x", SpanKind.TOOL, NOW - (5 - i) * M, end=NOW - (5 - i) * M,
             session_id="S-1", tool="bash", attributes={"command": "gh pr checks 1"})
        for i in range(3)
    )  # fmt: skip
    live = Session("S-1", "T-1", "a", NOW - 10 * M, last_heartbeat=NOW - M)
    snap = Snapshot(now=NOW, tasks=(waiting(),), sessions=(live,), spans=spans)
    assert [a.kind for a in rule.evaluate(snap)] == [ActionKind.ALERT]  # waiting on nothing
    stale = Session("S-1", "T-9", "a", NOW - 300 * M, last_heartbeat=NOW - 200 * M)
    assert rule.evaluate(Snapshot(now=NOW, sessions=(stale,), spans=spans)) == []


def kinds(rule: WaitCondition, *items: object) -> list[str]:
    tasks = tuple(i for i in items if isinstance(i, Task))
    conditions = tuple(i for i in items if isinstance(i, Condition))
    return [a.kind for a in rule.evaluate(Snapshot(now=NOW, tasks=tasks, conditions=conditions))]


def test_wakes_only_when_every_condition_holds() -> None:
    rule = WaitCondition()
    assert kinds(rule, waiting(), cond("a", True), cond("b", True)) == [ActionKind.WAKE]
    assert kinds(rule, waiting(), cond("a", True), cond("b", False)) == []
    assert kinds(rule, waiting(), cond("a", True), cond("b", None)) == []  # not checked yet


def test_block_condition_counts_even_without_task_link() -> None:
    task = waiting(block=Block("waiting_ci", NOW - 60 * M, "c"))
    assert kinds(WaitCondition(), task, cond("c", True, task_id=None)) == [ActionKind.WAKE]


def test_condition_from_an_earlier_wait_does_not_wake() -> None:
    old = Condition("old", CI, task_id="T-1", satisfied=True, registered_at=NOW - 300 * M)
    new = Condition("new", CI, task_id="T-1", satisfied=False, registered_at=NOW - 30 * M)
    assert kinds(WaitCondition(), waiting(NOW - 60 * M), old) == [ActionKind.ALERT]
    assert kinds(WaitCondition(), waiting(NOW - 60 * M), old, new) == []


def test_block_condition_wins_over_task_links() -> None:
    task = waiting(block=Block("waiting_ci", NOW - 60 * M, "c"))
    assert kinds(WaitCondition(), task, cond("c", False, task_id=None), cond("x", True)) == []


def test_waiting_on_nothing_has_a_grace_period() -> None:
    rule = WaitCondition(no_condition_grace=timedelta(minutes=10))
    assert kinds(rule, waiting(NOW - 9 * M)) == []
    assert kinds(rule, waiting(NOW - 10 * M)) == [ActionKind.ALERT]


def test_missing_block_condition_is_named() -> None:
    task = waiting(block=Block("waiting_ci", NOW - 60 * M, "C-gone"))
    [action] = WaitCondition().evaluate(Snapshot(now=NOW, tasks=(task,)))
    assert "C-gone" in action.reason and "cannot see" in action.reason


def test_other_tasks_conditions_are_ignored() -> None:
    assert kinds(WaitCondition(), waiting(), cond("x", True, task_id="T-2")) == [
        ActionKind.ALERT  # waiting on nothing of its own
    ]


def test_overdue_boundary() -> None:
    rule = WaitCondition(max_wait=timedelta(hours=2))
    assert kinds(rule, waiting(NOW - 119 * M), cond("a", False)) == []
    assert kinds(rule, waiting(NOW - 120 * M), cond("a", False)) == [ActionKind.ALERT]


@pytest.mark.parametrize("status", [Status.BLOCKED, Status.IN_PROGRESS, Status.READY])
def test_only_waiting_tasks_are_woken(status: Status) -> None:
    task = Task("T-1", "t", status, NOW - 60 * M)
    assert kinds(WaitCondition(), task, cond("a", True)) == []


def test_wake_key_changes_with_each_wait() -> None:
    rule = WaitCondition()
    first = rule.evaluate(
        Snapshot(now=NOW, tasks=(waiting(NOW - 60 * M),), conditions=(cond("a", True),))
    )
    second = rule.evaluate(
        Snapshot(now=NOW, tasks=(waiting(NOW - 5 * M),), conditions=(cond("a", True),))
    )
    assert first[0].key != second[0].key  # waiting again later wakes again


# --- polling detection ------------------------------------------------------------------


def call(i: int, args: str = "same", outcome: str = "ok", command: str = "") -> Span:
    attributes = {"command": command} if command else {}
    start = NOW - (20 - i) * M
    return Span(
        "tr", f"s{i}", "execute_tool bash", SpanKind.TOOL, start, end=start + timedelta(seconds=2),
        session_id="S-1", tool="bash", args_hash=args, outcome=outcome, attributes=attributes,
    )  # fmt: skip


def poll_actions(rule: WaitCondition, spans: list[Span], ended: datetime | None = None) -> list:
    session = Session("S-1", "T-1", "agent", NOW - 30 * M, ended_at=ended, last_heartbeat=NOW - M)
    return list(rule.evaluate(Snapshot(now=NOW, sessions=(session,), spans=tuple(spans))))


CI_RULE = {"ci_status": ("gh pr checks", "gh run watch")}


def check(i: int, **kw: object) -> Span:
    return call(i, args="h-checks", command="gh pr checks 7", **kw)  # type: ignore[arg-type]


def test_repeated_check_threshold_and_window() -> None:
    rule = WaitCondition(poll_min_repeats=8, poll_patterns=CI_RULE)
    assert poll_actions(rule, [check(i) for i in range(12, 19)]) == []  # 7 in window
    [action] = poll_actions(rule, [check(i) for i in range(11, 19)])  # 8 in window
    assert action.kind is ActionKind.COMMENT and action.task_id == "T-1"
    assert action.params == {"session_id": "S-1", "condition_kind": "ci_status"}
    assert "`ci_status`" in action.reason
    assert poll_actions(rule, [check(i) for i in range(0, 10)]) == []  # all older than window


def test_polling_with_other_calls_in_between_is_caught() -> None:
    rule = WaitCondition(
        poll_min_repeats=4, poll_window=timedelta(minutes=30), poll_patterns=CI_RULE
    )
    spans = [call(i, args="sleep 30") if i % 2 else check(i) for i in range(10, 18)]
    assert len(poll_actions(rule, spans)) == 1


def test_loops_that_are_not_known_checks_are_rule_10s() -> None:
    rule = WaitCondition(poll_min_repeats=3, poll_patterns=CI_RULE)
    heartbeat = [call(i, args="hb-9f2c", command="heartbeat_check") for i in range(10, 19)]
    assert poll_actions(rule, heartbeat) == []
    assert poll_actions(WaitCondition(poll_min_repeats=3), [check(i) for i in range(10, 19)]) == []


def test_calls_without_known_arguments_are_never_identical() -> None:
    rule = WaitCondition(poll_min_repeats=3, poll_patterns={"ci_status": ("bash",)})
    unknown = [call(i, args="") for i in range(10, 19)]
    assert poll_actions(rule, unknown) == []


def test_error_streak_must_be_one_known_check() -> None:
    rule = WaitCondition(poll_error_streak=5, poll_min_repeats=50, poll_patterns=CI_RULE)
    mixed = [call(i, args="pytest", outcome="error") for i in range(13, 17)]
    mixed.append(check(17, outcome="error"))
    assert poll_actions(rule, mixed) == []  # 4 test failures + 1 CI check is not polling
    [action] = poll_actions(rule, [check(i, outcome="error") for i in range(13, 18)])
    assert "failed the same check 5 times" in action.reason


def test_one_long_blocking_check_is_caught() -> None:
    rule = WaitCondition(poll_window=timedelta(minutes=10), poll_patterns=CI_RULE)
    watch = Span(
        "tr", "w", "execute_tool bash", SpanKind.TOOL, NOW - timedelta(minutes=40),
        session_id="S-1", tool="bash", attributes={"command": "gh run watch 12"},
    )  # fmt: skip
    [action] = poll_actions(rule, [watch])
    assert "blocked on one check for 40 minutes" in action.reason
    short = Span("tr", "w", "x", SpanKind.TOOL, NOW - timedelta(minutes=5), session_id="S-1",
                 attributes={"command": "gh run watch 12"})  # fmt: skip
    assert poll_actions(rule, [short]) == []


def test_ended_sessions_are_not_checked() -> None:
    rule = WaitCondition(poll_min_repeats=2, poll_patterns=CI_RULE)
    assert poll_actions(rule, [check(i) for i in range(15, 19)], ended=NOW) == []


def test_helpers() -> None:
    assert identical_streak([]) is None
    assert error_streak([call(1, outcome="error"), call(2)]) == ()
    llm = Span("tr", "l", "chat", SpanKind.LLM, NOW, session_id="S-1")
    assert tool_calls([call(2), llm, call(1)]) == [call(1), call(2)]
    no_args = Span("tr", "x", "execute_tool read", SpanKind.TOOL, NOW, attributes={"command": "ls"})
    assert call_key(no_args) == ("execute_tool read", "ls")
    assert call_key(Span("tr", "y", "edit", SpanKind.TOOL, NOW)) is None
    streak = identical_streak([call(1, "a"), call(2), call(3)])
    assert streak is not None and streak.length == 2 and streak.key == ("bash", "same")
    assert identical_streak([Span("tr", "y", "edit", SpanKind.TOOL, NOW)]) is None


# --- config -----------------------------------------------------------------------------


def test_build_from_config() -> None:
    rule = build(
        {
            "max_wait": "6h",
            "poll_window": "45m",
            "poll_min_repeats": 10,
            "poll_error_streak": 3,
            "poll_patterns": {"ci_status": ["gh pr checks"]},
        }
    )
    assert rule.max_wait == timedelta(hours=6) and rule.poll_window == timedelta(minutes=45)
    assert (rule.poll_min_repeats, rule.poll_error_streak) == (10, 3)
    assert rule.poll_patterns == {"ci_status": ("gh pr checks",)}


@pytest.mark.parametrize(
    "config",
    [
        {"poll_min_repeats": 0},
        {"poll_error_streak": True},
        {"poll_patterns": ["gh"]},
        {"poll_patterns": {"ci_status": "gh"}},
        {"poll_patterns": {"ci_status": [""]}},
        {"max_wait": [1]},
        {"max_wait": -5},
        {"poll_window": "0m"},
        {"max_condition_age": "0s"},
        {"poll_every": "1m"},
    ],
)
def test_bad_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)
