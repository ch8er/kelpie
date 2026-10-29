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
from kelpie.core.model import Status, Task
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


def open_tasks(*ids: str, at: datetime) -> tuple[Task, ...]:
    """Tasks on the board, open since ``at``: an outage counts only those."""
    return tuple(Task(t, t, Status.BLOCKED, at, role="developer") for t in ids)


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


def test_a_storm_switched_on_counts_from_then_and_is_listed_apart() -> None:
    """Observed from 09:00, switched on at 11:00 by a config change: the hours count from
    11:00 (not back-filled to 09:00), and before/after lists the storm as a signal the
    change turned on, not as an after-window regression."""
    from kelpie.report.fixes import CONFIG, FixEvent

    fix = datetime.fromisoformat("2026-03-01T11:00:00+00:00")
    seen, until = fix - timedelta(hours=2), fix + timedelta(hours=4)
    observed = OutageParamsFactory.storm(seen, ("T-1",), observe=True)
    counted = OutageParamsFactory.storm(fix, ("T-1",))
    state = row(observed.params["state"], "stale_blocks", seen, seen, until)
    switch_on = FixEvent(fix, CONFIG, "storm_mode = rule", "stale_blocks")
    inputs = ReportInputs(day=fix.date(), tz=UTC, states=(state,), actions=(observed, counted),
                          tasks=open_tasks("T-1", at=seen), fixes=(switch_on,))  # fmt: skip
    report = build_report(inputs)
    [line] = [c for c in report.chaos if c.rule == OUTAGE]
    assert line.hours == pytest.approx(4)  # 11:00-15:00
    assert report.outage_wait_hours == pytest.approx(4)  # the waits split agrees
    [v] = report.verifications
    [left_out] = [e for e in v.excluded if "outage" in e.name]
    assert left_out.first == fix and left_out.after  # first counted when switched on


# --- CI outages in the cost of chaos, precedence, observe-only waits, scope (review) -----


def test_a_ci_outages_tasks_count_once_under_the_outage() -> None:
    from tests.report.test_waits import at, inputs, outage_sample, outage_state

    state = outage_state("ci_economy:outage:0123456789ab", at(2), at(20))
    sample = outage_sample(at(3), repos=("example/app",), tasks=("R1", "R2"))
    block = row("stale_blocks:R1:block", "stale_blocks", at(10), at(10), at(14), "R1")
    report = build_report(inputs(states=(state, block), actions=(sample,)), sections={})
    lines = {c.rule: c for c in report.chaos}
    # its only loaded alert is at 03:00 (the history does not reach the announcement at
    # 02:00): a lower bound, from the first alert loaded
    assert lines[OUTAGE].hours == pytest.approx(2 * 17) and lines[OUTAGE].tasks == ("R1", "R2")
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
    assert ("- CI jobs not starting since 2026-03-02 02:00 UTC, still open: 1 PR(s), 0 alert(s) "
            "folded under it. Cause: jobs queued over 30 min on 3 PRs") in markdown  # fmt: skip


def test_before_after_sees_an_outages_tasks() -> None:
    from kelpie.report.fixes import MANUAL, FixEvent

    fix = datetime.fromisoformat("2026-03-01T08:00:00+00:00")
    storm = OutageParamsFactory.storm(fix + timedelta(hours=1), ("T-1", "T-2"))
    state = row(storm.params["state"], "stale_blocks", fix + timedelta(hours=1),
                fix + timedelta(hours=1), fix + timedelta(hours=5))  # fmt: skip
    report = build_report(ReportInputs(day=fix.date() + timedelta(days=1), tz=UTC,
                                       states=(state,), actions=(storm,),
                                       tasks=open_tasks("T-1", "T-2", at=fix),
                                       fixes=(FixEvent(fix, MANUAL, "a fix"),)))  # fmt: skip
    [v] = report.verifications
    lost = next(m for m in v.metrics if m.name.startswith("hours lost"))
    assert lost.after == pytest.approx(2 * 4)  # two tasks, 4 h, counted like the chaos


