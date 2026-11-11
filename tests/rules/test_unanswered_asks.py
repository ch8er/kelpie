from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome, Suppression, subject
from kelpie.core.model import (
    Block,
    Comment,
    HostSleep,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Snooze,
    Status,
    Task,
)
from kelpie.rules import PLANNED, REGISTRY
from kelpie.rules.unanswered_asks import UnansweredAsks, build, open_asks
from tests.fakes import FakeAdapter, FakeAlerter

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)

OWNERS = (
    Owner("dev-1", "developer"),
    Owner("arch-1", "architect"),
    Owner("person", "owner", OwnerKind.HUMAN),
)


def ask(
    text: str = "@arch-1 v3 or v4?", at: datetime = NOW - 2 * H, author: str = "dev-1"
) -> Comment:
    return Comment(author, text, at)


def task(
    *comments: Comment,
    tid: str = "T-1",
    status: Status = Status.IN_PROGRESS,
    block: Block | None = None,
    host: str | None = None,
) -> Task:
    return Task(
        tid, "t", status, NOW - 5 * H, owner_id="dev-1", comments=comments, block=block, host=host
    )


def run(
    *tasks: Task,
    sessions: tuple[Session, ...] = (),
    owners: tuple[Owner, ...] = OWNERS,
    rule: UnansweredAsks | None = None,
    **extra: object,
) -> list[Action]:
    snap = Snapshot(now=NOW, tasks=tasks, owners=owners, sessions=sessions, **extra)  # type: ignore[arg-type]
    return list((rule or UnansweredAsks()).evaluate(snap))


def test_registered_and_planned() -> None:
    assert isinstance(REGISTRY["unanswered_asks"]({}), UnansweredAsks)
    assert "unanswered_asks" in {r.name for r in PLANNED}


def test_idle_recipient_is_reminded() -> None:
    [a] = run(task(ask()))
    assert a.task_id == "T-1"
    assert a.severity is Severity.INFO
    assert a.params["recipient"] == "arch-1"
    assert a.params["delivery"] == "unknown"
    assert a.params["step"] == 1
    assert a.once and a.once_per_key
    assert "delivery unknown" in a.reason
    assert a.params["needed"].startswith("arch-1 to answer")


def test_later_steps_warn() -> None:
    [a] = run(task(ask(at=NOW - 13 * H)))
    assert a.params["step"] == 3
    assert a.severity is Severity.WARNING


def test_below_first_step_and_past_stop_after() -> None:
    assert run(task(ask(at=NOW - 30 * M))) == []
    assert run(task(ask(at=NOW - timedelta(days=3)))) == []


def test_busy_recipient_waits() -> None:
    live = Session("S-1", "T-9", "arch-1", NOW - H, last_heartbeat=NOW - 5 * M)
    assert run(task(ask()), sessions=(live,)) == []
    stale = Session("S-1", "T-9", "arch-1", NOW - 3 * H, last_heartbeat=NOW - 2 * H)
    assert len(run(task(ask()), sessions=(stale,))) == 1


def test_answers() -> None:
    reply = Comment("ARCH-1", "v4", NOW - H)
    assert run(task(ask(), reply)) == []
    person = Comment("person", "use v4", NOW - H, human=True)
    assert run(task(ask(), person)) == []
    elsewhere = task(Comment("arch-1", "@dev-1 v4.", NOW - H), tid="T-2")
    assert run(task(ask()), elsewhere) == []
    # an answer before the ask answers nothing
    early = task(Comment("arch-1", "@dev-1 v4.", NOW - 3 * H), tid="T-2")
    assert len(run(task(ask()), early)) == 1
    # an answer, then a new question: the new one is open
    again = ask("@arch-1 and the index?", at=NOW - 90 * M)
    [a] = run(task(ask(at=NOW - 3 * H), Comment("arch-1", "v4", NOW - 2 * H), again))
    assert a.params["asked_at"] == (NOW - 90 * M).isoformat()


def test_one_obligation_per_pair_from_its_first_ask() -> None:
    [a] = run(task(ask(at=NOW - 3 * H), ask("@arch-1 still: v3 or v4?", at=NOW - H)))
    assert a.params["asked_at"] == (NOW - 3 * H).isoformat()


