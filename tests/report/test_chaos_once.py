"""A waiting task-hour is counted once in the cost of chaos (#228, incident 20): when rule
13 lists a task as waiting on a person and rule 4 holds a stale block on it at the same
time, the hour counts under the person's inbox, and the stale block's overlapping hours
are shown as "also seen", never added."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kelpie.report.daily import (
    CHAOS_CAUSES,
    CHAOS_PRECEDENCE,
    PEOPLE,
    PREWORK,
    build_report,
    render_markdown,
)
from kelpie.report.store import StateRow
from tests.report.test_daily import at, day_inputs, inbox_sample, opened


def stale(task: str, start: tuple[int, int], end: tuple[int, int]) -> StateRow:
    return opened(f"stale_blocks:{task}:block", "stale_blocks", at(*start), at(*start),
                  at(*end), task)  # fmt: skip


def inbox(end: int = 17) -> StateRow:
    return opened("needs_you:human-owner", "needs_you", at(9), at(9), at(end), "H-1")


def test_a_task_blocked_on_a_person_counts_once_under_the_person() -> None:
    # A-1 waits on the person 09:00-17:00 (8 h); its stale block is open 09:30-17:00 (7.5 h)
    report = build_report(
        day_inputs(states=[inbox(), stale("A-1", (9, 30), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"])], hour_usd=10.0)
    )  # fmt: skip
    lines = {c.rule: c for c in report.chaos}
    assert lines[PEOPLE].hours == pytest.approx(8.0) and lines[PEOPLE].also_hours == 0
    assert lines["stale_blocks"].hours == pytest.approx(0.0)
    assert lines["stale_blocks"].also_hours == pytest.approx(7.5)
    assert lines["stale_blocks"].also_under == (PEOPLE,)
    assert report.chaos_hours == pytest.approx(8.0)
    assert report.chaos_hours == pytest.approx(sum(c.hours for c in report.chaos))
    markdown = render_markdown(report)
    assert "Cost of chaos: 8.0 hours lost" in markdown and "$80.00 in stuck hours" in markdown
    assert "Also seen, not added" in markdown
    assert "`stale_blocks` 7.5 h (counted under `needs_you`)" in markdown


def test_the_part_outside_the_overlap_still_counts_under_its_own_cause() -> None:
    # the stale block outlives the inbox by 2 h: those 2 h are the block's
    report = build_report(
        day_inputs(states=[inbox(end=15), stale("A-1", (13, 0), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"])])
    )  # fmt: skip
    lines = {c.rule: c for c in report.chaos}
    assert lines[PEOPLE].hours == pytest.approx(6.0)
    assert lines["stale_blocks"].hours == pytest.approx(2.0)
    assert lines["stale_blocks"].also_hours == pytest.approx(2.0)
    assert report.chaos_hours == pytest.approx(8.0)  # 09:00-17:00, once


def test_the_live_day_shape_is_not_inflated() -> None:
    # four tasks wait on one person all day; each also has a stale block 10:00-17:35
    tasks = ["T-1", "T-2", "T-3", "T-4"]
    report = build_report(
        day_inputs(
            states=[inbox(end=18), *(stale(t, (10, 0), (17, 35)) for t in tasks)],
            actions=[inbox_sample(at(9), tasks)],
        )
    )
    lines = {c.rule: c for c in report.chaos}
    assert lines[PEOPLE].hours == pytest.approx(4 * 9.0)
    assert lines["stale_blocks"].hours == pytest.approx(0.0)
    assert lines["stale_blocks"].also_hours == pytest.approx(4 * 7 + 4 * 35 / 60)
    assert report.chaos_hours == pytest.approx(36.0)  # was 36 + 30.3 before #228


def test_two_states_of_one_rule_on_one_task_count_once() -> None:
    a = stale("A-1", (9, 0), (13, 0))
    b = opened("stale_blocks:A-1:other", "stale_blocks", at(11), at(11), at(15), "A-1")
    [line] = build_report(day_inputs(states=[a, b])).chaos
    assert line.hours == pytest.approx(6.0) and line.also_hours == 0  # 09:00-15:00


def test_different_tasks_do_not_interact() -> None:
    report = build_report(
        day_inputs(states=[inbox(), stale("B-1", (9, 0), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"])])
    )  # fmt: skip
    lines = {c.rule: c for c in report.chaos}
    assert lines[PEOPLE].hours == pytest.approx(8.0)
    assert lines["stale_blocks"].hours == pytest.approx(8.0)
    assert lines["stale_blocks"].also_hours == 0
    assert "Also seen" not in render_markdown(build_report(day_inputs(states=[inbox()])))


def test_waits_past_the_listed_ids_still_count() -> None:
    # rule 13 counts 25 waiting tasks but lists 20: the 5 it cannot name add up as they are
    sample = inbox_sample(at(9), [f"A-{i}" for i in range(20)])
    counted = sample.params | {"waiting_agent_tasks": 25}
    report = build_report(
        day_inputs(states=[inbox(), stale("A-0", (9, 0), (17, 0))],
                   actions=[replace(sample, params=counted)])
    )  # fmt: skip
    lines = {c.rule: c for c in report.chaos}
    assert lines[PEOPLE].hours == pytest.approx(25 * 8.0)
    assert lines["stale_blocks"].hours == 0 and lines["stale_blocks"].also_hours == 8.0


def test_a_person_comes_first_then_a_stale_block() -> None:
    assert CHAOS_PRECEDENCE[:2] == (PEOPLE, "stale_blocks")
    assert sorted(CHAOS_PRECEDENCE) == sorted(CHAOS_CAUSES)


def test_a_task_behind_two_people_counts_once() -> None:
    lead = opened("needs_you:human-lead", "needs_you", at(9), at(9), at(17), "H-2")
    samples = [inbox_sample(at(9), ["A-1"]), inbox_sample(at(9), ["A-1"], person="human-lead")]
    [line] = build_report(day_inputs(states=[inbox(), lead], actions=samples)).chaos
    assert line.rule == PEOPLE and line.hours == pytest.approx(8.0)  # was 16 before #228


def test_a_wait_on_a_person_and_a_block_on_the_human_task_both_count() -> None:
    # A-1 waits on the person; the human task H-1 itself has a stale block: two tasks
    report = build_report(
        day_inputs(states=[inbox(), stale("H-1", (9, 0), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"])])
    )  # fmt: skip
    assert report.chaos_hours == pytest.approx(16.0)
    assert all(c.also_hours == 0 for c in report.chaos)


def test_an_ownerless_card_comes_before_its_stuck_before_work_line() -> None:
    card = "orphan_status:stuck-before-work:T-5:2026-03-02T06:00:00+00:00"
    stuck = opened(card, "orphan_status", at(6), at(8), at(12), "T-5")
    ownerless = opened("orphan_status:T-5", "orphan_status", at(9), at(9), at(10), "T-5")
    lines = {c.rule: c for c in build_report(day_inputs(states=[stuck, ownerless])).chaos}
    assert lines["orphan_status"].hours == pytest.approx(1.0)
    assert lines[PREWORK].hours == pytest.approx(3.0)
    assert lines[PREWORK].also_hours == pytest.approx(1.0)


def test_a_session_state_counts_on_its_task() -> None:
    session = opened("no_progress:S-1", "no_progress", at(10), at(10), at(14), "T-2")
    lines = {c.rule: c for c in build_report(
        day_inputs(states=[stale("T-2", (9, 0), (17, 0)), session])
    ).chaos}  # fmt: skip
    assert lines["stale_blocks"].hours == pytest.approx(8.0)
    assert lines["no_progress"].hours == 0 and lines["no_progress"].also_hours == 4.0


def test_a_line_whose_hours_were_all_counted_earlier_gets_no_advice() -> None:
    report = build_report(
        day_inputs(states=[inbox(), stale("A-1", (9, 30), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"])])
    )  # fmt: skip
    titles = [a.title for a in report.recommendations]
    assert not any("blocks that outlived their cause" in t for t in titles)
    assert all(a.hours > 0 or a.usd > 0 for a in report.recommendations)


def test_the_root_cause_digest_carries_the_overlap() -> None:
    from kelpie.report.root_cause import digest

    report = build_report(
        day_inputs(states=[inbox(), stale("A-1", (9, 30), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"])])
    )  # fmt: skip
    lost = digest(report).data["hours_lost"]
    assert [x["hours"] for x in lost] == [pytest.approx(8.0)]  # the 0 h line is left out
    partly = build_report(
        day_inputs(states=[inbox(end=15), stale("A-1", (13, 0), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"])])
    )  # fmt: skip
    rows = {x["rule"]: x for x in digest(partly).data["hours_lost"]}
    assert rows["stale_blocks"]["hours"] == pytest.approx(2.0)
    assert rows["stale_blocks"]["also_hours"] == pytest.approx(2.0)


def test_let_a_rule_act_names_its_own_hours() -> None:
    from kelpie.core.actions import ActionKind
    from kelpie.core.config import Mode
    from kelpie.core.ledger import Outcome
    from tests.report.test_daily import entry

    would = entry(at(14), "stale_blocks", ActionKind.COMMENT, Outcome.OBSERVED, "A-1")
    report = build_report(
        day_inputs(states=[inbox(end=15), stale("A-1", (13, 0), (17, 0))],
                   actions=[inbox_sample(at(9), ["A-1"]), would],
                   modes={"stale_blocks": Mode.OBSERVE})
    )  # fmt: skip
    [advice] = [a for a in report.recommendations if a.title == "Let stale_blocks act"]
    assert "lasted 4.0 task-hours" in advice.detail  # its own, 2 of them also a person's