def test_an_outage_counts_a_finished_task_until_it_finished() -> None:
    """The last hourly alert may name a task done since: it counts until it finished (one
    done before the outage not at all). A task off the board counts as named: adapters
    drop finished tasks after a while, and the alerts named it while it was open. The
    cost of chaos and before/after agree."""
    from kelpie.report.daily import state_hours
    from kelpie.report.fixes import MANUAL, FixEvent

    fix = datetime.fromisoformat("2026-03-01T08:00:00+00:00")
    at, until = fix + timedelta(hours=1), fix + timedelta(hours=5)
    storm = OutageParamsFactory.storm(at, ("T-1", "T-2", "T-3", "T-4"))
    state = row(storm.params["state"], "stale_blocks", at, at, until)
    board = (
        *open_tasks("T-1", at=fix),
        Task("T-2", "done mid-outage", Status.DONE, at + timedelta(hours=1), role="developer"),
        Task("T-3", "done before it", Status.DONE, fix, role="developer"),
    )  # T-4 is not on the board any more (finished, aged out)
    inputs = ReportInputs(day=fix.date(), tz=UTC, states=(state,), actions=(storm,),
                          tasks=board, fixes=(FixEvent(fix, MANUAL, "a fix"),))  # fmt: skip
    [line] = [c for c in build_report(inputs).chaos if c.rule == OUTAGE]
    assert line.hours == pytest.approx(4 + 1 + 4)  # T-1 and T-4 4 h, T-2 until it was done
    assert state_hours((storm,), state.state, at, until, tasks=board) == pytest.approx(9)
    assert state_hours((storm,), state.state, at, until) == pytest.approx(4 * 4)  # as named


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


# --- before/after scoping, tool failures, kind from the state id (review 2) -------------


def _fix_day(
    fixes: tuple[object, ...], states: tuple[StateRow, ...], actions: tuple[LedgerEntry, ...]
) -> list[object]:
    fix = datetime.fromisoformat("2026-03-01T08:00:00+00:00")
    report = build_report(ReportInputs(day=fix.date() + timedelta(days=1), tz=UTC, states=states,
                                       actions=actions, fixes=fixes,
                                       tasks=open_tasks("T-1", "T-2", "T-9", at=fix)))  # fmt: skip
    [v] = report.verifications
    return [v]


def _storm_and_ci(fix: datetime) -> tuple[tuple[StateRow, ...], tuple[LedgerEntry, ...]]:
    from kelpie.core.incidents import CiOutage

    storm = OutageParamsFactory.storm(fix + timedelta(hours=1), ("T-1", "T-2"))
    ci_sid = "ci_economy:outage:0123456789ab"
    red = CiOutage(state=ci_sid, signature="ci.yml > build", job="build", tasks=("T-9",))
    red_entry = entry(fix + timedelta(hours=1), "ci_economy", red.to_params(), ActionKind.ALERT)
    at, until = fix + timedelta(hours=1), fix + timedelta(hours=5)
    states = (row(storm.params["state"], "stale_blocks", at, at, until),
              row(ci_sid, "ci_economy", at, at, until))  # fmt: skip
    return states, (storm, red_entry)


def _lost(v: object) -> object:
    return next(m for m in v.metrics if m.name.startswith("hours lost"))  # type: ignore[attr-defined]


def test_a_rules_fix_counts_its_own_outages_only() -> None:
    from kelpie.report.fixes import MANUAL, FixEvent

    fix = datetime.fromisoformat("2026-03-01T08:00:00+00:00")
    states, actions = _storm_and_ci(fix)
    [blocks] = _fix_day((FixEvent(fix, MANUAL, "a", "stale_blocks"),), states, actions)
    assert _lost(blocks).name == "hours lost per day (stale_blocks)"
    assert _lost(blocks).after == pytest.approx(2 * 4)  # its storm, not rule 9's outage
    [ci] = _fix_day((FixEvent(fix, MANUAL, "b", "ci_economy"),), states, actions)
    assert _lost(ci).name == "hours lost per day (ci_economy)"  # not "has none"
    assert _lost(ci).after == pytest.approx(1 * 4)


def test_a_storm_switched_on_by_a_config_change_is_listed_apart() -> None:
    from kelpie.report.fixes import CONFIG, FixEvent

    fix = datetime.fromisoformat("2026-03-01T08:00:00+00:00")
    states, actions = _storm_and_ci(fix)
    [v] = _fix_day((FixEvent(fix, CONFIG, "storm_mode = rule", "stale_blocks"),), states, actions)
    [left_out] = [e for e in v.excluded if "outage" in e.name]
    assert left_out.after == pytest.approx(2 * 4)  # its own storm only, not rule 9's outage


