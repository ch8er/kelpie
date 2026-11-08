"""Stages from the status changes Kelpie recorded (#157), with the inference as fallback.

The day is 2026-03-02 (UTC), one developer:

- R1: first seen ready at 06:00 (the board said ready since 05:00); picked up 10-11 by a
  session that leaves it in review. Inferred, it counts from 10:00 (queue before its first
  pickup is not known); recorded, it is in the queue from 05:00.
- R2: ready at midnight, blocked at 02:00 (between sessions, none ran), moved to in
  progress at 08:00 with a session 08-09 that leaves it done. Inferred, nothing before
  08:00 is known; recorded: queue 0-2, waiting 2-8.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from kelpie.core.model import Owner, Session, Status, Task
from kelpie.core.transitions import StatusChange, history
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections.throughput import throughput_section
from kelpie.report.throughput import throughput

DAY = date(2026, 3, 2)
START = datetime(2026, 3, 2, tzinfo=UTC)
END = START + timedelta(days=1)
ONLY = {"throughput": throughput_section}
OWNERS = (Owner("dev-1", "developer"),)


def at(hour: float) -> datetime:
    return START + timedelta(hours=hour)


TASKS = (
    Task("R1", "R1", Status.REVIEW, at(11), role="developer", owner_id="dev-1"),
    Task("R2", "R2", Status.DONE, at(9), role="developer", owner_id="dev-1"),
)
SESSIONS = (
    Session("S1", "R1", "dev-1", at(10), ended_at=at(11), final_status=Status.REVIEW),
    Session("S2", "R2", "dev-1", at(8), ended_at=at(9), final_status=Status.DONE),
)
RECORDED = (
    StatusChange(at(6), "R1", Status.READY, at(5)),
    StatusChange(at(11), "R1", Status.REVIEW, at(11), previous=Status.READY, after=at(10.99)),
    StatusChange(at(-1), "R2", Status.READY, at(-3)),  # its last record before the day
    StatusChange(at(2), "R2", Status.BLOCKED, at(2), previous=Status.READY, after=at(1.99)),
    StatusChange(at(8), "R2", Status.IN_PROGRESS, at(8), previous=Status.BLOCKED, after=at(7.99)),
    StatusChange(at(9), "R2", Status.DONE, at(9), previous=Status.IN_PROGRESS, after=at(8.99)),
)


def _dev(recorded: tuple[StatusChange, ...] | None) -> dict[str, float]:
    found = throughput(
        TASKS,
        SESSIONS,
        OWNERS,
        (),
        START,
        END,
        history=None if recorded is None else history(recorded),
    )
    [dev] = found
    return dict(dev.stages)


def test_inferred_without_a_record() -> None:
    # R1: 10-11 working, 11-24 review; R2: 8-9 working
    assert _dev(None) == pytest.approx({"working": 2, "review": 13})


def test_recorded_changes_fill_the_gaps() -> None:
    stages = _dev(RECORDED)
    # R1 adds 05:00-10:00 in the queue; R2 adds 0-2 queue and 2-8 waiting (blocked)
    assert stages == pytest.approx({"working": 2, "review": 13, "queue": 5 + 2, "waiting": 6})
    [dev] = throughput(TASKS, SESSIONS, OWNERS, (), START, END, history=history(RECORDED))
    assert dev.recorded == ("R1", "R2")


def test_no_record_changes_nothing() -> None:
    # negative case: an empty record is the inference, exactly
    assert _dev(()) == _dev(None)
    [dev] = throughput(TASKS, SESSIONS, OWNERS, (), START, END, history={})
    assert dev.recorded == ()


def test_a_record_seen_after_the_day_is_not_used() -> None:
    late = (StatusChange(END + timedelta(hours=1), "R2", Status.BLOCKED, at(2)),)
    assert _dev(late) == _dev(None)


def test_a_session_after_the_record_still_wins() -> None:
    # R2 recorded blocked at 02:00, then a session 03-04 declared ready: ready from 04:00
    sessions = (
        *SESSIONS,
        Session("S3", "R2", "dev-1", at(3), ended_at=at(4), final_status=Status.READY),
    )
    rec = RECORDED[2:4]  # only up to "blocked"
    [dev] = throughput(TASKS, sessions, OWNERS, (), START, END, history=history(rec))
    # R2: queue 0-2, waiting 2-3, working 3-4 and 8-9, queue 4-8; R1 inferred
    assert dev.stages == pytest.approx(
        {"working": 1 + 1 + 1, "review": 13, "queue": 2 + 4, "waiting": 1}
    )


def _markdown(**kw: object) -> str:
    inputs = ReportInputs(
        day=DAY,
        tz=UTC,
        tasks=TASKS,
        owners=OWNERS,
        sessions=SESSIONS,
        **kw,  # type: ignore[arg-type]
    )
    return render_markdown(build_report(inputs, ONLY))


def test_the_report_says_which_source_it_used() -> None:
    recorded = _markdown(transitions=RECORDED, transitions_since=at(-1))
    assert "Kelpie's own record for 2 of 2 task(s) (recorded since 2026-03-01 23:00" in recorded
    assert "| developer | 2.0 | 7.0 | 13.0 | 6.0 | 0.0 |" in recorded
    inferred = _markdown()
    assert "Kelpie recorded no status change for these tasks yet" in inferred
    assert "Kelpie's own record for" not in inferred
    assert "Before/after verification of fixes still infers stages" in inferred
