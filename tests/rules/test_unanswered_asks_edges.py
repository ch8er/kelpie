"""Edges of ``unanswered_asks`` that mutation testing found untested: one task left out
must not end the scan of the others, thresholds are inclusive where the docs say "from",
and host sleeps count for the recipient's liveness too."""

from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.actions import Action
from kelpie.core.model import (
    Block,
    Comment,
    HostSleep,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules.unanswered_asks import MAX_QUESTION, MAX_STOP_AFTER, UnansweredAsks, _local

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)
OWNERS = (
    Owner("dev-1", "developer"),
    Owner("arch-1", "architect"),
    Owner("qa-1", "qa"),
    Owner("person", "owner", OwnerKind.HUMAN),
)


def task(tid: str, *comments: Comment, **kw: Any) -> Task:
    return Task(
        tid,
        "t",
        kw.pop("status", Status.IN_PROGRESS),
        NOW - 9 * H,
        owner_id="dev-1",
        comments=comments,
        **kw,
    )


def ask(at: datetime, text: str = "@arch-1 v3 or v4?", author: str = "dev-1") -> Comment:
    return Comment(author, text, at)


def run(*tasks: Task, rule: UnansweredAsks | None = None, **extra: object) -> list[Action]:
    owners = extra.pop("owners", OWNERS)
    snap = Snapshot(now=NOW, tasks=tasks, owners=owners, **extra)
    return list((rule or UnansweredAsks()).evaluate(snap))


def test_a_task_left_out_does_not_end_the_scan() -> None:
    """Each reason to skip one ask (too young, too old, left to rule 13, a person on the
    block) skips that ask only: a later task's ask is still reminded."""
    skipped = [
        task("T-1", ask(NOW - 10 * M)),  # too young
        task("T-2", ask(NOW - 7 * 24 * H)),  # past stop_after
        task("T-3", ask(NOW - 2 * H), block=Block("needs_input: which API version?", NOW - H)),
        task("T-4", ask(NOW - 2 * H), status=Status.DONE),  # terminal
        task("T-5"),  # no comments
        task("T-6", Comment("person", "@arch-1 thoughts?", NOW - 2 * H, human=True)),
        task("T-7", ask(NOW - 2 * H, "@arch-1 no question mark")),
    ]
    actions = run(*skipped, task("T-9", ask(NOW - 2 * H)))
    assert [a.task_id for a in actions] == ["T-9"]


def test_a_non_ask_comment_does_not_end_the_task() -> None:
    """A comment without a question on the task is skipped, not the rest of the task."""
    t = task("T-1", Comment("dev-1", "status update", NOW - 3 * H), ask(NOW - 2 * H))
    assert [a.params["recipient"] for a in run(t)] == ["arch-1"]


def test_a_mention_of_no_agent_does_not_end_the_comment() -> None:
    t = task("T-1", ask(NOW - 2 * H, "@nobody @arch-1 which one?"))
    assert [a.params["recipient"] for a in run(t)] == ["arch-1"]


def test_the_first_step_is_reached_at_its_age() -> None:
    assert len(run(task("T-1", ask(NOW - H)))) == 1
    assert run(task("T-1", ask(NOW - H + M))) == []


def test_a_step_is_counted_from_its_age() -> None:
    [a] = run(task("T-1", ask(NOW - 4 * H)))
    assert a.params["step"] == 2


def test_six_days_of_wall_clock_end_it_exactly() -> None:
    """A host that slept most of the time: awake age under stop_after, wall clock at the
    cap: at six days the rule stops (the configuration table: "this old")."""
    asked = NOW - MAX_STOP_AFTER
    sleep = HostSleep(asked + H, NOW - 2 * H)
    assert run(task("T-1", ask(asked)), host_sleeps=(sleep,)) == []
    assert len(run(task("T-1", ask(asked + M)), host_sleeps=(sleep,))) == 1