def test_a_tool_failure_explains_only_its_tasks_under_its_own_cause() -> None:
    from kelpie.core.incidents import TOOL_FAILURES
    from kelpie.report.waits import SHARED_TOOL_FAILURE, OutageSpan, WaitCauses, outages
    from tests.report.test_waits import at, ci, waiting

    tool = OutageSpan(at(8), at(12), repos=frozenset({"example/app"}), tasks=frozenset({"R1"}),
                      state="loop_breaker:outage:0123456789ab", kind=TOOL_FAILURES)  # fmt: skip
    assert tool.wait_cause == SHARED_TOOL_FAILURE
    causes = WaitCauses((ci("R1"), ci("R2")), (), (), (), [tool])
    assert causes.split(waiting("R1", "reviewer"), at(8), at(12)) == {
        SHARED_TOOL_FAILURE: pytest.approx(4)
    }
    assert causes.split(waiting("R2", "reviewer"), at(8), at(12)) == {"ci": pytest.approx(4)}
    # no alert loaded: the kind still follows the state's rule
    found = outages([row("loop_breaker:outage:0123456789ab", "loop_breaker", at(8), at(8),
                         at(12)).state,
                     row("stale_blocks:outage:0123456789ab", "stale_blocks", at(8), at(8),
                         at(12)).state], [])  # fmt: skip
    assert [o.kind for o in found] == [TOOL_FAILURES, BLOCK_STORM]


def test_a_tool_failure_renders_as_its_kind() -> None:
    from dataclasses import replace

    from kelpie.core.incidents import TOOL_FAILURES
    from kelpie.report.daily import OutageLine

    at = datetime.fromisoformat("2026-03-02T09:00:00+00:00")
    line = OutageLine("web_fetch", at, at + timedelta(hours=2), False, 3, 1, TOOL_FAILURES,
                      reason="HTTP 503 on every call")  # fmt: skip
    report = replace(build_report(storm_day(), sections={}), outages=(line,))
    assert ("- tool failure `web_fetch` since 2026-03-02 09:00 UTC, until 2026-03-02 11:00 UTC: "
            "3 task(s) failing, 1 alert(s) folded under it. Cause: HTTP 503 on every call"
            ) in render_markdown(report)  # fmt: skip


def test_an_outages_kind_without_its_alerts_follows_its_state() -> None:
    from kelpie.core.incidents import CI_FAILURES, CI_NOT_STARTED, CI_NOT_STARTED_STATE
    from kelpie.report.waits import _kind_of

    assert _kind_of(CI_NOT_STARTED_STATE) == CI_NOT_STARTED
    assert _kind_of("ci_economy:outage:0123456789ab") == CI_FAILURES
    assert _kind_of("some_new_rule:outage:0123456789ab") == CI_FAILURES  # the fallback


def test_a_check_switched_back_to_observe_stops_counting() -> None:
    """Counted from 10:00, back in observe at 12:00, open until 18:00: 2 h, in the cost of
    chaos and in the waits split alike."""
    from kelpie.report.waits import counted_spans

    day = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
    h = lambda n: day + timedelta(hours=n)  # noqa: E731
    on, off = (
        OutageParamsFactory.storm(h(10), ("T-1",)),
        OutageParamsFactory.storm(h(12), ("T-1",), observe=True),
    )
    state = row(on.params["state"], "stale_blocks", h(10), h(10), h(18))
    report = build_report(ReportInputs(day=day.date(), tz=UTC, states=(state,), actions=(on, off),
                                       tasks=open_tasks("T-1", at=h(9))), sections={})  # fmt: skip
    [line] = [c for c in report.chaos if c.rule == OUTAGE]
    assert line.hours == pytest.approx(2)
    assert report.outage_wait_hours == pytest.approx(2)
    [listed] = report.outages
    assert not listed.observe_only and listed.back_in_observe == h(12)
    markdown = render_markdown(report)
    assert "(its check back in observe since 2026-03-01 12:00 UTC: its waits counted" in markdown
    assert "not switched on" not in markdown
    # and switched on again: counted again
    assert counted_spans([(h(10), False), (h(12), True), (h(14), False)], h(18)) == (
        (h(10), h(12)),
        (h(14), h(18)),
    )
    assert counted_spans([(h(10), True)], h(18)) == ()


