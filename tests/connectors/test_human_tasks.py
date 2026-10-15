"""The human task connector: ``human_task`` conditions against human-owned tasks."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.app import (
    ConfigError,
    HermesSettings,
    Settings,
    _connector_steps,
    build_adapters,
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


@pytest.mark.parametrize("status", [Status.DONE, Status.FAILED, Status.CANCELLED])
def test_terminal_human_task_satisfies(status: Status) -> None:
    since = NOW - 5 * M
    result = checked(HumanTaskConnector(), snapshot(human_task(status, since)))
    assert result.satisfied is True
    assert result.satisfied_at == since  # when the human's task finished
    assert result.checked_at == NOW
    assert result.params["status"] == status.value and result.params["owner"] == "alice"
    assert result.registered_at == NOW - 60 * M and result.task_id == "T-1"


@pytest.mark.parametrize(
    "status",
    [Status.BACKLOG, Status.READY, Status.IN_PROGRESS, Status.BLOCKED, Status.REVIEW],
)
def test_open_human_task_does_not(status: Status) -> None:
    result = checked(HumanTaskConnector(), snapshot(human_task(status)))
    assert result.satisfied is False and result.satisfied_at is None
    assert result.checked_at == NOW and result.params["status"] == status.value


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


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[connectors.human_tasks]\nenabled = 'yes'\n", "enabled"),
        ("[connectors.human_tasks]\nurl = 'x'\n", "unknown"),
        ("[connectors]\nhuman_tasks = 1\n", "must be a table"),
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
        return [name for name, _ in _connector_steps(settings, Metrics(), lambda: NOW)]

    assert steps("") == []
    assert steps("[connectors.human_tasks]\nenabled = false\n") == []
    assert steps("[connectors.human_tasks]\n") == ["human_tasks"]


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
