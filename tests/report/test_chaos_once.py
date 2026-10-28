"""A waiting task-hour is counted once in the cost of chaos (#228, incident 20): when rule
13 lists a task as waiting on a person and rule 4 holds a stale block on it at the same
time, the hour counts under the person's inbox, and the stale block's overlapping hours
are shown as "also seen", never added."""

from __future__ import annotations

import pytest

from kelpie.report.daily import CHAOS_PRECEDENCE, PEOPLE, build_report, render_markdown
from tests.report.test_daily import at, day_inputs, inbox_sample, opened


def stale(task: str, start: tuple[int, int], end: tuple[int, int]) -> object:
    return opened(f"stale_blocks:{task}:block", "stale_blocks", at(*start), at(*start),
                  at(*end), task)  # fmt: skip


def inbox(end: int = 17) -> object:
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
    from dataclasses import replace

    report = build_report(
        day_inputs(states=[inbox(), stale("A-0", (9, 0), (17, 0))],
                   actions=[replace(sample, params=counted)])
    )  # fmt: skip
    lines = {c.rule: c for c in report.chaos}
    assert lines[PEOPLE].hours == pytest.approx(25 * 8.0)
    assert lines["stale_blocks"].hours == 0 and lines["stale_blocks"].also_hours == 8.0


def test_a_person_comes_first_then_a_stale_block() -> None:
    assert CHAOS_PRECEDENCE[:2] == (PEOPLE, "stale_blocks")
    from kelpie.report.daily import CHAOS_CAUSES

    assert sorted(CHAOS_PRECEDENCE) == sorted(CHAOS_CAUSES)
