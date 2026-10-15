"""End to end: a synthetic Hermes board -> adapter snapshot -> the registered rules.

Reproduces incident 2 as Hermes records it: a worker run crashed while the task stayed
``running``, so nobody works on it and nobody was told.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.adapters.hermes import HermesAdapter
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.rules import PLANNED, REGISTRY
from tests.adapters.kanban import Board

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def build_board(tmp_path: Path) -> HermesAdapter:
    board = Board(tmp_path / "kanban.db")
    # t_201: the worker crashed, the task is still "running".
    board.task("t_201", "running", ago(days=1), assignee="dev", started=ago(hours=2))
    board.event("t_201", "claimed", ago(hours=2))
    board.run(
        "t_201",
        "dev",
        ago(hours=2),
        status="crashed",
        ended=ago(hours=1),
        heartbeat=ago(hours=1, minutes=1),
        outcome="crashed",
        error="worker process disappeared",
    )
    # t_203: a healthy worker, fresh heartbeat.
    board.task("t_203", "running", ago(days=1), assignee="dev", started=ago(minutes=30))
    board.event("t_203", "claimed", ago(minutes=30))
    board.run("t_203", "dev", ago(minutes=30), heartbeat=ago(minutes=1))
    # t_204: finished properly and reviewed by another profile.
    board.task("t_204", "review", ago(days=1), assignee="reviewer")
    board.event("t_204", "review_requested", ago(hours=1))
    board.run(
        "t_204",
        "dev",
        ago(hours=3),
        status="done",
        ended=ago(hours=1),
        outcome="review_requested",
    )
    board.close()
    return HermesAdapter(db_path=tmp_path / "kanban.db", spool=None)


def evaluate_all(adapter: HermesAdapter) -> list[Action]:
    snapshot = adapter.snapshot(NOW)
    assert {p.name for p in PLANNED} >= set(REGISTRY)
    actions: list[Action] = []
    for factory in REGISTRY.values():
        actions.extend(factory({}).evaluate(snapshot))
    return actions


def test_exit_contract_requeues_the_crashed_run(tmp_path: Path) -> None:
    actions = evaluate_all(build_board(tmp_path))
    requeues = [a for a in actions if a.kind is ActionKind.REQUEUE]
    assert [(a.rule, a.task_id) for a in requeues] == [("exit_contract", "t_201")]
    assert "without a final status" in requeues[0].reason
    assert requeues[0].params["owner_id"] == "dev"
    # Negative cases: the live worker and the proper hand-off are left alone.
    touched = {a.task_id for a in actions}
    assert "t_203" not in touched and "t_204" not in touched


def test_engine_observes_without_an_executor_call(tmp_path: Path) -> None:
    adapter = build_board(tmp_path)
    rules = [factory({}) for factory in REGISTRY.values()]
    engine = Engine(rules, adapters=[adapter], config=EngineConfig())
    results = engine.tick(lambda: adapter.snapshot(NOW))
    requeues = [d for d in results if d.action.kind is ActionKind.REQUEUE]
    assert [d.action.task_id for d in requeues] == ["t_201"]
    assert requeues[0].outcome in {Outcome.DRY_RUN, Outcome.OBSERVED}  # nothing was executed