def test_what_is_no_ask() -> None:
    assert run(task(ask("@arch-1 v4 it is."))) == []  # no question
    assert run(task(ask("which version?"))) == []  # nobody mentioned
    assert run(task(ask("@person is Friday fine?"))) == []  # a person: rule 13
    assert run(task(ask("@stranger is Friday fine?"))) == []  # not an owner
    assert run(task(ask("@dev-1 note to self?"))) == []  # oneself
    assert run(task(ask("mail arch@arch-1 ok?"))) == []  # not a mention
    assert run(task(Comment("person", "@arch-1 v4?", NOW - 2 * H, human=True))) == []
    assert run(task(ask(), status=Status.DONE)) == []
    assert run(task(ask()), owners=(Owner("p", "o", OwnerKind.HUMAN),)) == []


def test_mention_forms() -> None:
    assert len(run(task(ask("@Arch-1: v3 or v4?")))) == 1
    prefixed = (Owner("dev-1", "developer"), Owner("host-a:arch-1", "architect"))
    [a] = run(task(ask()), owners=prefixed)
    assert a.params["recipient"] == "host-a:arch-1"


def test_recipient_on_another_machine_is_no_ask() -> None:
    owners = (Owner("dev-1", "developer"), Owner("arch-1", "architect", host="host-b"))
    assert run(task(ask(), host="host-a"), owners=owners) == []


def test_blocked_and_waiting_tasks_are_left_to_other_rules() -> None:
    block = Block("waiting_human: which version?", NOW - 2 * H)
    assert run(task(ask(), status=Status.BLOCKED, block=block)) == []
    assert run(task(ask(), status=Status.WAITING)) == []
    # a stale block left on a task in review that asks a person: rule 13's
    assert run(task(ask(), status=Status.REVIEW, block=block)) == []
    # a block that asks no person does not hide the ask
    other = Block("ci flaky", NOW - 2 * H)
    assert len(run(task(ask(), status=Status.REVIEW, block=other))) == 1


def test_delivery_seen() -> None:
    read = Session("S-1", "T-1", "arch-1", NOW - 90 * M, ended_at=NOW - H)
    [a] = run(task(ask()), sessions=(read,))
    assert a.params["delivery"] == "seen"
    assert "has had a run on the task" in a.reason
    before = Session("S-1", "T-1", "arch-1", NOW - 3 * H, ended_at=NOW - 150 * M)
    [b] = run(task(ask()), sessions=(before,))
    assert b.params["delivery"] == "unknown"


def test_snoozes() -> None:
    held = Snooze("T-1", NOW + H, "later", "person", NOW - H)
    assert run(task(ask()), snoozes={"T-1": held}) == []
    expired = Snooze("T-1", NOW - 10 * M, "later", "person", NOW - H)
    [a] = run(task(ask()), snoozes={"T-1": expired})
    assert a.params["snooze"]["state"] == "expired"
    assert "snooze expired" in a.reason


def test_host_sleep_does_not_age_the_ask() -> None:
    sleep = HostSleep(NOW - 2 * H, NOW - 30 * M)
    assert run(task(ask()), host_sleeps=(sleep,)) == []


def test_long_question_is_capped() -> None:
    [a] = run(task(ask("@arch-1 " + "x" * 500 + "?")))
    assert len(a.params["question"]) == 300
    assert a.params["question"].endswith("…")


def test_open_asks_without_agents() -> None:
    snap = Snapshot(now=NOW, tasks=(task(ask()),), owners=())
    assert open_asks(snap) == []


def test_build_validation() -> None:
    rule = build({"steps": ["30m", "2h"], "stop_after": "1d", "session_stale_after": "1h"})
    assert rule.steps == (30 * M, 2 * H)
    assert rule.stop_after == 24 * H
    assert rule.session_stale_after == H
    bad: list[dict[str, object]] = [
        {"steps": []},
        {"steps": "1h"},
        {"steps": ["2h", "1h"]},
        {"steps": ["0m"]},
        {"steps": ["soon"]},
        {"stop_after": "12h"},
        {"stop_after": "7d"},
        {"session_stale_after": "-1m"},
        {"other": 1},
    ]
    for config in bad:
        with pytest.raises(ValueError):
            build(config)


def test_one_reminder_per_step_across_ticks() -> None:
    """Busy across the first step, idle in the second: one reminder per step, not one per
    tick, and none while busy."""
    alerter = FakeAlerter()
    engine = Engine(
        [UnansweredAsks()],
        adapters=[FakeAdapter()],
        alerter=alerter,
        config=EngineConfig(dry_run=False, default_mode=Mode.SUGGEST),
    )
    asked = NOW - 2 * H
    alerted: list[int] = []
    for minutes, busy in ((0, False), (10, False), (60, True), (150, True), (150 + 10, False)):
        now = NOW + minutes * M
        sessions = (Session("S-1", "T-9", "arch-1", now - M, last_heartbeat=now),) if busy else ()
        snap = Snapshot(now=now, tasks=(task(ask(at=asked)),), owners=OWNERS, sessions=sessions)
        for d in engine.tick(lambda s=snap: s):
            if d.outcome is Outcome.ALERTED:
                alerted.append(d.action.params["step"])
    assert alerted == [1, 2]


