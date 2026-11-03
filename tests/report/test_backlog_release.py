"""Backlog release in before/after (#193 item 4, #284): a throughput spike right after a fix
that unblocked a queue is released backlog, not a faster farm.

Synthetic farm: one developer role, a steady task every two hours. A fix at noon of the day
before the report's day unblocks eight tasks that had been blocked since the morning; they
are all done within four hours, then the steady pace resumes. Throughput then compares the
next full day (the report's day), and the report names the release. A genuine improvement
(a faster pace that holds) is not named as one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.model import Session, Status, Task
from kelpie.report.daily import build_report, render_markdown
from kelpie.report.fixes import MANUAL, FixEvent
from kelpie.report.root_cause import digest
from kelpie.report.verify import Metric, Verification
from tests.report.test_verify_honest import PEOPLE, inputs

H = timedelta(hours=1)
F = datetime(2026, 10, 19, 12, tzinfo=UTC)  # the fix, on the day before the report's day
REPORT_DAY = datetime(2026, 10, 20, tzinfo=UTC)
FIX = FixEvent(F, MANUAL, "dispatcher guard no longer blocks ios tasks", role="developer")


def steady(prefix: str, start: datetime, end: datetime, every: timedelta) -> list[Task | Session]:
    """New tasks done one per ``every`` in ``[start, end)``, each worked for the hour before
    (none known before ``start``)."""
    out: list[Task | Session] = []
    at, i = start + every, 0
    while at < end:
        out.append(Task(f"{prefix}-{i}", "t", Status.DONE, at, role="developer", owner_id="dev-1"))
        out.append(Session(f"{prefix}-s{i}", f"{prefix}-{i}", "dev-1", at - every / 2, at))
        at, i = at + every, i + 1
    return out


def case(*parts: list[Task | Session]) -> dict[str, Any]:
    items = [x for p in parts for x in p]
    return {
        "tasks": tuple(x for x in items if isinstance(x, Task)),
        "sessions": tuple(x for x in items if isinstance(x, Session)),
        "owners": PEOPLE,
        "min_sample": 3,
    }


def released(n: int, blocked_at: datetime, first_done: datetime) -> list[Task | Session]:
    """``n`` tasks blocked at ``blocked_at`` (a session ended declaring blocked), done one
    every half hour from ``first_done``."""
    out: list[Task | Session] = []
    for i in range(n):
        done = first_done + i * H / 2
        out.append(Task(f"B-{i}", "b", Status.DONE, done, role="developer", owner_id="dev-1"))
        out.append(
            Session(f"b-{i}-0", f"B-{i}", "dev-1", blocked_at - H, blocked_at, Status.BLOCKED)
        )
        out.append(Session(f"b-{i}-1", f"B-{i}", "dev-1", done - H / 4, done, Status.DONE))
    return out


def spike() -> dict[str, Any]:
    return case(
        steady("D", F - 24 * H, REPORT_DAY + 24 * H, 2 * H),
        released(8, F - 8 * H, F + H / 2),
    )


def improvement() -> dict[str, Any]:
    """Before: one every two hours; after: one an hour, all day long. Four tasks had been
    ready since the morning and are done first, but at the pace that then holds: a faster
    farm, not a release."""
    ready = []
    for i in range(4):
        done = F + (i + 1) * H
        ready.append(Task(f"R-{i}", "r", Status.DONE, done, role="developer", owner_id="dev-1"))
        ready.append(Session(f"r-{i}-0", f"R-{i}", "dev-1", F - 9 * H, F - 8 * H, Status.READY))
        ready.append(Session(f"r-{i}-1", f"R-{i}", "dev-1", done - H / 2, done, Status.DONE))
    return case(
        steady("D", F - 24 * H, F, 2 * H),
        ready,
        steady("N", F + 4 * H, REPORT_DAY + 24 * H, H),
    )


def throughput(v: Verification) -> Metric:
    return next(m for m in v.metrics if m.name.startswith("tasks done per hour"))


def test_a_spike_right_after_an_unblocking_fix_is_a_backlog_release() -> None:
    report = build_report(inputs(fixes=(FIX,), **spike()))
    [v] = report.verifications
    r = v.release
    assert r is not None
    assert (r.tasks, r.done) == (8, 11)  # 3 of the steady ones fell in the burst window
    assert r.start == F and r.end == F + 6 * H  # the burst window: a quarter of a day
    assert r.full_day and r.steady == (REPORT_DAY, REPORT_DAY + 24 * H)
    m = throughput(v)
    # after: the report's day, the steady pace again (12 done, one every two hours)
    assert m.name.endswith("with work, after: the next full day)")
    assert m.n_after == 12  # done at 00:00 .. 22:00 of the report's day
    assert m.before is not None and m.after is not None
    # about the pace before (work hours shift a little), not the burst's 20 in the window
    assert m.after < 1.3 * m.before
    markdown = render_markdown(report)
    assert "Backlog release: 8 of the 11 tasks done in the 6.0h after the fix" in markdown
    assert "compares the next full day (2026-10-20 00:00 UTC to 2026-10-21 00:00 UTC)" in markdown
    assert digest(report).data["fix_checks"][0]["backlog_release"] == 8


def test_without_the_next_full_day_the_rest_of_the_after_window_is_compared() -> None:
    # the same shape, a day later: the fix is on the report's day, which has not ended
    fix = FixEvent(F + 24 * H, MANUAL, "x", role="developer")
    shifted = case(
        steady("D", F, REPORT_DAY + 24 * H, 2 * H),
        released(8, F + 16 * H, F + 24.5 * H),
    )
    report = build_report(inputs(fixes=(fix,), **shifted))
    [v] = report.verifications
    assert v.release is not None and not v.release.full_day
    assert v.release.steady == (F + 30 * H, REPORT_DAY + 24 * H)
    assert throughput(v).name.endswith("after: past the burst)")
    markdown = render_markdown(report)
    assert "the next full day is not in yet" in markdown
    assert "from 2026-10-20 18:00 UTC on" in markdown


def test_a_genuine_improvement_is_not_a_backlog_release() -> None:
    report = build_report(inputs(fixes=(FIX,), **improvement()))
    [v] = report.verifications
    assert v.release is None
    m = throughput(v)
    assert "after:" not in m.name
    assert m.improved is True
    assert "Backlog release" not in render_markdown(report)
    assert digest(report).data["fix_checks"][0]["backlog_release"] is None


def test_a_few_unblocked_tasks_are_not_a_release() -> None:
    # three released tasks: below the minimum, whatever the pace
    report = build_report(
        inputs(
            fixes=(FIX,),
            **case(steady("D", F - 24 * H, REPORT_DAY + 24 * H, 2 * H), released(3, F - 8 * H, F)),
        )
    )
    [v] = report.verifications
    assert v.release is None


def test_a_burst_of_new_work_is_not_a_release() -> None:
    # eight tasks done right after the fix, none of them known before it: new work
    burst = steady("N", F, F + 4 * H, H / 2)
    report = build_report(
        inputs(fixes=(FIX,), **case(steady("D", F - 24 * H, REPORT_DAY + 24 * H, 2 * H), burst))
    )
    [v] = report.verifications
    assert v.release is None


def test_a_routine_queue_is_not_a_release() -> None:
    # the same eight tasks done right after the fix, but ready only since an hour before
    # it: a queue turning over, not a backlog the fix let go
    report = build_report(
        inputs(
            fixes=(FIX,),
            **case(
                steady("D", F - 24 * H, REPORT_DAY + 24 * H, 2 * H), released(8, F - H, F + H / 2)
            ),
        )
    )
    [v] = report.verifications
    assert v.release is None


def test_a_fix_too_late_to_see_the_pace_after_the_burst_names_no_release() -> None:
    # fixed at 20:00 of the report's day: four hours after it, the pace that follows a
    # burst is unknown, so a faster farm cannot be told from a release
    late = F + 32 * H
    fix = FixEvent(late, MANUAL, "x", role="developer")
    shape = case(
        steady("D", F, REPORT_DAY + 24 * H, 2 * H), released(8, late - 8 * H, late + H / 4)
    )
    [v] = build_report(inputs(fixes=(fix,), **shape)).verifications
    assert v.release is None
    m = throughput(v)
    assert "after:" not in m.name and m.n_after > 0  # the after-window is kept


def test_the_next_full_day_stops_short_of_a_later_fix() -> None:
    # a second fix on the report's day, more than a window after the first: the next
    # full day would measure it too, so the first compares past its burst instead
    later = FixEvent(REPORT_DAY + 13 * H, MANUAL, "faster builds", role="developer")
    report = build_report(inputs(fixes=(FIX, later), **spike()))
    first = next(v for v in report.verifications if v.event == FIX)
    assert first.release is not None and not first.release.full_day
    assert first.release.steady == (F + 6 * H, F + 24 * H)


def test_a_group_names_the_release_after_its_last_fix() -> None:
    rev = FixEvent(F - 2 * H, MANUAL, "reviews read the diff", role="reviewer")
    report = build_report(inputs(fixes=(rev, FIX), **spike()))
    [v] = report.verifications
    assert v.together and v.release is not None
    assert (v.release.tasks, v.release.start) == (8, F)
    assert "Backlog release: 8 of the 11 tasks done in the 6.0h after the last fix" in (
        render_markdown(report)
    )