def test_a_reply_at_the_same_moment_is_not_an_answer() -> None:
    """Only a later comment answers; one written in the same second does not."""
    at = NOW - 2 * H
    other = task("T-2", Comment("arch-1", "@dev-1 see T-1", at))
    assert len(run(task("T-1", ask(at)), other)) == 1


def test_a_run_started_at_the_ask_is_not_seen() -> None:
    at = NOW - 2 * H
    run_at_ask = Session("S-1", "T-1", "arch-1", at, ended_at=at + 10 * M)
    [a] = run(task("T-1", ask(at)), sessions=(run_at_ask,))
    assert a.params["delivery"] == "unknown"


def test_a_recipient_whose_host_slept_is_still_busy() -> None:
    """The recipient's session was last heard before its host slept: awake time since is
    short, so it is mid-run, not idle, and is not interrupted."""
    session = Session("S-1", "T-8", "arch-1", NOW - 3 * H, last_heartbeat=NOW - 50 * M)
    sleep = HostSleep(NOW - 45 * M, NOW - 5 * M)
    assert run(task("T-1", ask(NOW - 3 * H)), sessions=(session,), host_sleeps=(sleep,)) == []
    assert len(run(task("T-1", ask(NOW - 3 * H)), sessions=(session,))) == 1


def test_since_is_when_it_was_asked() -> None:
    at = NOW - 2 * H
    [a] = run(task("T-1", ask(at)))
    assert a.since == at


def test_open_hours_have_one_decimal() -> None:
    [a] = run(task("T-1", ask(NOW - 2 * H - 9 * M)))
    assert a.params["open_hours"] == 2.1


def test_a_question_of_the_limit_is_quoted_whole() -> None:
    text = "@arch-1 " + "x" * (MAX_QUESTION - 9) + "?"
    assert len(text) == MAX_QUESTION
    [a] = run(task("T-1", ask(NOW - 2 * H, text)))
    assert a.params["question"] == text


def test_local_names_drop_only_the_host() -> None:
    assert _local("hub:host-a:dev-1") == "dev-1"
    assert _local("Host-A:Dev-1") == "dev-1"


def test_reminders_come_in_order_of_asking() -> None:
    t = task("T-1", ask(NOW - 2 * H, "@qa-1 tests?"), ask(NOW - 3 * H, "@arch-1 v3?"))
    assert [a.params["recipient"] for a in run(t)] == ["arch-1", "qa-1"]


def test_a_task_past_six_days_does_not_end_the_scan() -> None:
    asked = NOW - MAX_STOP_AFTER
    sleep = HostSleep(asked + H, NOW - 2 * H)
    actions = run(task("T-1", ask(asked)), task("T-2", ask(NOW - 2 * H)), host_sleeps=(sleep,))
    assert [a.task_id for a in actions] == ["T-2"]


def test_an_ask_after_a_person_spoke_still_counts() -> None:
    """A person's comment settles what was asked before it, not what is asked after."""
    t = task(
        "T-1",
        ask(NOW - 4 * H),
        Comment("person", "use v4", NOW - 3 * H, human=True),
        ask(NOW - 2 * H, "@arch-1 and the schema?"),
    )
    [a] = run(t)
    assert a.params["question"] == "@arch-1 and the schema?"


def test_a_mention_of_oneself_does_not_end_the_comment() -> None:
    t = task("T-1", ask(NOW - 2 * H, "@aaa @dev-1 @arch-1 which one?"))
    assert [a.params["recipient"] for a in run(t)] == ["arch-1"]


def test_a_host_qualified_mention_skips_the_other_hosts_agent() -> None:
    owners = (
        Owner("dev-1", "developer"),
        Owner("host-a:arch-1", "architect", host="host-a"),
        Owner("host-b:arch-1", "architect", host="host-b"),
    )
    t = task("T-1", ask(NOW - 2 * H, "@host-b:arch-1 which one?"))
    assert [a.params["recipient"] for a in run(t, owners=owners)] == ["host-b:arch-1"]
