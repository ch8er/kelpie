from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import Owner, OwnerKind, Return, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.ping_pong import PingPong, build
from tests.fakes import FakeAdapter, FakeAlerter

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def task(
    rounds: int = 0,
    returns: tuple[Return, ...] = (),
    status: Status = Status.IN_PROGRESS,
    tid: str = "T-1",
) -> Task:
    return Task(tid, "t", status, NOW - H, review_rounds=rounds, returns=returns)


def reviews(n: int, category: str = "review") -> tuple[Return, ...]:
    return tuple(Return(NOW - (n - i) * H, f"reason {i}", category) for i in range(n))


def run(*tasks: Task, rule: PingPong | None = None, owners: tuple[Owner, ...] = ()) -> list[Action]:
    return list((rule or PingPong()).evaluate(Snapshot(now=NOW, tasks=tasks, owners=owners)))


def test_registered() -> None:
    assert isinstance(REGISTRY["ping_pong"]({}), PingPong)


@pytest.mark.parametrize(("rounds", "escalated"), [(2, False), (3, True), (4, True)])
def test_threshold_boundary_on_review_rounds(rounds: int, escalated: bool) -> None:
    assert bool(run(task(rounds))) is escalated


def test_rounds_come_from_returns_when_counter_is_missing() -> None:
    [action] = run(task(0, reviews(3)))
    assert action.kind is ActionKind.ESCALATE
    assert action.params["review_rounds"] == 3


def test_counter_wins_when_larger_than_returns() -> None:
    [action] = run(task(9, reviews(2)))
    assert action.params["review_rounds"] == 9


def test_other_return_categories_are_not_review_rounds() -> None:
    assert run(task(0, reviews(5, "merge_conflict"))) == []
    uncategorized = tuple(Return(r.at, r.reason) for r in reviews(5))
    assert run(task(0, uncategorized)) == []


def test_custom_review_categories() -> None:
    rule = PingPong(review_categories=("review", "qa"))
    assert run(task(0, reviews(3, "qa")), rule=rule)


def test_terminal_tasks_are_ignored() -> None:
    for status in (Status.DONE, Status.FAILED, Status.CANCELLED):
        assert run(task(9, status=status)) == []


def test_message_lists_reasons_and_categories() -> None:
    [action] = run(task(0, reviews(3)))
    assert "3 review rounds (limit 3)" in action.reason
    assert "1. reason 0 [review]" in action.reason and "3. reason 2 [review]" in action.reason
    assert [r["reason"] for r in action.params["returns"]] == ["reason 0", "reason 1", "reason 2"]


def test_long_history_is_truncated_in_the_message_but_not_in_params() -> None:
    [action] = run(task(0, reviews(14)))
    assert "(+4 earlier)" in action.reason and "reason 0" not in action.reason
    assert len(action.params["returns"]) == 14


def test_no_history_still_escalates() -> None:
    [action] = run(task(5))
    assert "Return reasons" not in action.reason and action.params["returns"] == []


def test_target_from_config_then_first_human_then_none() -> None:
    owners = (Owner("a-1", "developer"), Owner("h-1", "owner", OwnerKind.HUMAN))
    [a] = run(task(3), rule=PingPong(escalate_to="architect"), owners=owners)
    assert a.params["escalate_to"] == "architect" and "Escalating to architect." in a.reason
    [b] = run(task(3), owners=owners)
    assert b.params["escalate_to"] == "h-1"
    [c] = run(task(3))
    assert c.params["escalate_to"] is None
    assert "no escalation target configured (set escalate_to)" in c.reason.lower()


def test_default_human_target_is_the_lowest_id_whatever_the_order() -> None:
    humans = (Owner("h-2", "owner", OwnerKind.HUMAN), Owner("h-1", "owner", OwnerKind.HUMAN))
    for owners in (humans, humans[::-1]):
        [a] = run(task(3), owners=owners)
        assert a.params["escalate_to"] == "h-1"


def test_escalation_is_a_lasting_state_with_a_stable_key() -> None:
    returns = reviews(4)
    [first] = run(task(4, returns))
    later = Snapshot(now=NOW + 5 * H, tasks=(task(4, returns),))
    [second] = PingPong().evaluate(later)
    assert first.state == second.state == "ping_pong:T-1"
    assert first.key == second.key == "ping_pong:T-1:1"
    # The state began when the limit was crossed (3rd review return), not at this tick.
    assert first.since == returns[2].at


def test_since_falls_back_to_status_change_and_never_future() -> None:
    [action] = run(task(5))
    assert action.since == NOW - H
    future = Task("T-1", "t", Status.REVIEW, NOW + H, review_rounds=5)
    [clamped] = PingPong().evaluate(Snapshot(now=NOW, tasks=(future,)))
    assert clamped.since == NOW


def test_each_task_gets_its_own_state() -> None:
    actions = run(task(3, tid="T-1"), task(3, tid="T-2"), task(1, tid="T-3"))
    assert [a.task_id for a in actions] == ["T-1", "T-2"]
    assert len({a.state for a in actions}) == 2


def test_build_accepts_valid_config() -> None:
    rule = build(
        {
            "max_review_rounds": 5,
            "escalate_to": " architect ",
            "review_categories": ["review", "qa"],
        }
    )
    assert rule == PingPong(5, "architect", ("review", "qa"))


@pytest.mark.parametrize(
    "config",
    [
        {"max_review_rounds": 0},
        {"max_review_rounds": -1},
        {"max_review_rounds": True},
        {"max_review_rounds": "3"},
        {"max_review_rounds": 2.5},
        {"escalate_to": ""},
        {"escalate_to": "  "},
        {"escalate_to": 7},
        {"review_categories": []},
        {"review_categories": "review"},
        {"review_categories": ["review", 3]},
        {"typo": 1},
    ],
)
def test_build_rejects_bad_config(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build(config)


# --- through the engine ------------------------------------------------------------------


def ticks(*rounds: int) -> list[str]:
    """Run one engine tick per round count (10 minutes apart); return what was announced."""
    alerter = FakeAlerter()
    engine = Engine(
        [PingPong(max_review_rounds=3)],
        adapters=[FakeAdapter()],
        alerter=alerter,
        config=EngineConfig(dry_run=False, default_mode=Mode.ACT),
    )
    announced: list[str] = []
    for i, n in enumerate(rounds):
        snapshot = Snapshot(now=NOW + i * 10 * M, tasks=(task(n),))
        for d in engine.tick(lambda s=snapshot: s):
            if d.outcome is Outcome.ALERTED:
                announced.append(f"{n} rounds: {d.action.reason.split(':')[0]}")
    assert len(engine.states.store.open_states()) <= 1  # one state per task, however it grows
    return announced


def test_engine_announces_once_while_the_loop_is_steady() -> None:
    assert len(ticks(3, 3, 3)) == 1


def test_engine_stays_quiet_for_one_more_round() -> None:
    assert len(ticks(3, 4, 5)) == 1


def test_engine_reannounces_once_when_the_loop_doubles() -> None:
    assert len(ticks(3, 4, 6, 7, 8)) == 2
