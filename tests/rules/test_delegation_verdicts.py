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
        delegation_read=True,
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
    held = [replace(card(t), kind="held", attempt=0, outcome="delegated") for t in ("T-1", "T-2")]
    actions = run([task(), task("T-2")], [problem(), problem("T-2")], held, policy=limited)
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
    snap = Snapshot(
        now=NOW, tasks=tuple(tasks or [task()]), delegations=tuple(events), delegation_read=True
    )
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
    engine.tick(lambda: Snapshot(now=NOW, tasks=(task(),), delegation_read=True))
    emit.actions = []
    later = NOW + timedelta(hours=1)
    snap = Snapshot(
        now=later,
        tasks=(task(),),
        delegations=(replace(card(), at=later - timedelta(minutes=30)),),
        delegation_read=True,
    )
    done = engine.tick(lambda: snap)
    notices = [d for d in done if d.action.params.get("state") == "stale:T-1"]
    assert [d.outcome for d in notices] == [Outcome.DELEGATED] and alerter.events == []
    # without a card the person hears it resolved
    engine2 = Engine([Emit([alert()]), Delegation(POLICY)], alerter=FakeAlerter(), config=config)
    engine2.tick(lambda: Snapshot(now=NOW, tasks=(task(),), delegation_read=True))
    engine2.rules[0].actions = []  # type: ignore[attr-defined]
    done = engine2.tick(lambda: Snapshot(now=later, tasks=(task(),), delegation_read=True))
    assert [d.outcome for d in done if d.action.params.get("state") == "stale:T-1"] == [
        Outcome.ALERTED
    ]


def test_a_problem_kelpie_lost_track_of_is_never_verified() -> None:
    assert verdicts(run([task()], events=[card(ago=timedelta(hours=2))])) == []
    (said,) = run([task()], events=[card(ago=timedelta(hours=7))])
    assert said.params["verdict"] == "not_finished" and said.reason.startswith("Not verified")


def test_nothing_is_held_back_without_the_history() -> None:
    alerter = FakeAlerter()
    config = EngineConfig(dry_run=False, default_mode=Mode.ACT)
    engine = Engine([Emit([alert()]), Delegation(POLICY)], alerter=alerter, config=config)
    done = engine.tick(lambda: Snapshot(now=NOW, tasks=(task(),)))  # delegation_read False
    assert [d.outcome for d in done if d.action.rule == "stale_blocks"] == [Outcome.ALERTED]


def test_a_problem_held_and_told_is_held_no_more() -> None:
    kept = DelegationEvent(
        "stale:T-1", "task:T-1", 0, "verdict", NOW - timedelta(minutes=5), "alerted", verdict="kept"
    )
    _, outcomes = tick([alert()], events=[kept])
    assert outcomes == [Outcome.ALERTED]
    held = replace(card(), kind="held", attempt=0, outcome="delegated")
    assert run([task()], [problem()], [held, kept]) == []  # told once already


# --- across ticks: every held alert ends in a message to the person ---------------------


@dataclass
class Board:
    """A platform adapter that makes cards."""

    name: str = "hermes"
    made: int = 0
    actions = frozenset({ActionKind.DELEGATE})

    def delegate(self, task_id: str, operator: str, title: str, body: str, key: str) -> str:
        self.made += 1
        return f"C-{self.made}"

    def list_tasks(self) -> list[Task]:
        return []

    def get_task(self, task_id: str) -> None:
        return None

    def requeue(self, task_id: str, reason: str) -> None: ...

    def unblock(self, task_id: str, reason: str) -> None: ...

    def comment(self, task_id: str, body: str) -> None: ...

    def wip_commit(self, task_id: str, message: str, **kw: object) -> None: ...

    def events(self, since: datetime) -> list[object]:
        return []


class Farm:
    """An engine with the delegation rule, its history filled as kelpie serve fills it."""

    def __init__(self) -> None:
        from kelpie.core.ledger import MemoryLedgerStore
        from kelpie.core.states import MemoryStateStore

        self.emit = Emit([alert()])
        self.board = Board()
        self.alerter = FakeAlerter()
        self.ledger = MemoryLedgerStore()
        self.states = MemoryStateStore()
        self.engine = Engine(
            [self.emit, Delegation(POLICY)],
            adapters=[self.board],  # type: ignore[list-item]
            alerter=self.alerter,
            config=EngineConfig(dry_run=False, default_mode=Mode.ACT),
            ledger_store=self.ledger,
            state_store=self.states,
        )

    def tick(self, at: datetime, tasks: list[Task]) -> None:
        from kelpie.core.delegation import history

        entries = [
            e
            for e in self.ledger.since(at - timedelta(days=30))
            if e.rule == "delegation" or e.outcome is Outcome.DELEGATED
        ]
        problems, resolved, events = history(self.states.open_states(), self.states.closed, entries)
        snap = Snapshot(
            now=at,
            tasks=tuple(tasks),
            problems=problems,
            resolved_problems=resolved,
            delegations=events,
            delegation_read=True,
        )
        self.engine.tick(lambda: snap)

    @property
    def told(self) -> list[str]:
        return [e.reason for e in self.alerter.events]


def test_hand_off_then_verified_two_messages() -> None:
    farm = Farm()
    farm.tick(NOW, [task()])
    assert farm.told == []  # held for ops-1
    farm.tick(NOW + timedelta(minutes=1), [task()])
    assert farm.board.made == 1 and len(farm.told) == 1  # the hand-off
    assert "ops-1" in farm.told[0]
    farm.emit.actions = []  # the block is gone
    done = cardtask("C-1", status=Status.DONE, since=timedelta(minutes=-5))
    for minutes in (5, 20, 40, 60):
        farm.tick(NOW + timedelta(minutes=minutes), [task(), done])
    assert len(farm.told) == 2 and farm.told[1].startswith("Fixed, verified")


def test_a_person_taking_the_task_gets_it_back_in_one_message() -> None:
    from kelpie.core.model import Owner, OwnerKind

    alice = Owner("alice", "ceo", OwnerKind.HUMAN)
    taken = task(owner_id="alice")
    farm2 = Farm()
    farm2.tick(NOW, [task()])
    from kelpie.core.delegation import history

    entries = [e for e in farm2.ledger.since(NOW - timedelta(days=1))]
    problems, _, events = history(farm2.states.open_states(), farm2.states.closed, entries)
    snap = Snapshot(
        now=NOW + timedelta(minutes=1),
        tasks=(taken,),
        owners=(alice,),
        problems=problems,
        delegations=events,
        delegation_read=True,
    )
    farm2.engine.tick(lambda: snap)
    assert farm2.board.made == 0
    assert len(farm2.told) == 1 and farm2.told[0].startswith("Kelpie did not hand this to ops-1")


def test_the_breaker_tells_the_person_about_each_held_problem() -> None:
    failed = [verdict(f"T-{i}", what="still_broken", ago=timedelta(hours=i)) for i in range(2, 5)]
    held = replace(card(), kind="held", attempt=0, outcome="delegated")
    actions = run([task()], [problem()], [*failed, held])
    kept = [a for a in actions if "kept" in a.params]
    assert [a.params["kept"] for a in kept] == ["delegation is stopped by the circuit breaker"]
    assert Delegation(POLICY).boards("delegation:breaker") is False
    assert Delegation(POLICY).boards("stale:T-1") is True
