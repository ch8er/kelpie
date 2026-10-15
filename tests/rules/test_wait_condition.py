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


def cond(cid: str, satisfied: bool | None, task_id: str | None = "T-1") -> Condition:
    return Condition(cid, CI, task_id=task_id, satisfied=satisfied)


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
    return Span(
        "tr", f"s{i}", "execute_tool bash", SpanKind.TOOL, NOW - (20 - i) * M,
        session_id="S-1", tool="bash", args_hash=args, outcome=outcome, attributes=attributes,
    )  # fmt: skip


def poll_actions(rule: WaitCondition, spans: list[Span], ended: datetime | None = None) -> list:
    session = Session("S-1", "T-1", "agent", NOW - 30 * M, ended_at=ended)
    return list(rule.evaluate(Snapshot(now=NOW, sessions=(session,), spans=tuple(spans))))


def test_identical_calls_threshold_and_window() -> None:
    rule = WaitCondition(poll_min_repeats=8, poll_window=timedelta(minutes=10))
    assert poll_actions(rule, [call(i) for i in range(12, 19)]) == []  # 7 in window
    [action] = poll_actions(rule, [call(i) for i in range(11, 19)])  # 8 in window
    assert action.kind is ActionKind.COMMENT and action.task_id == "T-1"
    assert "condition_kind" not in action.params
    assert poll_actions(rule, [call(i) for i in range(0, 10)]) == []  # all older than window


def test_streak_must_be_consecutive_and_trailing() -> None:
    rule = WaitCondition(poll_min_repeats=3, poll_window=timedelta(minutes=30))
    assert poll_actions(rule, [call(1), call(2), call(3, "other"), call(4), call(5)]) == []
    assert poll_actions(rule, [call(1, "x"), call(2), call(3), call(4)]) != []


def test_condition_kind_from_command_pattern() -> None:
    rule = WaitCondition(poll_min_repeats=3, poll_patterns={"ci_status": ("gh pr checks",)})
    spans = [call(i, args="h", command="gh pr checks 7") for i in range(15, 18)]
    [action] = poll_actions(rule, spans)
    assert action.params == {"session_id": "S-1", "condition_kind": "ci_status"}
    assert "`ci_status`" in action.reason


def test_error_streak_only_for_known_polling_checks() -> None:
    rule = WaitCondition(poll_error_streak=5, poll_patterns={"ci_status": ("gh run watch",)})
    varied = [call(i, args=f"a{i}", outcome="error") for i in range(13, 18)]
    assert poll_actions(rule, varied) == []  # generic error streaks belong to rule 10
    polled = [call(i, args=f"a{i}", outcome="error", command="gh run watch") for i in range(13, 18)]
    [action] = poll_actions(rule, polled)
    assert "failed the same check 5 times" in action.reason


def test_ended_sessions_are_not_checked() -> None:
    rule = WaitCondition(poll_min_repeats=2)
    assert poll_actions(rule, [call(i) for i in range(15, 19)], ended=NOW) == []


def test_helpers() -> None:
    assert identical_streak([]) is None
    assert error_streak([call(1, outcome="error"), call(2)]) == ()
    llm = Span("tr", "l", "chat", SpanKind.LLM, NOW, session_id="S-1")
    assert tool_calls([call(2), llm, call(1)]) == [call(1), call(2)]
    no_args = Span("tr", "x", "execute_tool read", SpanKind.TOOL, NOW, attributes={"command": "ls"})
    assert call_key(no_args) == ("execute_tool read", "ls")


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
        {"poll_every": "1m"},
    ],
)
def test_bad_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)
