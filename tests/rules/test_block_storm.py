"""Rule 4, block storms: many tasks blocked on the same reason at once (#229)."""

from __future__ import annotations

from dataclasses import replace
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
    Snooze,
    Status,
    Task,
)
from kelpie.rules._block_storm import _refs, informative, keep_open, normalise
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
        "needs_input: Waiting for CEO (Xcode first launch) [T-1]",
        "NEEDS_INPUT:  waiting for ceo (xcode first launch).",
        "needs_input: waiting for CEO (Xcode first launch) 2026-03-02 10:12",
        "needs_input: waiting for CEO (Xcode first launch) - T-1, a1b2c3d4e5, 20 min",
    ],
)
def test_reasons_differing_in_own_id_case_times_and_hashes_are_one(variant: str) -> None:
    assert normalise(variant, "T-1") == normalise(ASK, "T-2")
    assert _refs(variant, "T-1") == ()


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("waiting for T-100 to merge its schema", "waiting for T-101 to merge its schema"),
        ("disk full on host-1 build cache", "disk full on host-2 build cache"),
        ("quota exhausted for gpt-4 batch jobs", "quota exhausted for gpt-5 batch jobs"),
        ("build fails on ios17 simulator", "build fails on ios18 simulator"),
        ("needs Xcode 15 installed on the mac", "needs Xcode 16 installed on the mac"),
        ("tests fail under python3.11 only", "tests fail under python3.12 only"),
    ],
)
def test_what_a_reason_names_keeps_reasons_apart(a: str, b: str) -> None:
    assert (normalise(a, "T-9"), _refs(a, "T-9")) != (normalise(b, "T-9"), _refs(b, "T-9"))


@pytest.mark.parametrize(
    "reason",
    [
        "label 'blocked'",
        "waiting_human: needs_input",
        "guard: hermes circuit breaker (3/3)",
        "waiting for review",
        "waiting for CI",
        "needs_input: approve",
        "blocked: dependency",
        "blocked",
    ],
)
def test_a_kind_of_wait_alone_is_no_shared_reason(reason: str) -> None:
    assert not informative(normalise(reason))
    assert storms(*(blocked(n, 50 - n, reason) for n in range(6))) == []


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
    assert alert.reason.endswith("T-0, T-1, T-2, T-3, T-4.")  # no one named: no promise
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
    a = tuple(blocked(n, 50 - n, "waiting for design sign-off on example/app#7") for n in range(5))
    b = tuple(
        blocked(n + 10, 50 - n, "waiting for design sign-off on example/app#8") for n in range(5)
    )
    found = storms(*a, *b)
    assert len(found) == 2 and found[0].state != found[1].state


def test_a_storm_half_answered_stays_one_and_does_not_flap() -> None:
    [alert] = storms(*FIVE)
    opened = {"open_states": (alert.state,)}
    assert keep_open(5) == 2 and keep_open(9) == 4
    assert storms(*FIVE[3:], **opened) != []  # three answered: two still wait on it
    assert storms(*FIVE[4:], **opened) == []  # one left: resolved
    assert storms(*FIVE[3:]) == []  # not open: two do not open one
    early = tuple(blocked(n, 300 - 10 * n) for n in range(5))  # 4-5 h ago
    [alert] = storms(*early)
    later = (*early[1:], blocked(5, 10))  # T-0 answered, T-5 blocked hours after the rest
    assert storms(*later) == []  # not within the window...
    [kept] = storms(*later, open_states=(alert.state,))  # ...but still the open storm
    tasks = Outage.from_params(kept.params).tasks  # type: ignore[union-attr]
    assert tasks == ("T-1", "T-2", "T-3", "T-4", "T-5")


def test_tasks_a_person_snoozed_do_not_count() -> None:
    def snooze(task: Task) -> Snooze:
        return Snooze(task.id, NOW + 600 * M, "after the release", "ceo", NOW - 5 * M)

    held = {t.id: snooze(t) for t in FIVE[:2]}
    assert storms(*FIVE, snoozes=held) == []  # three left
    assert storms(*FIVE, blocked(5, 5), blocked(6, 6), snoozes=held) != []


def test_the_person_is_one_the_reason_waits_for() -> None:
    def person(reason: str, *owners: Owner) -> str | None:
        found = storms(*(blocked(n, 50 - n, reason) for n in range(5)), owners=owners)
        return Outage.from_params(found[0].params).person if found else "no storm"  # type: ignore[union-attr]

    alice = Owner("alice-smith", "pm", OwnerKind.HUMAN)
    assert person("needs alice-smith to sign the vendor contract", alice) == "alice-smith"
    assert person("needs alice-smithson to sign the vendor contract", alice) is None
    lead = Owner("person-7", "lead", OwnerKind.HUMAN)
    assert person("lead time on the runner pool is too long", lead) is None  # not an ask
    assert person("ask the lead about the runner pool", lead) == "person-7"
    assert person("lead: restart the runner pool", lead) == "person-7"
    other_lead = Owner("person-8", "lead", OwnerKind.HUMAN)
    assert person("ask the lead about the runner pool", lead, other_lead) is None  # which?


def test_long_storms_and_long_lists() -> None:
    many = tuple(blocked(n, 400 - 20 * n) for n in range(12))
    [alert] = storms(*many, rule=StaleBlocks(storm_window=timedelta(hours=5)))
    assert "within 3.7 h" in alert.reason and "(+2 more)" in alert.reason
    own = tuple(blocked(n, 50 - n, f"{ASK} [T-{n}]") for n in range(5))
    [alert] = storms(*own)
    assert Outage.from_params(alert.params).signature == ASK  # type: ignore[union-attr]


