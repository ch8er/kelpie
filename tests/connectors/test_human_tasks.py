"""The human task connector: ``human_task`` conditions against human-owned tasks."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.app import (
    ConfigError,
    HermesSettings,
    Settings,
    build_adapters,
    connector_steps,
    load_rules,
    load_settings,
    render_status,
)
from kelpie.connectors.human_tasks import (
    HumanTaskConnector,
    HumanTaskSettings,
    load_human_tasks,
)
from kelpie.core.actions import ActionKind
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Owner,
    OwnerKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.core.ports import ConditionConnector
from kelpie.metrics import Metrics
from kelpie.rules.wait_condition import WaitCondition
from tests.adapters.kanban import Board

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
HUMAN = Owner("alice", "human", OwnerKind.HUMAN)
AGENT = Owner("coder", "coder", OwnerKind.AGENT)


def human_task(status: Status = Status.IN_PROGRESS, since: datetime = NOW - 30 * M) -> Task:
    return Task("H-1", "Approve access", status, since, owner_id="alice")


def waiting_task() -> Task:
    return Task("T-1", "needs access", Status.WAITING, NOW - 60 * M, owner_id="coder")


def condition(**params: object) -> Condition:
    return Condition(
        "wait-h1",
        ConditionKind.HUMAN_TASK,
        task_id="T-1",
        params={"task": "H-1", **params},
        registered_at=NOW - 60 * M,
    )


def snapshot(*tasks: Task, conditions: tuple[Condition, ...] | None = None) -> Snapshot:
    return Snapshot(
        now=NOW,
        tasks=(waiting_task(), *tasks),
        owners=(HUMAN, AGENT),
        conditions=(condition(),) if conditions is None else conditions,
    )


def checked(connector: HumanTaskConnector, snap: Snapshot) -> Condition:
    (result,) = connector(snap).conditions
    return result


def test_implements_the_port() -> None:
    connector = HumanTaskConnector()
    assert isinstance(connector, ConditionConnector)
    assert connector.kinds == {ConditionKind.HUMAN_TASK}


def test_done_human_task_satisfies() -> None:
    since = NOW - 5 * M
    result = checked(HumanTaskConnector(), snapshot(human_task(Status.DONE, since)))
    assert result.satisfied is True
    assert result.satisfied_at == since  # when the human's task finished
    assert result.checked_at == NOW
    assert result.params["status"] == "done" and result.params["owner"] == "alice"
    assert "terminal" not in result.params
    assert result.registered_at == NOW - 60 * M and result.task_id == "T-1"


@pytest.mark.parametrize("status", [Status.FAILED, Status.CANCELLED])
def test_failed_or_cancelled_never_satisfies_and_is_terminal(status: Status) -> None:
    result = checked(HumanTaskConnector(), snapshot(human_task(status)))
    assert result.satisfied is False and result.satisfied_at is None
    assert result.params["status"] == status.value and result.params["terminal"] is True
    assert result.checked_at == NOW


@pytest.mark.parametrize(
    "status",
    [Status.BACKLOG, Status.READY, Status.IN_PROGRESS, Status.BLOCKED, Status.REVIEW],
)
def test_open_human_task_does_not(status: Status) -> None:
    result = checked(HumanTaskConnector(), snapshot(human_task(status)))
    assert result.satisfied is False and result.satisfied_at is None
    assert result.checked_at == NOW and result.params["status"] == status.value
    assert "terminal" not in result.params


def test_task_not_on_the_board_is_unknown() -> None:
    result = checked(HumanTaskConnector(), snapshot())
    assert result.satisfied is None and result.params["error"] == "not_found"
    assert result.checked_at == NOW


@pytest.mark.parametrize("owner_id", ["coder", None, "nobody"])
def test_task_of_a_non_human_is_unknown(owner_id: str | None) -> None:
    task = Task("H-1", "t", Status.DONE, NOW - M, owner_id=owner_id)
    result = checked(HumanTaskConnector(), snapshot(task))
    assert result.satisfied is None and result.params["error"] == "not_human_task"


@pytest.mark.parametrize("params", [{}, {"task": ""}, {"task": "  "}, {"task": 3}])
def test_invalid_params_are_unknown(params: dict[str, object]) -> None:
    bad = Condition("c", ConditionKind.HUMAN_TASK, task_id="T-1", params=params)
    result = checked(HumanTaskConnector(), snapshot(human_task(Status.DONE), conditions=(bad,)))
    assert result.satisfied is None and result.params["error"] == "invalid_params"


def test_other_conditions_are_untouched() -> None:
    ci = Condition("ci", ConditionKind.CI_STATUS, task_id="T-1", satisfied=False)
    snap = snapshot(human_task(Status.DONE), conditions=(ci, condition()))
    out = HumanTaskConnector()(snap).conditions
    assert out[0] is ci and out[1].satisfied is True


def test_flip_back_and_stale_results_are_replaced() -> None:
    connector = HumanTaskConnector()
    done = connector(snapshot(human_task(Status.DONE))).conditions
    assert done[0].satisfied is True
    # The human reopened the task; last tick's error/status must not linger either.
    reopened = connector(snapshot(human_task(Status.IN_PROGRESS), conditions=done)).conditions
    assert reopened[0].satisfied is False and reopened[0].satisfied_at is None
    assert "error" not in reopened[0].params and reopened[0].params["status"] == "in_progress"
    failed = connector(snapshot(human_task(Status.FAILED), conditions=reopened)).conditions
    assert failed[0].params["terminal"] is True
    again = connector(snapshot(human_task(Status.IN_PROGRESS), conditions=failed)).conditions
    assert "terminal" not in again[0].params and again[0].satisfied is False
    missing = connector(snapshot(conditions=reopened)).conditions
    assert missing[0].params["error"] == "not_found" and "status" not in missing[0].params


def test_finished_task_that_left_the_board_stays_satisfied() -> None:
    connector = HumanTaskConnector()
    since = NOW - 5 * M
    connector(snapshot(human_task(Status.DONE, since)))
    result = checked(connector, snapshot())  # the adapter no longer lists the old task
    assert result.satisfied is True and result.satisfied_at == since
    assert result.params["task_left_board"] is True
    # A condition that is gone from the board is forgotten.
    connector(snapshot(conditions=()))
    assert checked(connector, snapshot()).satisfied is None


def test_check_needs_a_snapshot_and_the_right_kind() -> None:
    connector = HumanTaskConnector()
    c = condition()
    assert connector.check(c) is c  # nothing bound yet
    connector.bind(snapshot(human_task(Status.DONE)))
    assert connector.check(c).satisfied is True
    other = Condition("x", ConditionKind.TIME)
    assert connector.check(other) is other


# --- with the wait_condition rule -------------------------------------------------------


def wake_kinds(snap: Snapshot) -> list[ActionKind]:
    return [a.kind for a in WaitCondition(max_condition_age=15 * M).evaluate(snap)]


def test_finished_human_task_wakes_the_waiting_agent_task() -> None:
    connector = HumanTaskConnector()
    done = connector(snapshot(human_task(Status.DONE, NOW - 2 * M)))
    actions = WaitCondition(max_condition_age=15 * M).evaluate(done)
    assert [a.kind for a in actions] == [ActionKind.WAKE] and actions[0].task_id == "T-1"
    assert actions[0].params == {"conditions": ["wait-h1"]}


@pytest.mark.parametrize(
    "tasks",
    [
        (human_task(Status.IN_PROGRESS),),
        (),
        (Task("H-1", "t", Status.DONE, NOW, owner_id="coder"),),
    ],
    ids=["open", "missing", "not-human"],
)
def test_nothing_wakes_while_the_human_is_not_done(tasks: tuple[Task, ...]) -> None:
    assert wake_kinds(HumanTaskConnector()(snapshot(*tasks))) == []


# --- block reasons -------------------------------------------------------------------


def blocked_agent(reason: str, task_id: str = "T-1", since: datetime = NOW - 60 * M) -> Task:
    return Task(
        task_id,
        "needs approval",
        Status.BLOCKED,
        since,
        owner_id="coder",
        block=Block(reason, since),
    )


PATTERN = re.compile(r"approval in (?P<task>\S+)", re.IGNORECASE)


def attach(*tasks: Task, patterns: tuple[re.Pattern[str], ...] = (PATTERN,)) -> Snapshot:
    snap = Snapshot(now=NOW, tasks=tasks, owners=(HUMAN, AGENT))
    return HumanTaskConnector(patterns)(snap)


def test_block_reason_naming_a_human_task_gets_a_condition() -> None:
    out = attach(blocked_agent("waiting_human: Approval in H-1."), human_task(Status.DONE))
    (c,) = out.conditions
    assert c.id == "block:T-1:human_task:H-1" and c.task_id == "T-1"
    assert c.kind is ConditionKind.HUMAN_TASK and c.registered_at == NOW - 60 * M
    assert c.satisfied is True and c.params["task"] == "H-1"
    assert c.params["source"] == "block_reason"
    task = out.task("T-1")
    assert task is not None and task.block is not None
    assert (
        task.block.condition_id == c.id and task.block.reason == "waiting_human: Approval in H-1."
    )


def test_attached_condition_drives_rule_4() -> None:
    from kelpie.rules.stale_blocks import StaleBlocks

    done = attach(
        blocked_agent("waiting_human: approval in H-1"), human_task(Status.DONE, NOW - 30 * M)
    )
    kinds = [a.kind for a in StaleBlocks(unblock_grace=5 * M).evaluate(done)]
    assert kinds == [ActionKind.UNBLOCK]
    cancelled = attach(
        blocked_agent("waiting_human: approval in H-1"), human_task(Status.CANCELLED, NOW - 30 * M)
    )
    (alert,) = StaleBlocks().evaluate(cancelled)
    assert alert.kind is ActionKind.ALERT and "ended as cancelled" in alert.reason
    open_ = attach(blocked_agent("waiting_human: approval in H-1"), human_task())
    assert StaleBlocks().evaluate(open_) == []


@pytest.mark.parametrize(
    "tasks",
    [
        (blocked_agent("waiting for a nice day"), human_task()),  # no pattern match
        (blocked_agent("approval in H-9"), human_task()),  # task not on the board
        (
            blocked_agent("approval in T-2", "T-1"),
            Task("T-2", "t", Status.READY, NOW, owner_id="coder"),
        ),
        (blocked_agent("approval in T-1"),),  # itself
        (blocked_agent("approval in H-1"), Task("H-1", "t", Status.READY, NOW)),  # unowned
    ],
    ids=["no-match", "not-on-board", "agent-task", "self", "unowned"],
)
def test_block_without_a_human_task_is_left_alone(tasks: tuple[Task, ...]) -> None:
    out = attach(*tasks)
    assert out.conditions == () and out.tasks == tasks


def test_blocks_with_a_condition_or_other_status_are_not_touched() -> None:
    linked = replace(
        blocked_agent("approval in H-1"),
        block=Block("approval in H-1", NOW - 60 * M, condition_id="other"),
    )
    waiting = replace(blocked_agent("approval in H-1"), status=Status.WAITING)
    existing = Condition("mine", ConditionKind.HUMAN_TASK, task_id="T-3", params={"task": "H-1"})
    third = replace(blocked_agent("approval in H-1", "T-3"))
    snap = Snapshot(
        now=NOW,
        tasks=(linked, waiting, third, human_task()),
        owners=(HUMAN, AGENT),
        conditions=(existing,),
    )
    out = HumanTaskConnector((PATTERN,))(snap)
    assert [c.id for c in out.conditions] == ["mine"]  # T-3 already has a linked condition
    assert out.tasks[:3] == (linked, waiting, third)


def test_patterns_are_tried_in_order_and_the_reference_is_trimmed() -> None:
    first = re.compile(r"blocked by (?P<task>\S+)")
    out = attach(
        blocked_agent("approval in (H-1); blocked by H-404"),
        human_task(),
        patterns=(first, PATTERN),
    )
    assert [c.params["task"] for c in out.conditions] == ["H-1"]  # H-404 is not a human task


def test_no_patterns_means_no_attachment() -> None:
    out = attach(blocked_agent("approval in H-1"), human_task(), patterns=())
    assert out.conditions == ()


# --- with the Hermes adapter (human-owned tasks on the same board) -----------------------


def test_hermes_human_owners_end_to_end(tmp_path: Path) -> None:
    board = Board(tmp_path / "kanban.db")
    try:
        board.task("h1", "ready", NOW - 3 * M * 60, assignee="Alice")
        board.task("a1", "blocked", NOW - 3 * M * 60, assignee="coder")
        adapter = HermesAdapter(db_path=board.path, spool=None, human_owners=["alice"])
        snap = adapter.snapshot(NOW)
        kinds = {o.id: o.kind for o in snap.owners}
        assert kinds == {"Alice": OwnerKind.HUMAN, "coder": OwnerKind.AGENT}
        waiting = Condition(
            "w", ConditionKind.HUMAN_TASK, task_id="a1", params={"task": "h1"}, registered_at=NOW
        )
        connector = HumanTaskConnector()
        open_ = connector(replace(snap, conditions=(waiting,))).conditions[0]
        assert open_.satisfied is False and open_.params["status"] == "ready"
        board.update("h1", status="done", completed_at=NOW - M)
        done = connector(replace(adapter.snapshot(NOW), conditions=(waiting,))).conditions[0]
        assert done.satisfied is True and done.params["status"] == "done"
    finally:
        board.close()


def test_hermes_without_human_owners_has_only_agents(tmp_path: Path) -> None:
    board = Board(tmp_path / "kanban.db")
    try:
        board.task("h1", "ready", NOW - M, assignee="alice")
        snap = HermesAdapter(db_path=board.path, spool=None).snapshot(NOW)
        assert {o.kind for o in snap.owners} == {OwnerKind.AGENT}
    finally:
        board.close()


# --- config and wiring -------------------------------------------------------------------


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


def test_settings_parsing(tmp_path: Path) -> None:
    assert load_settings(write(tmp_path, "")).human_tasks is None
    on = load_settings(write(tmp_path, "[connectors.human_tasks]\n")).human_tasks
    assert on == HumanTaskSettings(enabled=True)
    off = load_settings(write(tmp_path, "[connectors.human_tasks]\nenabled = false\n"))
    assert off.human_tasks == HumanTaskSettings(enabled=False)
    assert load_human_tasks({}) == HumanTaskSettings()
    text = (
        "[connectors.human_tasks]\n"
        "[[connectors.human_tasks.block_patterns]]\n"
        "pattern = 'approval in (?P<task>\\S+)'\n"
    )
    configured = load_settings(write(tmp_path, text)).human_tasks
    assert configured is not None and len(configured.block_patterns) == 1
    assert configured.block_patterns[0].search("Approval in T-9")["task"] == "T-9"  # type: ignore[index]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[connectors.human_tasks]\nenabled = 'yes'\n", "enabled"),
        ("[connectors.human_tasks]\nurl = 'x'\n", "unknown"),
        ("[connectors]\nhuman_tasks = 1\n", "must be a table"),
        ("[connectors.human_tasks]\nblock_patterns = 1\n", "block_patterns"),
        ("[connectors.human_tasks]\nblock_patterns = [1]\n", "block_patterns"),
        ("[[connectors.human_tasks.block_patterns]]\npattern = 'x'\n", "named group"),
        ("[[connectors.human_tasks.block_patterns]]\npattern = '(?P<task>'\n", "compile"),
        ("[[connectors.human_tasks.block_patterns]]\npattern = ''\n", "non-empty"),
        (
            "[[connectors.human_tasks.block_patterns]]\npattern = '(?P<task>x)'\nrepo = 'a'\n",
            "unknown",
        ),
        ("[adapters.hermes]\nhuman_owners = 'alice'\n", "human_owners"),
        ("[adapters.hermes]\nhuman_owners = ['']\n", "human_owners"),
        ("[adapters.hermes]\nhuman_owners = [1]\n", "human_owners"),
        ("[adapters.hermes]\nhuman_owners = ['a', 'A']\n", "human_owners"),
    ],
)
def test_bad_config(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_settings(write(tmp_path, text))


def test_step_is_wired_only_when_configured(tmp_path: Path) -> None:
    def steps(text: str) -> list[str]:
        settings = load_settings(write(tmp_path, text))
        return [name for name, _ in connector_steps(settings, Metrics(), lambda: NOW)[0]]

    # task_status needs only the board and always runs first.
    assert steps("") == ["task_status"]
    assert steps("[connectors.human_tasks]\nenabled = false\n") == ["task_status"]
    assert steps("[connectors.human_tasks]\n") == ["task_status", "human_tasks"]


def test_hermes_human_owners_reach_the_adapter(tmp_path: Path) -> None:
    path = write(tmp_path, '[adapters.hermes]\nhuman_owners = [" Alice ", "bob"]\n')
    settings = load_settings(path)
    assert settings.hermes is not None
    assert settings.hermes.human_owners == frozenset({"Alice", "bob"})
    (adapter,) = build_adapters(settings)
    assert isinstance(adapter, HermesAdapter)
    assert adapter.human_owners == frozenset({"alice", "bob"})  # compared case-insensitively
    assert HermesSettings().human_owners == frozenset()


def test_status_lines(tmp_path: Path) -> None:
    def status(text: str) -> str:
        settings = load_settings(write(tmp_path, text))
        return render_status(settings, load_rules(settings.engine))

    assert "human_tasks" not in status("")
    assert "human_tasks: disabled" in status("[connectors.human_tasks]\nenabled = false\n")
    assert "no human owners configured" in status("[connectors.human_tasks]\n")
    both = status(
        "[connectors.human_tasks]\n[adapters.hermes]\nhuman_owners = ['bob', 'alice']\n"
        "db_path = '/nonexistent/kanban.db'\n"
    )
    assert "hermes human owners alice, bob" in both
    assert Settings().human_tasks is None


def test_block_reason_names_a_human_task_of_the_same_board_by_its_local_id() -> None:
    def on(board: str, task: Task) -> Task:
        return replace(task, id=f"{board}:{task.id}", board=board)

    agent = on("qr", blocked_agent("waiting_human: Approval in H-1."))
    out = attach(agent, on("qr", human_task(Status.DONE)), on("sign", human_task()))
    (c,) = out.conditions
    assert c.task_id == "qr:T-1" and c.params["task"] == "qr:H-1" and c.satisfied is True
    # The same local id on another board only: never that board's task.
    out = attach(agent, on("sign", human_task(Status.DONE)))
    assert out.conditions == ()
