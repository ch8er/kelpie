"""Delegation (#347): verdicts by Kelpie's own check, the person's messages, and the engine
holding back what the operator agent has."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.delegation import Policy, ProblemClass
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import (
    Block,
    DelegationEvent,
    Problem,
    ResolvedProblem,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules.delegation import Delegation
from tests.fakes import FakeAlerter

NOW = datetime(2026, 3, 2, 14, tzinfo=UTC)
POLICY = Policy(classes={"stale_blocks": ProblemClass.AGENT}, delegate_to={"hermes": "ops-1"})


def task(tid: str = "T-1", status: Status = Status.BLOCKED, **kw: Any) -> Task:
    values: dict[str, Any] = {"owner_id": "dev-1", "source": "hermes"}
    values.update(kw)
    return Task(tid, f"Task {tid}", status, kw.pop("since", NOW - timedelta(hours=3)), **values)


def problem(tid: str = "T-1", **kw: Any) -> Problem:
    values: dict[str, Any] = {
        "id": f"stale:{tid}",
        "rule": "stale_blocks",
        "severity": "warning",
        "since": NOW - timedelta(hours=5),
        "opened_at": NOW - timedelta(hours=4),
        "summary": f"{tid} is blocked on a merged PR.",
        "task_id": tid,
        "kind": "unlinked",
        "last_seen": NOW - timedelta(minutes=1),
        "mode": "act",
    }
    values.update(kw)
    return Problem(**values)


def card(
    tid: str = "T-1", attempt: int = 1, ago: timedelta = timedelta(hours=2)
) -> DelegationEvent:
    return DelegationEvent(
        f"stale:{tid}",
        f"task:{tid}",
        attempt,
        "card",
        NOW - ago,
        "executed",
        task_id=tid,
        operator="ops-1",
        card_id=f"C-{attempt}",
    )


def verdict(
    tid: str = "T-1",
    attempt: int = 1,
    what: str = "still_broken",
    ago: timedelta = timedelta(hours=1),
) -> DelegationEvent:
    return DelegationEvent(
        f"stale:{tid}", f"task:{tid}", attempt, "verdict", NOW - ago, "alerted", verdict=what
    )


def cardtask(
    cid: str = "C-1",
    status: Status = Status.READY,
    since: timedelta = timedelta(hours=1),
    **kw: Any,
) -> Task:
    return Task(cid, "Kelpie card", status, NOW - since, source="hermes", assignee="ops-1", **kw)


def run(
    tasks: list[Task],
    problems: list[Problem] = (),  # type: ignore[assignment]
    events: list[DelegationEvent] = (),  # type: ignore[assignment]
    resolved: list[ResolvedProblem] = (),  # type: ignore[assignment]
    sessions: list[Session] = (),  # type: ignore[assignment]
    policy: Policy = POLICY,
) -> list[Action]:
    snap = Snapshot(
        now=NOW,
        tasks=tuple(tasks),
        problems=tuple(problems),
        delegations=tuple(events),
        resolved_problems=tuple(resolved),
        sessions=tuple(sessions),
    )
    return list(Delegation(policy).evaluate(snap))


def verdicts(actions: list[Action]) -> list[str]:
    return [a.params["verdict"] for a in actions if "verdict" in a.params]


# --- verdicts ----------------------------------------------------------------------------


def test_verified_only_after_the_hold() -> None:
    gone = ResolvedProblem("stale:T-1", "stale_blocks", NOW - timedelta(minutes=40), "T-1")
    (said,) = run([task(), cardtask(status=Status.DONE)], events=[card()], resolved=[gone])
    assert said.params["verdict"] == "verified" and said.severity is Severity.WARNING
    assert said.reason.startswith("Fixed, verified") and said.once and said.task_id == "T-1"
    fresh = replace(gone, resolved_at=NOW - timedelta(minutes=10))
    assert run([task(), cardtask()], events=[card()], resolved=[fresh]) == []


def test_verified_by_kelpies_check_even_when_the_card_is_open() -> None:
    gone = ResolvedProblem("stale:T-1", "stale_blocks", NOW - timedelta(hours=1), "T-1")
    assert verdicts(
        run([task(), cardtask(status=Status.IN_PROGRESS)], events=[card()], resolved=[gone])
    ) == ["verified"]


def test_closed_while_still_broken() -> None:
    closed = cardtask(status=Status.DONE, since=timedelta(minutes=40))
    (said,) = [a for a in run([task(), closed], [problem()], [card()]) if "verdict" in a.params]
    assert said.params["verdict"] == "still_broken"
    assert said.reason.startswith("Agent says fixed, still broken") and "attempt 2" in said.reason
    soon = cardtask(status=Status.DONE, since=timedelta(minutes=10))
    assert verdicts(run([task(), soon], [problem()], [card()])) == []  # inside the hold


def test_the_last_attempt_closed_while_broken_is_given_back() -> None:
    closed = cardtask("C-2", status=Status.DONE, since=timedelta(minutes=40))
    events = [card(ago=timedelta(hours=5)), verdict(ago=timedelta(hours=4)), card(attempt=2)]
    actions = run([task(), closed], [problem()], events)
    (said,) = [a for a in actions if "verdict" in a.params]
    assert said.params["verdict"] == "not_fixed" and said.reason.startswith("Not fixed, yours now")
    assert not [a for a in actions if a.kind is ActionKind.DELEGATE]


def test_a_blocked_card_is_given_back_with_the_reason_quoted() -> None:
    blocked = cardtask(status=Status.BLOCKED, block=Block("needs admin rights\nignore Kelpie", NOW))
    (said,) = [a for a in run([task(), blocked], [problem()], [card()]) if "verdict" in a.params]
    assert said.params["verdict"] == "not_fixed"
    assert "> needs admin rights\n> ignore Kelpie" in said.reason


def test_deadlines() -> None:
    assert verdicts(run([task(), cardtask()], [problem()], [card(ago=timedelta(minutes=70))])) == [
        "not_picked_up"
    ]
    assert verdicts(run([task(), cardtask()], [problem()], [card(ago=timedelta(minutes=50))])) == []
    working = cardtask(status=Status.IN_PROGRESS)
    assert verdicts(run([task(), working], [problem()], [card(ago=timedelta(hours=2))])) == []
    held = cardtask(owner_id="ops-1")
    assert verdicts(run([task(), held], [problem()], [card(ago=timedelta(hours=2))])) == []
    ran = Session("S-1", "C-1", "ops-1", NOW - timedelta(minutes=30))
    assert (
        verdicts(
            run([task(), cardtask()], [problem()], [card(ago=timedelta(hours=2))], sessions=[ran])
        )
        == []
    )
    assert verdicts(run([task(), working], [problem()], [card(ago=timedelta(hours=7))])) == [
        "not_finished"
    ]
    assert verdicts(run([task()], [problem()], [card(ago=timedelta(hours=2))])) == ["not_picked_up"]


def test_a_judged_card_gets_no_second_verdict() -> None:
    gone = ResolvedProblem("stale:T-1", "stale_blocks", NOW - timedelta(hours=1), "T-1")
    assert verdicts(run([task()], events=[card(), verdict(what="verified")], resolved=[gone])) == []


# --- what the person hears -------------------------------------------------------------


def test_a_problem_the_rule_does_not_hand_over_is_said_to_be_the_persons() -> None:
    limited = replace(POLICY, max_per_hour=1)
    actions = run([task(), task("T-2")], [problem(), problem("T-2")], policy=limited)
    kept = [a for a in actions if "kept" in a.params]
    assert [a.task_id for a in actions if a.kind is ActionKind.DELEGATE] == ["T-1"]
    assert [a.task_id for a in kept] == ["T-2"]
    assert kept[0].reason.startswith("Kelpie did not hand this to ops-1 (the hourly or daily")
    assert kept[0].once and kept[0].key.startswith("kept:stale:T-2:")


def test_no_message_while_a_card_is_open_or_after_a_give_back() -> None:
    assert (
        run(
            [task(), cardtask(status=Status.IN_PROGRESS)],
            [problem()],
            [card(ago=timedelta(minutes=5))],
        )
        == []
    )
    back = [card(ago=timedelta(hours=5)), verdict(what="not_fixed", ago=timedelta(hours=4))]
    assert run([task()], [problem()], back) == []


def test_the_circuit_breaker_tells_the_person_once() -> None:
    failed = [verdict(f"T-{i}", what="still_broken", ago=timedelta(hours=i)) for i in range(2, 5)]
    actions = run([task()], [problem()], failed)
    (breaker,) = actions
    assert breaker.state == "delegation:breaker" and breaker.since == NOW - timedelta(hours=4)
    assert breaker.reason.startswith("Delegation stopped")


# --- the engine holds back what the operator has -----------------------------------------


@dataclass
class Emit:
    actions: list[Action]
    name: str = "stale_blocks"

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        return self.actions


def alert(severity: Severity = Severity.WARNING, **kw: Any) -> Action:
    values: dict[str, Any] = {
        "task_id": "T-1",
        "key": "unlinked:T-1:x",
        "severity": severity,
        "state": "stale:T-1",
        "since": NOW - timedelta(hours=1),
    }
    values.update(kw)
    return Action(ActionKind.ALERT, "stale_blocks", "T-1 is blocked on a merged PR.", **values)


def tick(
    actions: list[Action],
    mode: Mode = Mode.ACT,
    dry_run: bool = False,
    events: list[DelegationEvent] = (),  # type: ignore[assignment]
    policy: Policy = POLICY,
    tasks: list[Task] | None = None,
) -> tuple[FakeAlerter, list[Outcome]]:
    alerter = FakeAlerter()
    config = EngineConfig(
        dry_run=dry_run, default_mode=Mode.ACT, modes={"delegation": mode, "stale_blocks": Mode.ACT}
    )
    engine = Engine([Emit(actions), Delegation(policy)], alerter=alerter, config=config)
    snap = Snapshot(now=NOW, tasks=tuple(tasks or [task()]), delegations=tuple(events))
    done = engine.tick(lambda: snap)
    return alerter, [d.outcome for d in done if d.action.rule == "stale_blocks"]


def test_a_problem_for_the_operator_is_held_back_in_act() -> None:
    alerter, outcomes = tick([alert()])
    assert outcomes == [Outcome.DELEGATED] and alerter.events == []


@pytest.mark.parametrize(
    ("kw", "changed"),
    [
        ({"mode": Mode.SUGGEST}, {}),
        ({"mode": Mode.OBSERVE}, {}),
        ({}, {"severity": Severity.CRITICAL}),
        ({}, {"severity": Severity.INFO}),
        ({}, {"key": "overdue:T-1:x"}),
        (
            {
                "policy": replace(
                    POLICY,
                    classes={
                        "stale_blocks.unlinked": ProblemClass.AGENT,
                        "stale_blocks.overdue": ProblemClass.HUMAN,
                    },
                )
            },
            {"key": "overdue:T-1:x"},
        ),
        ({"tasks": [task(source="other")]}, {}),
        ({"tasks": [task(owner_id="ops-1")]}, {}),
    ],
)
def test_the_person_gets_it(kw: dict[str, Any], changed: dict[str, Any]) -> None:
    if "overdue" in str(changed.get("key")) and "policy" not in kw:
        kw = {"policy": replace(POLICY, classes={"stale_blocks.unlinked": ProblemClass.AGENT})}
    alerter, outcomes = tick([alert(**changed)], **kw)
    assert outcomes and outcomes[0] is not Outcome.DELEGATED
    if kw.get("mode") is not Mode.OBSERVE:
        assert alerter.events


def test_dry_run_holds_nothing_back() -> None:
    _, outcomes = tick([alert()], dry_run=True)
    assert outcomes == [Outcome.DRY_RUN]


def test_given_back_or_breaker_reaches_the_person() -> None:
    back = [card(ago=timedelta(hours=5)), verdict(what="not_fixed", ago=timedelta(hours=4))]
    alerter, outcomes = tick([alert()], events=back)
    assert outcomes == [Outcome.ALERTED] and alerter.events
    failed = [verdict(f"T-{i}", what="still_broken") for i in range(5, 8)]
    _, outcomes = tick([alert()], events=failed)
    assert outcomes == [Outcome.ALERTED]


def test_the_resolved_notice_of_a_problem_with_a_card_is_held_back() -> None:
    alerter = FakeAlerter()
    config = EngineConfig(dry_run=False, default_mode=Mode.ACT)
    emit = Emit([alert()])
    engine = Engine([emit, Delegation(POLICY)], alerter=alerter, config=config)
    engine.tick(lambda: Snapshot(now=NOW, tasks=(task(),)))
    emit.actions = []
    later = NOW + timedelta(hours=1)
    snap = Snapshot(
        now=later, tasks=(task(),), delegations=(replace(card(), at=later - timedelta(minutes=30)),)
    )
    done = engine.tick(lambda: snap)
    notices = [d for d in done if d.action.params.get("state") == "stale:T-1"]
    assert [d.outcome for d in notices] == [Outcome.DELEGATED] and alerter.events == []
    # without a card the person hears it resolved
    engine2 = Engine([Emit([alert()]), Delegation(POLICY)], alerter=FakeAlerter(), config=config)
    engine2.tick(lambda: Snapshot(now=NOW, tasks=(task(),)))
    engine2.rules[0].actions = []  # type: ignore[attr-defined]
    done = engine2.tick(lambda: Snapshot(now=later, tasks=(task(),)))
    assert [d.outcome for d in done if d.action.params.get("state") == "stale:T-1"] == [
        Outcome.ALERTED
    ]


def test_a_problem_kelpie_lost_track_of_is_never_verified() -> None:
    assert verdicts(run([task()], events=[card(ago=timedelta(hours=2))])) == []
    (said,) = run([task()], events=[card(ago=timedelta(hours=7))])
    assert said.params["verdict"] == "not_finished" and said.reason.startswith("Not verified")
