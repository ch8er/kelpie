"""Snoozed waits are deliberate, not chaos (#176): the part of a wait a snooze covers is its
own wait cause, ``snoozed``, listed apart with its task-hours; never the biggest wait, and
never in the cost of chaos."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.budgets import PriceTable
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Snapshot,
    Snooze,
    Status,
    Task,
)
from kelpie.core.states import OpenState
from kelpie.core.storage import SqliteLedgerStore
from kelpie.report.config import ReportSettings
from kelpie.report.daily import DailyReport, ReportInputs, build_report, render_markdown
from kelpie.report.run import ReportSources, load_inputs
from kelpie.report.store import ReportStore, StateRow
from kelpie.report.waits import SNOOZED, WAIT_CAUSES, OutageSpan, WaitCauses
from kelpie.rules import REGISTRY
from kelpie.traces.outputs import OutputSettings
from tests.incidents import load

DAY = date(2026, 10, 19)
H = timedelta(hours=1)
OWNERS = (
    Owner("ceo", "owner", OwnerKind.HUMAN),
    Owner("agent-dev-1", "developer"),
)


def at(hour: float, day: int = 19) -> datetime:
    return datetime(2026, 10, day, tzinfo=UTC) + timedelta(hours=hour)


def snooze(tid: str, created: datetime, until: datetime, **kw: Any) -> Snooze:
    base: dict[str, Any] = {"reason": "deferred to the release", "by": "ceo", "source": "cli"}
    return Snooze(tid, until, created_at=created, **{**base, **kw})


def blocked(tid: str, since: datetime, reason: str = "waiting_human: needs_input: ok?") -> Task:
    return Task(
        tid, f"agent {tid}", Status.BLOCKED, since, role="developer", owner_id="agent-dev-1",
        block=Block(reason, since),
    )  # fmt: skip


def report(**kw: Any) -> DailyReport:
    kw.setdefault("owners", OWNERS)
    return build_report(ReportInputs(day=DAY, tz=UTC, hour_usd=20.0, **kw), sections={})


def test_track_c_probe_a_snoozed_wait_is_no_biggest_wait() -> None:
    """Track C's probe: W1 waits all day without a condition and is snoozed from 02:30. It
    was "Snoozed 21.5" and also "biggest wait: unknown 22.0, register a condition". Now
    the snoozed part is its own cause, so nothing is left to be the biggest wait."""
    w1 = blocked("W1", at(2))
    r = report(tasks=(w1,), snoozes=(snooze("W1", at(2.5), at(0, 21)),))
    assert r.biggest_wait is None
    assert r.snoozed is not None and r.snoozed.hours == pytest.approx(21.5)
    assert r.snoozed.tasks == ("W1",)
    markdown = render_markdown(r)
    assert "Biggest wait" not in markdown
    assert (
        "**Snoozed**: 21.5 task-hours of 1 task(s) waiting on purpose, through 2026-10-20 at "
        "the latest."
    ) in markdown
    assert "- `W1` through 2026-10-20 (deferred to the release, by ceo)\n" in markdown
    assert r.chaos == () and r.chaos_usd == 0


def test_the_next_real_wait_is_the_biggest() -> None:
    w1 = blocked("W1", at(2))
    w2 = blocked("W2", at(14), "waiting for CI")
    ci = Condition("C-2", ConditionKind.CI_STATUS, "W2", {"repo": "example/repo"}, False)
    r = report(
        tasks=(w1, w2),
        conditions=(ci,),
        snoozes=(snooze("W1", at(2.5), at(0, 21)),),
    )
    assert r.biggest_wait is not None
    assert (r.biggest_wait.cause, r.biggest_wait.hours) == ("ci", pytest.approx(10.0))
    assert r.snoozed is not None and r.snoozed.hours == pytest.approx(21.5)


def test_a_wait_on_a_snoozed_human_task_is_snoozed_and_listed_under_it() -> None:
    human = Task("H-1", "Sign the contract", Status.READY, at(0, 15), role="owner", owner_id="ceo")
    waiting = Task(
        "W3", "Integrate", Status.WAITING, at(0, 18), role="developer", owner_id="agent-dev-1"
    )
    cond = Condition("C-3", ConditionKind.HUMAN_TASK, "W3", {"task": "H-1"}, False)
    until = datetime(2026, 10, 25, 9, tzinfo=UTC)
    r = report(tasks=(human, waiting), conditions=(cond,), snoozes=(snooze("H-1", at(6), until),))
    assert r.snoozed is not None and r.snoozed.hours == pytest.approx(18.0)  # from 06:00
    assert r.snoozed.tasks == ("W3",) and r.snoozed.held == {"H-1": ("W3",)}
    markdown = render_markdown(r)
    assert "until 2026-10-25 09:00 UTC at the latest" in markdown
    assert "- `H-1` until 2026-10-25 09:00 UTC (deferred to the release, by ceo): held `W3`" in (
        markdown
    )
    assert r.biggest_wait is not None and r.biggest_wait.cause == "people"  # 00:00-06:00


def test_a_snooze_that_held_nothing_counts_nothing() -> None:
    """The old figure counted every stored snooze; now only waits a snooze covered count."""
    ready = Task("T-9", "nothing waits", Status.READY, at(0, 18), role="developer")
    r = report(
        tasks=(ready,), snoozes=(snooze("T-9", at(1), at(0, 25)), snooze("gone", at(1), at(0, 25)))
    )
    assert r.snoozed is None and "**Snoozed**" not in render_markdown(r)


def test_many_snoozes_are_counted() -> None:
    tasks = tuple(blocked(f"t_{n:02}", at(0, 18)) for n in range(12))
    r = report(tasks=tasks, snoozes=tuple(snooze(t.id, at(1), at(0, 25)) for t in tasks))
    assert r.snoozed is not None and r.snoozed.hours == pytest.approx(12 * 23)
    assert "- and 2 more" in render_markdown(r)


def test_a_snooze_never_takes_a_ci_merge_outage_or_hold_wait() -> None:
    task = Task("W2", "w", Status.BLOCKED, at(0), block=Block("ci", at(0), "C-2"))
    ci = Condition("C-2", ConditionKind.CI_STATUS, "W2", {"repo": "example/repo"}, False)
    outage = OutageSpan(at(4), at(12), repos=frozenset({"example/repo"}))
    causes = WaitCauses([ci], [], ("freeze",), (), [outage], [snooze("W2", at(8), at(16))])
    assert causes.split(task, at(0), at(20)) == pytest.approx({"ci_outage": 8.0, "ci": 12.0})
    held = Task("W2", "w", Status.BLOCKED, at(0), labels=("freeze",))
    assert causes.split(held, at(0), at(20)) == {"hold": 20.0}
    assert WAIT_CAUSES[SNOOZED] == "snoozed (deliberate)"


def escalation(at_: datetime, **params: Any) -> LedgerEntry:
    return LedgerEntry(
        at=at_, fingerprint="f", subject="s", rule="needs_you", kind=ActionKind.ESCALATE,
        mode=Mode.ACT, outcome=Outcome.ALERTED, reason="r",
        params={"escalate_to": "ceo", "approvals": [], "waiting": [], **params},
    )  # fmt: skip


SNOOZED_H1 = snooze("H-1", at(0, 18), at(0, 22), behind_at_snooze=1)


def test_review_case_1_a_deep_task_waiting_on_a_pr_keeps_its_merge_cause() -> None:
    """Rule 13 listed T-9 in ``items[].behind`` of the snoozed H-1, but it waits on a PR
    merge first: the snooze is not its wait (``behind`` is not read any more)."""
    pr = Condition("pr:9", ConditionKind.PR_MERGED, "T-9", {"repo": "example/repo", "pr": 9}, False)
    t9 = Task(
        "T-9", "deep", Status.BLOCKED, at(-5), block=Block("waiting for PR 9", at(-5), "pr:9")
    )
    behind = [{"kind": "human_task", "task_id": "H-1", "behind": ["A-1", "T-9"]}]
    entry = escalation(at(2), waiting=["A-1"], items=behind)
    causes = WaitCauses(
        [pr], [entry], snoozes=[SNOOZED_H1], dependencies=[Dependency("T-9", "A-1")]
    )
    assert causes.split(t9, at(0), at(10)) == {"merge": 10.0}


def test_review_case_2_a_task_rule_13_lists_as_live_is_people_chaos() -> None:
    """H-1's snooze grew: rule 13 lists A-1 in ``waiting`` (a live item) all day, so its
    wait is people chaos, not snoozed."""
    a1 = Task("A-1", "a", Status.BLOCKED, at(-5), block=Block("waiting_human: H-1", at(-5), "hc"))
    hc = Condition("hc", ConditionKind.HUMAN_TASK, "A-1", {"task": "H-1"}, False)
    entry = escalation(at(2), waiting=["A-1"])
    causes = WaitCauses([hc], [entry], snoozes=[SNOOZED_H1])
    assert causes.split(a1, at(0), at(10)) == {"people": 10.0}


def test_review_case_3_a_dependency_waiter_of_a_snoozed_human_task_is_snoozed() -> None:
    """H-1 snoozed all day, in the digest's footer only: its backlog dependent waits on it
    by a dependency on the board (track C's first probe), so its wait is snoozed, not
    unknown."""
    dep = Task("D-1", "dep", Status.BACKLOG, at(-5))
    foot = escalation(at(2), approvals=["X"], waiting=["X"], snoozed=[{"task_id": "H-1"}])
    causes = WaitCauses([], [foot], snoozes=[SNOOZED_H1], dependencies=[Dependency("D-1", "H-1")])
    assert causes.split(dep, at(0), at(10)) == {SNOOZED: 10.0}
    # rule 13's own list of what is parked on a snoozed item is a source too
    listed = escalation(at(2), snoozed=[{"task_id": "H-1", "waiting": ["D-1"]}])
    assert WaitCauses([], [listed], snoozes=[SNOOZED_H1]).split(dep, at(0), at(10)) == {
        SNOOZED: 10.0
    }
    assert WaitCauses([], [foot], snoozes=[SNOOZED_H1]).split(dep, at(0), at(10)) == {
        "unknown": 10.0
    }  # nothing on the board says it waits on H-1


def test_track_c_probe_2_a_ci_wait_behind_a_snoozed_human_task_keeps_ci() -> None:
    w = Task("W-5", "w", Status.BLOCKED, at(-5), block=Block("ci", at(-5), "C-5"))
    ci = Condition("C-5", ConditionKind.CI_STATUS, "W-5", {"repo": "example/repo"}, False)
    listed = escalation(at(2), snoozed=[{"task_id": "H-1", "waiting": ["W-5"]}])
    causes = WaitCauses(
        [ci], [listed], snoozes=[SNOOZED_H1], dependencies=[Dependency("W-5", "H-1")]
    )
    assert causes.split(w, at(0), at(10)) == {"ci": 10.0}


def test_a_grown_snooze_holds_its_waiter_only_until_growth_was_latched() -> None:
    w = Task("W-6", "w", Status.WAITING, at(-5))
    hc = Condition("hc6", ConditionKind.HUMAN_TASK, "W-6", {"task": "H-1"}, False)
    grown = replace(SNOOZED_H1, grown_at=at(4), grown_by=1)
    causes = WaitCauses([hc], [], snoozes=[grown])
    assert causes.split(w, at(0), at(10)) == pytest.approx({SNOOZED: 4.0, "people": 6.0})
    from_the_start = replace(SNOOZED_H1, grown_at=at(-1), grown_by=1)
    assert WaitCauses([hc], [], snoozes=[from_the_start]).split(w, at(0), at(10)) == {
        "people": 10.0
    }


def test_live_listing_follows_rule_13s_escalations_and_its_resolve_notice() -> None:
    """A-1 is live from the first escalation (backfilled to the start), drops out when the
    next escalation no longer lists it, comes back, and the inbox's resolve notice ends it."""
    a1 = Task("A-1", "a", Status.BLOCKED, at(-5), block=Block("waiting_human: H-1", at(-5), "hc"))
    hc = Condition("hc", ConditionKind.HUMAN_TASK, "A-1", {"task": "H-1"}, False)
    notice = LedgerEntry(
        at=at(8), fingerprint="g", subject="s", rule="needs_you", kind=ActionKind.ALERT,
        mode=Mode.ACT, outcome=Outcome.ALERTED, reason="resolved",
        params={"state": "needs_you:ceo"},
    )  # fmt: skip
    entries = [
        escalation(at(2), waiting=["A-1"]),
        escalation(at(4), waiting=["B-1"]),
        escalation(at(6), waiting=["A-1"]),
        notice,
        escalation(at(9), escalate_to="lead", waiting=["A-1"]),  # another person's inbox
    ]
    lead = OpenState("needs_you:lead", "needs_you", at(9), at(9), at(12), 0,
                     Severity.WARNING, "s")  # fmt: skip
    causes = WaitCauses(
        [hc], entries, snoozes=[snooze("H-1", at(0, 18), at(0, 22))], inboxes=[lead]
    )
    # live 00-04 (backfilled: the CEO's inbox was open before), not 04-06, live 06-08, not
    # 08-09, live 09- (the lead's inbox, which opened at 09:00: no backfill before that)
    assert causes.split(a1, at(0), at(12)) == pytest.approx({SNOOZED: 3.0, "people": 9.0})


def test_rule_13_people_line_leaves_the_snoozed_wait_out() -> None:
    """The contract end to end: rule 13 on the snoozed-wait fixture, as the ledger stores its
    escalation: the CEO's snoozed question is in no waiting count, so the people line counts
    only the question nobody snoozed; the snoozed one is listed apart."""
    fixture = load(Path(__file__).parents[1] / "fixtures" / "incidents" / "13-snoozed-wait.yaml")
    now = fixture.snapshot.now
    (escalation,) = REGISTRY["needs_you"](fixture.config).evaluate(fixture.snapshot)
    assert escalation.state is not None
    opened = now - 4 * H
    state = OpenState(escalation.state, "needs_you", opened, opened, now, 0,
                      escalation.severity, "s")  # fmt: skip
    entry = LedgerEntry(
        at=opened, fingerprint="k", subject="s", rule="needs_you", kind=escalation.kind,
        mode=Mode.ACT, outcome=Outcome.ALERTED, reason=escalation.reason,
        params=dict(escalation.params),
    )  # fmt: skip
    r = report(
        states=(StateRow(state),),
        actions=(entry,),
        tasks=fixture.snapshot.tasks,
        owners=fixture.snapshot.owners,
        snoozes=tuple(fixture.snapshot.snoozes.values()),
    )
    [people] = r.chaos
    assert people.tasks == ("t_dns7a8b9",) and people.hours == pytest.approx(4.0)
    assert r.snoozed is not None and set(r.snoozed.tasks) == {"t_r2a1b2c3", "t_qa4d5e6f"}


def test_the_report_reads_the_stored_snoozes(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    store = SqliteLedgerStore(db)
    try:
        store.put_snooze(snooze("t_r2", at(12, 18), at(0, 26)))
        store.put_snooze(snooze("t_old", at(1, 10), at(0, 17)))
    finally:
        store.close()
    with ReportStore(db) as read:
        assert [s.task_id for s in read.snoozes(at(0))] == ["t_r2"]
    sources = ReportSources(db, ReportSettings(), PriceTable(), OutputSettings(), False, {})
    board = Snapshot(at(0, 20), (blocked("t_r2", at(0, 18)),), OWNERS)
    inputs = load_inputs(sources, DAY, board)
    assert [s.task_id for s in inputs.snoozes] == ["t_r2"]
    found = build_report(inputs, sections={}).snoozed
    assert found is not None and found.hours == pytest.approx(24.0)


def test_the_day_boundary_counts_no_hour_twice() -> None:
    """The reviewer's report3 case 1: yesterday's last escalation (23:30) lists A-1 live,
    H-1 is snoozed at 00:05, today's first escalation (00:40) no longer lists A-1. The
    people line carries yesterday's sample to 00:40; so must the live listing, so that
    snoozed and people chaos add up to the wait, not more."""
    from kelpie.report.daily import people_agent_hours

    a1 = Task("A-1", "a", Status.BACKLOG, at(-30))
    held = snooze("H-1", at(0) + 5 * timedelta(minutes=1), at(72), behind_at_snooze=1)
    inbox = OpenState("needs_you:ceo", "needs_you", at(-4), at(-4), at(10), 0,
                      Severity.WARNING, "s")  # fmt: skip
    yesterday = escalation(at(-0.5), waiting=["A-1"], waiting_agent_tasks=1)
    today = escalation(
        at(40 / 60), waiting=[], waiting_agent_tasks=0,
        snoozed=[{"task_id": "H-1", "waiting": ["A-1"]}],
    )  # fmt: skip
    entries = [yesterday, today]
    causes = WaitCauses(
        [], [today], snoozes=[held], dependencies=[Dependency("A-1", "H-1")], inboxes=[inbox],
        history=entries,
    )  # fmt: skip
    split = causes.split(a1, at(0), at(2))
    chaos, _ = people_agent_hours(entries, inbox, at(0), at(2))
    assert split[SNOOZED] == pytest.approx(2 - 40 / 60)
    assert chaos == pytest.approx(40 / 60)
    assert split[SNOOZED] + chaos == pytest.approx(2.0)
    # without the earlier entries the first sample of the day was back-filled: a double
    day_only = WaitCauses([], [today], snoozes=[held], dependencies=[Dependency("A-1", "H-1")])
    assert day_only.split(a1, at(0), at(2))[SNOOZED] + chaos > 2.0
