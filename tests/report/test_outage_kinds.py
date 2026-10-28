"""Outages of every kind in the report (#229, incident 20): a block storm's waits count once,
under the outage, ahead of the person its reason names and the stale blocks of its tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.incidents import BLOCK_STORM, is_outage_state
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.states import OpenState
from kelpie.report.daily import OUTAGE, PEOPLE, ReportInputs, build_report, render_markdown
from kelpie.report.store import StateRow
from kelpie.report.waits import SHARED_BLOCKER, WAIT_CAUSES
from kelpie.rules import REGISTRY
from tests.incidents import load

FIXTURE = Path(__file__).parents[1] / "fixtures" / "incidents" / "20-block-storm.yaml"
STORM_TASKS = [f"t_5a0c2e7{i}" for i in range(7)]


def entry(at: datetime, rule: str, params: dict[str, object], kind: ActionKind) -> LedgerEntry:
    return LedgerEntry(at=at, fingerprint=f"{rule}-{at:%H%M}", subject="s", rule=rule,
                       kind=kind, mode=Mode.OBSERVE, outcome=Outcome.OBSERVED, reason="r",
                       params=params)  # fmt: skip


def row(sid: str, rule: str, since: datetime, opened: datetime, last: datetime,
        task: str | None = None) -> StateRow:  # fmt: skip
    return StateRow(OpenState(sid, rule, since, opened, last, 0, Severity.WARNING, "s", task))


def storm_day() -> ReportInputs:
    """The 2026-10-17 shape with the storm check switched on: rule 4's storm (its own alert
    on the fixture), plus what the live farm also had: the seven tasks in the person's inbox
    and a stale block on each."""
    incident = load(FIXTURE)
    now = incident.snapshot.now  # 11:00, the storm's first sighting
    [storm] = [
        a
        for a in REGISTRY["stale_blocks"](incident.config).evaluate(incident.snapshot)
        if a.state is not None and is_outage_state(a.state)
    ]
    assert storm.params["kind"] == BLOCK_STORM and storm.params["tasks"] == STORM_TASKS
    until = now + timedelta(hours=6)  # still blocked at 17:00
    since = datetime.fromisoformat("2026-03-02T10:00:00+00:00")
    states = [
        row(storm.state or "", "stale_blocks", since, now, until),
        row("needs_you:ceo", PEOPLE, since, since, until, "H-1"),
        *(row(f"stale_blocks:{t}:block", "stale_blocks", now, now, until, t) for t in STORM_TASKS),
    ]
    actions = [
        entry(now, "stale_blocks", dict(storm.params), ActionKind.ALERT),
        entry(since, PEOPLE, {"escalate_to": "ceo", "waiting_agent_tasks": 7,
                              "waiting": STORM_TASKS}, ActionKind.ESCALATE),
    ]  # fmt: skip
    return ReportInputs(day=now.astimezone(UTC).date(), tz=UTC, states=tuple(states),
                        actions=tuple(actions), tasks=incident.snapshot.tasks,
                        owners=incident.snapshot.owners, hour_usd=10.0)  # fmt: skip


def test_a_block_storms_hours_count_once_under_the_outage() -> None:
    report = build_report(storm_day(), sections={})
    lines = {c.rule: c for c in report.chaos}
    # 11:00-17:00, seven tasks: the outage's; 10:00-11:00 before it opened: the person's
    assert lines[OUTAGE].hours == pytest.approx(7 * 6)
    assert set(lines[OUTAGE].tasks) == set(STORM_TASKS)
    assert lines[PEOPLE].hours == pytest.approx(7 * 1)
    assert lines[PEOPLE].also_hours == pytest.approx(7 * 6)
    assert lines["stale_blocks"].hours == 0
    assert lines["stale_blocks"].also_hours == pytest.approx(7 * 6)
    assert report.chaos_hours == pytest.approx(49)  # once; the live report summed all three
    markdown = render_markdown(report)
    assert "Cost of chaos: 49.0 hours lost" in markdown


def test_the_outages_list_names_the_storm_and_who_can_end_it() -> None:
    report = build_report(storm_day(), sections={})
    [line] = report.outages
    assert line.kind == BLOCK_STORM and line.person == "ceo" and line.scope == 7 and line.open
    markdown = render_markdown(report)
    assert "**Outages**" in markdown
    assert (
        "- block storm `needs_input: waiting for CEO (Xcode first launch)` since 2026-03-02 "
        "10:00 UTC, still open: 7 task(s) blocked on it, 0 alert(s) folded under it; only "
        "ceo can end it"
    ) in markdown


def test_a_storm_explains_only_the_tasks_it_names() -> None:
    from kelpie.report.daily import day_bounds, wait_causes

    inputs = storm_day()
    start, end = day_bounds(inputs.day, UTC)
    causes = wait_causes(inputs, start, end)
    by_id = {t.id: t for t in inputs.tasks}
    at = datetime.fromisoformat("2026-03-02T11:00:00+00:00")
    stormed = causes.split(by_id["t_5a0c2e70"], at, at + timedelta(hours=2))
    assert stormed == {SHARED_BLOCKER: pytest.approx(2.0)}
    other = causes.split(by_id["t_7b1d0001"], at, at + timedelta(hours=2))  # same person
    assert SHARED_BLOCKER not in other
    assert "shared blocker" in WAIT_CAUSES[SHARED_BLOCKER]


def test_a_storm_still_in_observe_is_listed_not_counted() -> None:
    from dataclasses import replace

    from kelpie.core.ledger import OBSERVE_ONLY

    inputs = storm_day()
    trial = tuple(
        replace(e, params={**e.params, OBSERVE_ONLY: True}) if e.rule == "stale_blocks" else e
        for e in inputs.actions
    )
    report = build_report(replace(inputs, actions=trial), sections={})
    lines = {c.rule: c for c in report.chaos}
    assert OUTAGE not in lines  # switching the check on is what moves the hours
    assert lines[PEOPLE].hours == pytest.approx(7 * 7)  # 10:00-17:00, the person's
    [line] = report.outages
    assert line.observe_only
    assert "(observe only: its check is not switched on" in render_markdown(report)


# --- CI outages in the cost of chaos, precedence, observe-only waits, scope (review) -----


def test_a_ci_outages_tasks_count_once_under_the_outage() -> None:
    from tests.report.test_waits import at, inputs, outage_sample, outage_state

    state = outage_state("ci_economy:outage:0123456789ab", at(2), at(20))
    sample = outage_sample(at(3), repos=("example/app",), tasks=("R1", "R2"))
    block = row("stale_blocks:R1:block", "stale_blocks", at(10), at(10), at(14), "R1")
    report = build_report(inputs(states=(state, block), actions=(sample,)), sections={})
    lines = {c.rule: c for c in report.chaos}
    assert lines[OUTAGE].hours == pytest.approx(2 * 18) and lines[OUTAGE].tasks == ("R1", "R2")
    assert lines["stale_blocks"].hours == 0 and lines["stale_blocks"].also_hours == 4.0
    titles = [a.title for a in report.recommendations]
    assert "Reduce farm outages (one shared cause)" in titles
    assert not any(t.startswith("Cut the") for t in titles)  # one item for one outage
    [advice] = [a for a in report.recommendations if a.title.startswith("Reduce farm outages")]
    assert "switch on" not in advice.detail  # its check is on: that is why it counts
    assert "- CI failure `ci.yml > build > Select toolchain` since" in render_markdown(report)


def test_a_storm_comes_before_a_ci_outage_and_both_before_a_snooze() -> None:
    from kelpie.core.model import Snooze
    from kelpie.report.waits import CI_OUTAGE, OutageSpan, WaitCauses
    from tests.report.test_waits import at, ci, waiting

    storm = OutageSpan(at(10), at(12), tasks=frozenset({"R1"}), state="s", kind=BLOCK_STORM)
    red = OutageSpan(at(8), at(14), repos=frozenset({"example/app"}), state="c")
    snooze = Snooze("R1", at(20), None, None, at(6))
    causes = WaitCauses((ci("R1"),), (), (), (), [storm, red], snoozes=[snooze])
    split = causes.split(waiting("R1", "reviewer"), at(6), at(16))
    assert split == {
        SHARED_BLOCKER: pytest.approx(2),
        CI_OUTAGE: pytest.approx(4),
        "ci": pytest.approx(4),
    }  # fmt: skip  (a CI wait is never snoozed)


def test_an_observe_only_outage_moves_no_wait() -> None:
    from dataclasses import replace

    from kelpie.core.ledger import OBSERVE_ONLY
    from tests.report.test_waits import at, inputs, outage_sample, outage_state

    state = outage_state("ci_economy:outage:0123456789ab", at(2), at(20))
    sample = outage_sample(at(3), repos=("example/app",), tasks=("R1",))
    trial = replace(sample, params={**sample.params, OBSERVE_ONLY: True})
    live = build_report(inputs(states=(state,), actions=(sample,)), sections={})
    observed = build_report(inputs(states=(state,), actions=(trial,)), sections={})
    assert live.outage_wait_hours > 0 and observed.outage_wait_hours == 0
    assert observed.biggest_wait is not None and observed.biggest_wait.cause == "ci"
    assert [o.observe_only for o in observed.outages] == [True]
    # a live CI outage next to a storm still in observe: only the live one moves waits
    storm = OutageParamsFactory.storm(at(4), ("R2",), observe=True)
    mixed = build_report(
        inputs(states=(state, row(storm.params["state"], "stale_blocks", at(4), at(4), at(20))),
               actions=(sample, storm)), sections={})  # fmt: skip
    assert mixed.outage_wait_hours == pytest.approx(live.outage_wait_hours)
    assert {o.observe_only for o in mixed.outages} == {True, False}


def test_a_storms_scope_that_day_comes_from_its_samples() -> None:
    from kelpie.report.waits import OutageSpan, outage_day

    day = datetime.fromisoformat("2026-03-02T00:00:00+00:00")
    before = OutageParamsFactory.storm(day - timedelta(hours=2), ("A", "B", "C"))
    later = OutageParamsFactory.storm(day + timedelta(hours=3), ("A", "B"))  # C unblocked
    storm = OutageSpan(day - timedelta(hours=3), day + timedelta(hours=6),
                       state=before.params["state"], kind=BLOCK_STORM)  # fmt: skip
    scope, folded = outage_day(storm, [before, later], day, day + timedelta(days=1))
    assert (scope, folded) == (3, 0)  # its scope at midnight (A, B, C), then A and B


def test_ci_jobs_not_starting_render_as_their_kind() -> None:
    from kelpie.core.incidents import CI_NOT_STARTED, CiOutage
    from kelpie.rules._ci_not_started import STATE
    from tests.report.test_waits import at, inputs, ledger

    o = CiOutage(state=STATE, signature="CI jobs not starting", kind=CI_NOT_STARTED,
                 cause="jobs queued over 30 min on 3 PRs", prs=("example/app#1",))  # fmt: skip
    from dataclasses import replace

    sample = replace(ledger("ci_economy", **o.to_params()), at=at(3))
    state = row(STATE, "ci_economy", at(2), at(2), at(20))
    markdown = render_markdown(
        build_report(inputs(states=(state,), actions=(sample,)), sections={})
    )
    assert ("- CI jobs not starting `CI jobs not starting` (jobs queued over 30 min on 3 PRs) "
            "since 2026-03-02 02:00 UTC") in markdown  # fmt: skip
    assert "1 PR(s)" in markdown


def test_before_after_sees_an_outages_tasks() -> None:
    from kelpie.report.fixes import MANUAL, FixEvent

    fix = datetime.fromisoformat("2026-03-01T08:00:00+00:00")
    storm = OutageParamsFactory.storm(fix + timedelta(hours=1), ("T-1", "T-2"))
    state = row(storm.params["state"], "stale_blocks", fix + timedelta(hours=1),
                fix + timedelta(hours=1), fix + timedelta(hours=5))  # fmt: skip
    report = build_report(ReportInputs(day=fix.date() + timedelta(days=1), tz=UTC,
                                       states=(state,), actions=(storm,),
                                       fixes=(FixEvent(fix, MANUAL, "a fix"),)))  # fmt: skip
    [v] = report.verifications
    lost = next(m for m in v.metrics if m.name.startswith("hours lost"))
    assert lost.after == pytest.approx(2 * 4)  # two tasks, 4 h, counted like the chaos


class OutageParamsFactory:
    """Ledger entries of a block storm's alert, as rule 4 writes them."""

    @staticmethod
    def storm(at: datetime, tasks: tuple[str, ...], observe: bool = False) -> LedgerEntry:
        from kelpie.core.incidents import Outage, incident_state_id
        from kelpie.core.ledger import OBSERVE_ONLY

        sid = incident_state_id("stale_blocks", "waiting for ceo")
        o = Outage(state=sid, signature="waiting for ceo", kind=BLOCK_STORM, tasks=tasks,
                   person="ceo", cause='blocked "waiting for ceo"')  # fmt: skip
        params = {**o.to_params(), **({OBSERVE_ONLY: True} if observe else {})}
        return entry(at, "stale_blocks", params, ActionKind.ALERT)
