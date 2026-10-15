"""End to end: a synthetic Hermes board -> adapter snapshot -> the registered rules.

The board is built from transitions Hermes really makes (``hermes_cli/kanban_db.py``):

- a worker that blocks a task ends its run with outcome ``blocked``, records a ``blocked``
  event with a typed ``kind`` and leaves the task ``blocked``;
- the crash sweep closes the crashed run and puts the task back to ``ready`` in the same
  transaction, so a crashed run never leaves its task ``running``;
- the circuit breaker parks a task ``blocked`` with a ``gave_up`` event and leaves
  ``tasks.block_kind`` alone;
- a reviewer claims a task in ``review`` and returns it with ``changes_requested``;
- long LLM calls: Hermes extends the claim of a live worker, and its heartbeats may be up to
  an hour old.
"""

from __future__ import annotations

import json
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
RECOMMENDED = {"session_stale_after": "60m"}  # see the Hermes README


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def build_board(tmp_path: Path) -> HermesAdapter:
    board = Board(tmp_path / "kanban.db")
    spool = tmp_path / "spool.jsonl"

    # A worker blocked its task on a question for an operator.
    board.task("t_typed", "blocked", ago(days=1), assignee="dev", block_kind="needs_input")
    board.event("t_typed", "claimed", ago(hours=2))
    board.run(
        "t_typed", "dev", ago(hours=2), status="blocked", ended=ago(hours=1), outcome="blocked"
    )
    board.event(
        "t_typed",
        "blocked",
        ago(hours=1),
        {"kind": "needs_input", "reason": "which API version?", "source_status": "ready"},
    )

    # The same, but an untyped block with a reason Kelpie does not know: this one is bad.
    board.task("t_bad_block", "blocked", ago(days=1), assignee="dev")
    board.event("t_bad_block", "claimed", ago(hours=2))
    board.run(
        "t_bad_block", "dev", ago(hours=2), status="blocked", ended=ago(hours=1), outcome="blocked"
    )
    board.event("t_bad_block", "blocked", ago(hours=1), {"reason": "stuck, not sure why"})

    # Three crashes, then the breaker parks the task. block_kind is left over from an
    # old, long resolved block.
    board.task(
        "t_breaker",
        "blocked",
        ago(days=2),
        assignee="dev",
        block_kind="needs_input",
        failures=3,
        last_error="worker pid gone",
    )
    board.event("t_breaker", "blocked", ago(hours=20), {"kind": "needs_input", "reason": "old"})
    board.event("t_breaker", "unblocked", ago(hours=19))
    for i in range(3):
        start = ago(hours=4 - i)
        board.event("t_breaker", "claimed", start)
        board.run(
            "t_breaker",
            "dev",
            start,
            status="crashed",
            ended=start + timedelta(minutes=10),
            outcome="crashed",
            error="worker pid gone",
        )
        board.event("t_breaker", "crashed", start + timedelta(minutes=10))
    board.event(
        "t_breaker",
        "gave_up",
        ago(hours=1, minutes=49),
        {"failures": 3, "effective_limit": 3, "error": "worker pid gone"},
    )

    # Crash sweep: the run is closed and the task is ready again.
    board.task("t_crash", "ready", ago(days=1), assignee="dev")
    board.event("t_crash", "claimed", ago(hours=2))
    board.run(
        "t_crash",
        "dev",
        ago(hours=2),
        status="crashed",
        ended=ago(hours=1),
        outcome="crashed",
        error="worker pid gone",
    )
    board.event("t_crash", "crashed", ago(hours=1))

    # Review round trip: dev hands over, a reviewer claims and sends it back.
    board.task("t_review", "ready", ago(days=1), assignee="dev")
    board.event("t_review", "claimed", ago(hours=4))
    board.run(
        "t_review",
        "dev",
        ago(hours=4),
        status="done",
        ended=ago(hours=3),
        outcome="review_requested",
    )
    board.event("t_review", "review_requested", ago(hours=3))
    board.event("t_review", "claimed", ago(hours=2), {"source_status": "review"})
    board.run(
        "t_review",
        "reviewer",
        ago(hours=2),
        status="done",
        ended=ago(hours=1),
        outcome="changes_requested",
    )
    board.event("t_review", "changes_requested", ago(hours=1), {"status": "ready"})

    # Live workers whose last heartbeat is 40-45 minutes old.
    board.task("t_hb", "running", ago(days=1), assignee="dev", started=ago(hours=3))
    board.event("t_hb", "claimed", ago(hours=3))
    hb = board.run("t_hb", "dev", ago(hours=3), heartbeat=ago(minutes=45))
    board.update("t_hb", current_run_id=hb, last_heartbeat_at=ago(minutes=45))

    board.task("t_claim", "running", ago(days=1), assignee="dev", started=ago(hours=3))
    board.event("t_claim", "claimed", ago(hours=3))
    claim = board.run("t_claim", "dev", ago(hours=3), heartbeat=ago(minutes=50))
    board.update(
        "t_claim",
        current_run_id=claim,
        last_heartbeat_at=ago(minutes=50),
        claim_expires=NOW + timedelta(minutes=10),
    )

    board.task("t_span", "running", ago(days=1), assignee="dev", started=ago(hours=3))
    board.event("t_span", "claimed", ago(hours=3))
    span = board.run("t_span", "dev", ago(hours=3), heartbeat=ago(minutes=40))
    board.update("t_span", current_run_id=span, last_heartbeat_at=ago(minutes=40))
    spool.write_text(
        json.dumps(
            {"type": "tool", "at": ago(minutes=3).timestamp(), "task_id": "t_span", "run_id": span}
        )
        + "\n"
    )

    # A worker that vanished: stale heartbeat, claim expired, nobody reaped it yet.
    board.task("t_gone", "running", ago(days=1), assignee="dev", started=ago(hours=5))
    board.event("t_gone", "claimed", ago(hours=5))
    gone = board.run("t_gone", "dev", ago(hours=5), heartbeat=ago(hours=2))
    board.update(
        "t_gone", current_run_id=gone, last_heartbeat_at=ago(hours=2), claim_expires=ago(hours=1)
    )

    board.close()
    return HermesAdapter(db_path=tmp_path / "kanban.db", spool=spool)


