"""The daily report per board (#283): three boards of one Hermes home (two app projects
and a service), each with its cost of chaos, throughput and biggest wait, plus the farm."""

from __future__ import annotations

from datetime import timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.ledger import Outcome
from kelpie.core.model import Owner, Session, Status, Task
from kelpie.report.boards import board_prefix, board_prefixes, rest_line, split_inputs
from kelpie.report.daily import build_report, render_markdown
from kelpie.report.store import StateRow
from tests.report.test_daily import at, day_inputs, entry, llm, state
from tests.test_single_board_golden import assert_golden


def task(board: str, local: str, status: Status, since: object, **kw: object) -> Task:
    return Task(f"{board}:{local}", f"{board} {local}", status, since, board=board, **kw)  # type: ignore[arg-type]


TASKS = (
    task("app-a", "t_1", Status.DONE, at(15), role="developer"),
    task("app-a", "t_2", Status.IN_PROGRESS, at(8), role="developer", owner_id=None),
    task("app-b", "t_1", Status.DONE, at(11), role="developer"),
    task("app-b", "t_2", Status.DONE, at(17), role="developer"),
    task("app-b", "t_3", Status.READY, at(9), role="developer"),
    task("svc", "t_9", Status.BLOCKED, at(6), role="ops"),
)
SESSIONS = (
    Session("app-a:hermes-run-1", "app-a:t_1", "dev-1", at(8), ended_at=at(15)),
    Session("app-b:hermes-run-1", "app-b:t_3", "dev-1", at(13), ended_at=at(13, 45)),
    Session("svc:hermes-run-1", "svc:t_9", "ops-1", at(6), ended_at=at(10)),
)
ACTIONS = (
    entry(
        at(14),
        "exit_contract",
        ActionKind.REQUEUE,
        Outcome.EXECUTED,
        "app-b:t_3",
        session_id="app-b:hermes-run-1",
    ),
)
STATES = (
    StateRow(state("orphan_status:app-a:t_2", "orphan_status", at(8), at(12), "app-a:t_2")),
    StateRow(state("stale_blocks:svc:t_9", "stale_blocks", at(10), at(20), "svc:t_9")),
    StateRow(
        state("no_progress:svc:hermes-run-1", "no_progress", at(6), at(10), "svc:t_9"), at(10, 5)
    ),
    # a state that names no task names no board: the farm total only
    StateRow(state("orphan_status:farm", "orphan_status", at(18), at(20))),
)
SPANS = (
    *llm("svc:hermes-run-1", "svc:t_9", at(6), 48, timedelta(minutes=5), (12_000, 40, 0)),
    *llm("app-b:hermes-run-1", "app-b:t_3", at(13), 5, timedelta(minutes=5), (20_000, 500, 0)),
    *llm("app-a:hermes-run-1", "app-a:t_1", at(8), 10, timedelta(minutes=30), (50_000, 2_000, 0)),
)


def inputs(**kw: object):  # type: ignore[no-untyped-def]
    return day_inputs(
        tasks=TASKS,
        sessions=SESSIONS,
        actions=ACTIONS,
        states=STATES,
        spans=SPANS,
        owners=(Owner("dev-1", "developer"), Owner("ops-1", "ops")),
        **kw,
    )


def test_board_prefix_of_one_board_several_and_hosts() -> None:
    assert board_prefix(Task("t_1", "t", Status.READY, at(1))) is None
    assert board_prefix(TASKS[0]) == "app-a"
    on_host = Task("h1:svc:t_1", "t", Status.READY, at(1), board="svc", host="h1")
    assert board_prefix(on_host) == "h1:svc"
    assert board_prefixes((*TASKS, on_host)) == ("app-a", "app-b", "h1:svc", "svc")


def test_one_board_splits_into_nothing() -> None:
    one = tuple(t for t in TASKS if t.board == "app-a")
    assert split_inputs(day_inputs(tasks=one)) == {}
    assert build_report(day_inputs(tasks=one), {}, {}).boards == ()
    plain = (Task("t_1", "t", Status.DONE, at(3)), Task("t_2", "t", Status.DONE, at(4)))
    assert build_report(day_inputs(tasks=plain), {}, {}).boards == ()


def test_each_input_goes_to_its_board() -> None:
    parts = split_inputs(inputs())
    assert list(parts) == ["app-a", "app-b", "svc"]
    svc = parts["svc"]
    assert [t.id for t in svc.tasks] == ["svc:t_9"]
    assert [s.id for s in svc.sessions] == ["svc:hermes-run-1"]
    assert {s.session_id for s in svc.spans} == {"svc:hermes-run-1"}
    assert [r.state.id for r in svc.states] == [
        "stale_blocks:svc:t_9",
        "no_progress:svc:hermes-run-1",
    ]
    assert [e.task_id for e in parts["app-b"].actions] == ["app-b:t_3"]
    assert not any(r.state.task_id is None for p in parts.values() for r in p.states)


def test_three_boards_and_the_farm_total() -> None:
    report = build_report(inputs(hour_usd=40.0), {}, {})
    lines = {b.board: b for b in report.boards}
    assert list(lines) == ["app-a", "app-b", "svc"]
    assert lines["app-a"].chaos_hours == pytest.approx(4.0)  # ownerless 08-12
    assert lines["app-a"].completed == 1
    assert lines["app-b"].completed == 2
    assert lines["app-b"].chaos_tokens == 5 * 20_500  # the restart
    assert lines["svc"].chaos_hours == pytest.approx(14.0)  # no progress 06-10, block 10-20
    assert lines["svc"].chaos_tokens == 48 * 12_040
    assert lines["svc"].completed == 0
    # the boards add up to the farm, but for what names no board (the hold)
    rest = rest_line(report, report.boards)
    assert rest is not None and rest.chaos_hours == pytest.approx(2.0)
    total = sum(b.chaos_hours for b in report.boards) + (rest.chaos_hours if rest else 0)
    assert total == pytest.approx(report.chaos_hours)
    assert sum(b.chaos_tokens for b in report.boards) == report.chaos_spend.billed_tokens
    assert sum(b.completed for b in report.boards) == len(report.completed) == 3
    markdown = render_markdown(report)
    assert "## Per board" in markdown and "| **Farm total** |" in markdown
    assert "| not on one board | 2.0 |" in markdown
    assert_golden("report_three_boards.md", markdown)


def test_hostile_board_names_stay_in_their_cell() -> None:
    evil = (
        Task("a|b:t_1", "t", Status.DONE, at(3), board="a|b"),
        Task("c`d:t_1", "t", Status.DONE, at(4), board="c`d"),
    )
    markdown = render_markdown(build_report(day_inputs(tasks=evil), {}, {}))
    rows = [line for line in markdown.splitlines() if line.startswith("| `")]
    assert len(rows) == 2 and all(line.count(" | ") == 5 for line in rows)
