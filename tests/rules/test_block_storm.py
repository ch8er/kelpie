"""Rule 4, block storms: many tasks blocked on the same reason at once (#229)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action
from kelpie.core.incidents import BLOCK_STORM, Outage, is_outage_state
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    KelpieMark,
    Owner,
    OwnerKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules._block_storm import normalise
from kelpie.rules.stale_blocks import StaleBlocks, build

NOW = datetime(2026, 3, 2, 11, tzinfo=UTC)
M = timedelta(minutes=1)
ASK = "needs_input: waiting for CEO (Xcode first launch)"
CEO = Owner("ceo", "ceo", OwnerKind.HUMAN)


def blocked(n: int, minutes_ago: float, reason: str = ASK, **block: Any) -> Task:
    since = NOW - minutes_ago * M
    return Task(f"T-{n}", f"task {n}", Status.BLOCKED, since, block=Block(reason, since, **block))


def storms(*tasks: Task, rule: StaleBlocks | None = None, **snap: Any) -> list[Action]:
    snapshot = Snapshot(now=NOW, tasks=tasks, owners=snap.pop("owners", (CEO,)), **snap)
    found = (rule or StaleBlocks()).evaluate(snapshot)
    return [a for a in found if is_outage_state(a.state or "")]


FIVE = tuple(blocked(n, 60 - 10 * n) for n in range(5))


@pytest.mark.parametrize(
    "variant",
    [
        "needs_input: Waiting for CEO (Xcode first launch) [t_5a0c2e71]",
        "NEEDS_INPUT:  waiting for ceo (xcode first launch).",
        "needs_input: waiting for CEO (Xcode first launch) since 2026-03-02 10:12",
        "needs_input: waiting for CEO (Xcode first launch) - T-17, a1b2c3d4e5",
    ],
)
def test_reasons_differing_in_ids_case_and_times_are_one(variant: str) -> None:
    extra = variant.endswith("10:12")  # "since" is a word: that one stays different
    assert (normalise(variant) == normalise(ASK)) is not extra
    assert normalise("re-add the deadbeef cache") == "re add the deadbeef cache"  # no digit


def test_five_tasks_blocked_on_the_same_question_are_one_outage() -> None:
    [alert] = storms(*FIVE, blocked(9, 300))  # T-9 was blocked hours before the storm
    outage = Outage.from_params(alert.params)
    assert outage is not None and outage.kind == BLOCK_STORM and alert.state == outage.state
    assert outage.tasks == ("T-0", "T-1", "T-2", "T-3", "T-4") and outage.person == "ceo"
    assert alert.since == NOW - 60 * M and alert.key == alert.state
    assert outage.cause == f'5 tasks blocked within 40 min on "{ASK}"'
    assert "Block storm since 2026-03-02 10:00 UTC" in alert.reason
    assert "Only ceo can end it" in alert.reason
    assert (
        alert.params["headline"]
        == "Block storm: 5 tasks blocked on the same reason, waiting for ceo"
    )


def test_a_role_names_the_person_and_no_one_named_is_fine() -> None:
    lead = Owner("person-7", "lead", OwnerKind.HUMAN)
    asks_lead = tuple(
        blocked(n, 50 - n, "waiting for the lead to restart the runner") for n in range(5)
    )
    [alert] = storms(*asks_lead, owners=(lead,))
    assert Outage.from_params(alert.params).person == "person-7"  # type: ignore[union-attr]
    plain = tuple(blocked(n, 50 - n, "simulator will not boot") for n in range(5))
    [alert] = storms(*plain)
    assert Outage.from_params(alert.params).person is None  # type: ignore[union-attr]
    assert "One fix unblocks all of them." in alert.reason
    agent = Owner("ceo", "ceo")  # an agent named like a person is no person
    [alert] = storms(*FIVE, owners=(agent,))
    assert Outage.from_params(alert.params).person is None  # type: ignore[union-attr]


def test_not_a_storm() -> None:
    assert storms(*FIVE[:4]) == []  # four tasks
    spread = tuple(blocked(n, 600 - 130 * n) for n in range(5))
    assert storms(*spread) == []  # not within the window
    questions = (
        "needs_input: CEO to approve the pricing page copy",
        "needs_input: CEO to pick the release date",
        "needs_input: CEO sign-off on the legal text",
    )
    different = tuple(blocked(n, 30, q) for n, q in enumerate(questions))
    assert storms(*FIVE[:2], *different) == []  # the same person, different questions
    own_prs = tuple(blocked(n, 30 - n, f"waiting for CI on example/api#{40 + n}") for n in range(5))
    assert storms(*own_prs) == []  # each names its own PR
    assert storms(*FIVE, rule=StaleBlocks(storm_min_tasks=0)) == []


def test_waits_kelpie_can_check_and_kelpie_s_own_stops_do_not_count() -> None:
    cond = tuple(
        Condition(f"C-{n}", ConditionKind.CI_STATUS, f"T-{n}", {"repo": "example/app", "pr": 1})
        for n in range(5)
    )
    checked = tuple(blocked(n, 50 - n, condition_id=f"C-{n}") for n in range(5))
    assert storms(*checked, conditions=cond) == []
    unseen = tuple(blocked(n, 50 - n, condition_id=f"C-missing-{n}") for n in range(5))
    assert storms(*unseen) == []  # names a condition Kelpie cannot see: broken, not a storm
    stops = tuple(blocked(n, 50 - n, kelpie=KelpieMark.STOP) for n in range(5))
    assert storms(*stops) == []
    working = (*FIVE[:4], Task("T-4", "t", Status.IN_PROGRESS, NOW, block=FIVE[4].block))
    assert storms(*working) == []  # only blocked tasks


def test_it_resolves_once_enough_are_unblocked() -> None:
    assert storms(*FIVE) != []
    assert storms(*FIVE[1:]) == []  # one answered: four left, below the threshold


def test_a_storm_naming_different_prs_is_two() -> None:
    a = tuple(blocked(n, 50 - n, "waiting for review of example/app#7") for n in range(5))
    b = tuple(blocked(n + 10, 50 - n, "waiting for review of example/app#8") for n in range(5))
    found = storms(*a, *b)
    assert len(found) == 2 and found[0].state != found[1].state


def test_config() -> None:
    rule = build({"storm_min_tasks": 3, "storm_window": "30m"})
    assert rule.storm_min_tasks == 3 and rule.storm_window == 30 * M
    assert storms(*FIVE[2:], rule=rule) != []  # 3 tasks within 20 minutes
    for bad in (1, -1, True, 2.5, 1001):
        with pytest.raises(ValueError, match="storm_min_tasks"):
            build({"storm_min_tasks": bad})
    with pytest.raises(ValueError, match="storm_window"):
        build({"storm_window": "0s"})
    assert build({"storm_mode": "rule"}).storm_mode == "rule"
    assert StaleBlocks().storm_mode == "observe"
    with pytest.raises(ValueError, match="storm_mode"):
        build({"storm_mode": "act"})


def test_with_the_engine_the_storm_folds_the_per_task_alerts_once_switched_on() -> None:
    from kelpie.core.config import EngineConfig, Mode
    from kelpie.core.engine import Engine
    from kelpie.core.incidents import FOLDED_UNDER
    from kelpie.core.ledger import Outcome, Suppression
    from tests.fakes import FakeAdapter, FakeAlerter

    old = tuple(blocked(n, 300 - n) for n in range(5))  # past unlinked_alert_after (4h)
    other = blocked(9, 300, "simulator will not boot")
    snapshot = Snapshot(now=NOW, tasks=(*old, other), owners=(CEO,))
    config = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)

    def tick(rule: StaleBlocks) -> list[Any]:
        engine = Engine([rule], adapters=[FakeAdapter()], alerter=FakeAlerter(), config=config)  # type: ignore[list-item]
        return engine.tick(lambda: snapshot)

    # A new check: by default the storm is only logged, and hides no alert people get.
    done = tick(StaleBlocks())
    [storm] = [d for d in done if is_outage_state(d.action.state or "")]
    assert storm.outcome is Outcome.OBSERVED
    assert not [d for d in done if d.suppression is Suppression.INCIDENT]
    # Switched on, it follows the rule's mode and folds the tasks' own alerts.
    done = tick(StaleBlocks(storm_mode="rule"))
    [storm] = [d for d in done if is_outage_state(d.action.state or "")]
    assert storm.outcome is Outcome.ALERTED
    folded = {d.action.task_id for d in done if d.suppression is Suppression.INCIDENT}
    assert folded == {f"T-{n}" for n in range(5)}
    assert all(
        d.action.params[FOLDED_UNDER] == storm.action.state
        for d in done
        if d.suppression is Suppression.INCIDENT
    )
    assert {d.action.task_id for d in done if d.outcome is Outcome.ALERTED} == {None, "T-9"}