def test_on_a_multi_host_hub_ids_match_as_the_agents_wrote_them() -> None:
    from kelpie.remote.protocol import namespace_snapshot

    own = tuple(blocked(n, 50 - n, f"{ASK} [T-{n}]") for n in range(3))
    others = tuple(blocked(n, 40 - n, ASK) for n in range(3, 6))
    parts = [
        namespace_snapshot(host, "hermes", Snapshot(now=NOW, tasks=tasks, owners=(CEO,)))
        for host, tasks in (("mac1", own), ("mac2", others))
    ]
    hub = Snapshot(
        now=NOW,
        tasks=tuple(t for p in parts for t in p.tasks),
        owners=tuple(o for p in parts for o in p.owners),
    )
    [alert] = [a for a in StaleBlocks().evaluate(hub) if is_outage_state(a.state or "")]
    outage = Outage.from_params(alert.params)
    assert outage is not None and len(outage.tasks) == 6  # own ids stripped on both hosts
    assert outage.person == "mac1:ceo"  # the CEO on both boards is one person


def test_on_a_hub_what_a_reason_names_is_its_own_machine_s() -> None:
    def on(host: str, n: int) -> Task:
        t = blocked(n, 50 - n, "waiting for t_5a01 to ship the schema migration")
        return replace(t, id=f"{host}:{t.id}", host=host)

    mixed = (on("mac1", 0), on("mac1", 1), on("mac2", 2), on("mac2", 3), on("mac2", 4))
    assert storms(*mixed, rule=StaleBlocks(storm_min_tasks=3)) != []  # mac2's three
    assert storms(*mixed) == []  # five tasks, but two different t_5a01s
    lead = Owner("mac1:person-7", "lead", OwnerKind.HUMAN, host="mac1")
    bot = Owner("mac2:agent-lead", "lead", host="mac2")  # another machine's agent
    asks = tuple(
        replace(t, id=f"mac1:{t.id}", host="mac1")
        for t in (blocked(n, 50 - n, "ask the lead about the runner pool") for n in range(5))
    )
    [alert] = storms(*asks, owners=(lead, bot))
    assert Outage.from_params(alert.params).person == "mac1:person-7"  # type: ignore[union-attr]


def test_a_role_agents_hold_too_names_no_one() -> None:
    reviewer = Owner("person-3", "reviewer", OwnerKind.HUMAN)
    bot = Owner("agent-rev", "reviewer")
    asks = tuple(blocked(n, 50 - n, "waiting for reviewer (signing keys)") for n in range(5))
    [alert] = storms(*asks, owners=(reviewer,))
    assert Outage.from_params(alert.params).person == "person-3"  # type: ignore[union-attr]
    [alert] = storms(*asks, owners=(reviewer, bot))
    assert Outage.from_params(alert.params).person is None  # type: ignore[union-attr]
    lead = Owner("person-7", "lead", OwnerKind.HUMAN)
    times = tuple(blocked(n, 50 - n, "needs lead time on the runner pool") for n in range(5))
    [alert] = storms(*times, owners=(lead,))
    assert Outage.from_params(alert.params).person is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "reason", ["waiting_ci: flaky", "merge conflict in CHANGELOG.md", "waiting_pr_merge: queue"]
)
def test_more_kinds_of_wait_alone(reason: str) -> None:
    assert not informative(normalise(reason))


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


def test_the_storm_s_hours_are_never_counted_twice_in_the_cost_of_chaos() -> None:
    """Its tasks' hours count where they count without it: their own states, or rule 13's
    inbox when it takes their blocks. Switched on, it folds their alerts and they open no
    state: those hours wait for the report's attribution by kind (roadmap, #229)."""
    from datetime import date

    from kelpie.core.config import EngineConfig, Mode
    from kelpie.core.engine import Engine
    from kelpie.report.daily import ReportInputs, build_report
    from kelpie.report.store import StateRow
    from kelpie.rules.needs_you import NeedsYou
    from tests.fakes import FakeAdapter, FakeAlerter

    start = datetime(2026, 3, 2, 1, tzinfo=UTC)
    tasks = tuple(
        Task(f"T-{n}", "t", Status.BLOCKED, start, block=Block(ASK, start)) for n in range(5)
    )
    config = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)

    def chaos(*rules: Any) -> dict[str, float]:
        engine = Engine(list(rules), adapters=[FakeAdapter()], alerter=FakeAlerter(), config=config)
        for h in range(9):  # 01:30 to 09:30; the tasks' own alerts from 05:00 (4h)
            now = start + timedelta(hours=h, minutes=30)
            engine.tick(lambda now=now: Snapshot(now=now, tasks=tasks, owners=(CEO,)))
        rows = [StateRow(s) for s in engine.states.store.open_states()]  # type: ignore[attr-defined]
        log = list(engine.ledger.store.entries)  # type: ignore[attr-defined]
        report = build_report(
            ReportInputs(day=date(2026, 3, 2), tz=UTC, actions=log, states=rows, tasks=tasks),
            sections={},
        )
        return {c.rule: c.hours for c in report.chaos if c.hours}

    assert chaos(StaleBlocks()) == {"stale_blocks": pytest.approx(5 * 4)}  # 05:30 to 09:30
    assert chaos(StaleBlocks(storm_mode="rule")) == {}  # folded: the known gap
    inbox = NeedsYou(default_approver="ceo")
    handed = chaos(StaleBlocks(inbox=inbox), inbox)  # rule 13 takes the blocks
    assert set(handed) == {"needs_you"}  # once, not again under the storm