def evaluate_all(adapter: HermesAdapter, config: dict[str, dict[str, str]]) -> list[Action]:
    snapshot = adapter.snapshot(NOW)
    assert {p.name for p in PLANNED} >= set(REGISTRY)
    actions: list[Action] = []
    for name, factory in REGISTRY.items():
        actions.extend(factory(dict(config.get(name, {}))).evaluate(snapshot))
    return actions


def requeued(actions: list[Action]) -> set[str | None]:
    return {a.task_id for a in actions if a.kind is ActionKind.REQUEUE}


def recommended() -> dict[str, dict[str, str]]:
    return {
        "exit_contract": RECOMMENDED,
        "orphan_status": RECOMMENDED,
        "wait_condition": RECOMMENDED,
    }


def test_only_the_broken_tasks_are_requeued(tmp_path: Path) -> None:
    actions = evaluate_all(build_board(tmp_path), recommended())
    # Negative cases: a typed block, the breaker's park, a crash that the sweep already
    # reset, a review round trip, and live workers with 40-50 minute old heartbeats.
    assert requeued(actions) == {"t_bad_block", "t_gone"}
    reasons = {a.task_id: a.reason for a in actions if a.kind is ActionKind.REQUEUE}
    assert "not an allowed block reason" in reasons["t_bad_block"]
    assert "stopped sending heartbeats" in reasons["t_gone"]


def test_nothing_at_all_is_said_about_the_deliberate_states(tmp_path: Path) -> None:
    actions = evaluate_all(build_board(tmp_path), recommended())
    quiet = {"t_typed", "t_breaker", "t_review", "t_hb", "t_claim", "t_span"}
    assert not [a for a in actions if a.task_id in quiet and a.rule == "exit_contract"]


def test_snapshot_describes_the_board(tmp_path: Path) -> None:
    snap = HermesAdapter(db_path=build_board(tmp_path).db_path, spool=None).snapshot(NOW)
    reasons = {t.id: t.block.reason for t in snap.tasks if t.block}
    assert reasons["t_typed"] == "waiting_human: needs_input: which API version?"
    assert reasons["t_breaker"] == "guard: hermes circuit breaker (3/3): worker pid gone"
    assert reasons["t_bad_block"] == "stuck, not sure why"
    finals = {s.task_id: s.final_status for s in snap.sessions if s.owner_id == "reviewer"}
    assert finals == {"t_review": None}


def test_default_thirty_minute_liveness_would_requeue_a_slow_heartbeat(tmp_path: Path) -> None:
    """Why the README recommends session_stale_after = 60m on Hermes."""
    actions = evaluate_all(build_board(tmp_path), {})
    assert "t_hb" in requeued(actions)
    # A valid claim and a recent tool call keep their workers alive even at the default.
    assert not {"t_claim", "t_span"} & requeued(actions)


def test_engine_observes_without_an_executor_call(tmp_path: Path) -> None:
    adapter = build_board(tmp_path)
    rules = [factory(recommended().get(name, {})) for name, factory in REGISTRY.items()]
    engine = Engine(rules, adapters=[adapter], config=EngineConfig())
    results = engine.tick(lambda: adapter.snapshot(NOW))
    requeues = [d for d in results if d.action.kind is ActionKind.REQUEUE]
    assert {d.action.task_id for d in requeues} == {"t_bad_block", "t_gone"}
    assert all(d.outcome in {Outcome.DRY_RUN, Outcome.OBSERVED} for d in requeues)