def test_multi_host_agents_sharing_names() -> None:
    owners = (
        Owner("host-a:dev-1", "developer", host="host-a"),
        Owner("host-a:arch-1", "architect", host="host-a"),
        Owner("host-b:dev-1", "developer", host="host-b"),
        Owner("host-b:arch-1", "architect", host="host-b"),
    )
    [a] = run(task(ask(), host="host-b"), owners=owners)
    assert a.params["recipient"] == "host-b:arch-1"
    # host-b's arch-1 answering its dev-1 does not answer host-a's question
    answer_b = task(Comment("arch-1", "@dev-1 v4.", NOW - H), tid="T-2", host="host-b")
    [b] = run(task(ask(), host="host-a"), answer_b, owners=owners)
    assert b.params["recipient"] == "host-a:arch-1"
    answer_a = task(Comment("arch-1", "@dev-1 v4.", NOW - H), tid="T-2", host="host-a")
    assert run(task(ask(), host="host-a"), answer_a, owners=owners) == []
    # an explicit host must be the task's own
    assert run(task(ask("@host-b:arch-1 v3?"), host="host-a"), owners=owners) == []
    [c] = run(task(ask("@HOST-A:arch-1 v3?"), host="host-a"), owners=owners)
    assert c.params["recipient"] == "host-a:arch-1"


def test_wall_clock_stop_when_the_host_slept() -> None:
    asked = NOW - timedelta(days=6, hours=1)
    sleep = HostSleep(asked + H, NOW - 2 * H)  # awake age: 3h
    assert run(task(ask(at=asked)), host_sleeps=(sleep,)) == []


def test_an_older_mention_elsewhere_does_not_hide_a_newer_answer() -> None:
    """The recipient answered on T-3 after the ask; an older mention of the asker on T-2,
    read later, must not make the ask look unanswered."""
    newer = task(Comment("arch-1", "@dev-1 v4.", NOW - H), tid="T-3")
    older = task(Comment("arch-1", "@dev-1 earlier note", NOW - 3 * H), tid="T-2")
    assert run(task(ask()), newer, older) == []


BROADCAST_OWNERS = OWNERS + tuple(Owner(f"dev-{i}", "developer") for i in range(2, 9))
BROADCAST = "@dev-2 @dev-3 @dev-4 @dev-5 @dev-6 @dev-7 @dev-8 anyone?"


def test_reminders_have_a_budget_of_their_own() -> None:
    actions = run(task(ask(BROADCAST)), owners=BROADCAST_OWNERS)
    assert len(actions) == 7
    assert all(a.own_budget for a in actions)
    assert {subject(a) for a in actions} == {"rule:unanswered_asks:task:T-1"}


def test_a_broadcast_question_cannot_suppress_another_rules_action() -> None:
    """One comment mentioning seven agents gives seven reminders on the task; a requeue of
    that task five minutes later is not rate-limited by them (#390)."""

    @dataclass(frozen=True)
    class Requeue:
        name: str = "requeue_rule"

        def evaluate(self, snapshot: Snapshot) -> list[Action]:
            if snapshot.now == NOW:
                return []  # asks first, the requeue on the next tick
            return [Action(ActionKind.REQUEUE, self.name, "stuck", task_id="T-1")]

    alerter = FakeAlerter()
    engine = Engine(
        [UnansweredAsks(), Requeue()],
        adapters=[FakeAdapter()],
        alerter=alerter,
        config=EngineConfig(dry_run=False, default_mode=Mode.SUGGEST),
    )
    snap = Snapshot(now=NOW, tasks=(task(ask(BROADCAST)),), owners=BROADCAST_OWNERS)
    first = engine.tick(lambda: snap)
    alerted = [d for d in first if d.outcome is Outcome.ALERTED]
    limited = [d for d in first if d.suppression is Suppression.RATE_LIMIT]
    assert len(alerted) == 6 and len(limited) == 1  # the reminders' own budget: 6/hour
    (notice,) = [e.reason for e in alerter.events if "Rate limit" in e.reason]
    assert "unanswered_asks alerts on task T-1" in notice
    assert "other actions on the task are not" in notice
    later = replace(snap, now=NOW + 5 * M)
    (requeue,) = [d for d in engine.tick(lambda: later) if d.action.rule == "requeue_rule"]
    assert requeue.outcome is Outcome.SUGGESTED