def test_a_storm_holds_a_task_from_the_alert_that_names_it() -> None:
    """A task a later alert names (T-2 from 12:00, blocked since 09:00), and one a later
    alert drops (T-3 after 12:00, still blocked): held only while named, in the cost of
    chaos and in the waits split alike (not back-filled per task)."""
    day = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
    h = lambda n: day + timedelta(hours=n)  # noqa: E731
    first = OutageParamsFactory.storm(h(10), ("T-1", "T-3"))
    later = OutageParamsFactory.storm(h(12), ("T-1", "T-2"))
    state = row(first.params["state"], "stale_blocks", h(10), h(10), h(16))
    report = build_report(ReportInputs(day=day.date(), tz=UTC, states=(state,),
                                       actions=(first, later),
                                       tasks=open_tasks("T-1", "T-2", "T-3", at=h(9))),
                          sections={})  # fmt: skip
    [line] = [c for c in report.chaos if c.rule == OUTAGE]
    assert line.hours == pytest.approx(6 + 4 + 2)  # T-1 10-16, T-2 12-16, T-3 10-12
    assert report.outage_wait_hours == pytest.approx(line.hours)


def test_alerts_at_one_time_an_alert_that_counts_wins() -> None:
    from dataclasses import replace

    from kelpie.report.waits import outage_samples

    at = datetime.fromisoformat("2026-03-01T10:00:00+00:00")
    counted = OutageParamsFactory.storm(at, ("T-1",))
    observed = replace(OutageParamsFactory.storm(at, ("T-1",), observe=True), fingerprint="o")
    for order in ((counted, observed), (observed, counted)):
        assert [obs for _, _, obs in outage_samples(order)] == [True, False]


def test_a_check_switched_on_again_keeps_its_earlier_hours() -> None:
    """On at 10:00, back in observe at 12:00, on again at 16:00: the engine re-opens the
    state then (``opened_at`` 16:00, ``since`` still 10:00). Both spans count, in the
    cost of chaos and in the waits split alike."""
    day = datetime.fromisoformat("2026-03-01T00:00:00+00:00")
    h = lambda n: day + timedelta(hours=n)  # noqa: E731
    alerts = (
        OutageParamsFactory.storm(h(10), ("T-1",)),
        OutageParamsFactory.storm(h(12), ("T-1",), observe=True),
        OutageParamsFactory.storm(h(16), ("T-1",)),
    )
    state = row(alerts[0].params["state"], "stale_blocks", h(10), h(16), h(18))
    report = build_report(ReportInputs(day=day.date(), tz=UTC, states=(state,), actions=alerts,
                                       tasks=open_tasks("T-1", at=h(9))), sections={})  # fmt: skip
    [line] = [c for c in report.chaos if c.rule == OUTAGE]
    assert line.hours == pytest.approx(2 + 2)
    assert report.outage_wait_hours == pytest.approx(4)


def test_a_ci_outage_holds_a_named_task_while_named_and_its_scope_throughout() -> None:
    """A task a CI outage's later alert names (not waiting on CI in its scope) is held from
    that alert on, as the cost of chaos counts it; a CI wait in its repository throughout."""
    from kelpie.core.incidents import CiOutage
    from kelpie.report.waits import CI_OUTAGE, WaitCauses, outages
    from tests.report.test_waits import at, ci, waiting

    sid = "ci_economy:outage:0123456789ab"

    def alert(when: datetime, tasks: tuple[str, ...]) -> LedgerEntry:
        red = CiOutage(state=sid, signature="ci.yml > build", job="build",
                       repos=("example/app",), tasks=tasks)  # fmt: skip
        return entry(when, "ci_economy", red.to_params(), ActionKind.ALERT)

    entries = [alert(at(10), ("R1",)), alert(at(12), ("R1", "N1"))]
    [o] = outages([row(sid, "ci_economy", at(10), at(10), at(16)).state], entries)
    causes = WaitCauses((ci("R1"),), (), (), (), [o])
    assert causes.split(waiting("R1", "reviewer"), at(10), at(16)) == {CI_OUTAGE: pytest.approx(6)}
    assert causes.split(waiting("N1", "reviewer"), at(10), at(16))[CI_OUTAGE] == pytest.approx(4)
