"""Farm throughput: a synthetic farm day rebuilt stage by stage, the ``throughput`` section
of the daily report, and the live gauges on ``/metrics``.

The day (2026-03-02, UTC), two developers and a reviewer:

- T1: developed 08-10, waits for review 10-11, reviewed 11-12 and sent back (a return),
  waits for a developer 12-13, fixed 13-14, waits 14-14:30, reviewed 14:30-15, accepted and
  waits for the merge 15-16, done at 16. Working 3h, review 4h, queue 1h.
- T2: a session 08:30-09 that leaves it in progress without an owner: ownerless from 09.
- T3: a session 09-10 that ends blocked; a wait_condition state 10-18; blocked after that.
- T4: ready since 20:00, nobody picks it up: queue 4h, still in the queue at the end.
- Distractors: T5 finished the day before, T6 in the backlog (neither counted).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from kelpie.core.actions import Severity
from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import (
    Owner,
    OwnerKind,
    Return,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.core.stages import live_stage
from kelpie.core.states import OpenState
from kelpie.metrics import Metrics
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections import SECTIONS
from kelpie.report.sections.throughput import throughput_section
from kelpie.report.store import StateRow
from kelpie.report.throughput import RoleThroughput, session_end, throughput

DAY = date(2026, 3, 2)
START = datetime(2026, 3, 2, tzinfo=UTC)
END = START + timedelta(days=1)
ONLY = {"throughput": throughput_section}
PRICES = PriceTable((("model-large", Price(input=3.0, output=15.0, cached_input=0.3)),))


def at(hour: float, day: int = 2) -> datetime:
    return datetime(2026, 3, day, tzinfo=UTC) + timedelta(hours=hour)


def task(
    tid: str,
    status: Status,
    since: datetime,
    owner: str | None = "dev-1",
    returns: tuple[Return, ...] = (),
) -> Task:
    return Task(tid, tid, status, since, role="developer", owner_id=owner, returns=returns)


def session(sid: str, tid: str, owner: str, a: float, b: float, final: Status | None) -> Session:
    return Session(sid, tid, owner, at(a), ended_at=at(b), final_status=final)


OWNERS = (
    Owner("dev-1", "developer"),
    Owner("dev-2", "developer"),
    Owner("rev-1", "reviewer"),
    Owner("human-1", "lead", OwnerKind.HUMAN),
)
TASKS = (
    task("T1", Status.DONE, at(16), returns=(Return(at(12), "missing test", "review"),)),
    task("T2", Status.IN_PROGRESS, at(8.5), owner=None),
    task("T3", Status.BLOCKED, at(10), owner="dev-2"),
    task("T4", Status.READY, at(20), owner=None),
    task("T5", Status.DONE, at(15, day=1)),
    task("T6", Status.BACKLOG, at(9), owner=None),
)
SESSIONS = (
    session("S1", "T1", "dev-1", 8, 10, Status.REVIEW),
    session("S2", "T1", "rev-1", 11, 12, None),
    session("S3", "T1", "dev-1", 13, 14, Status.REVIEW),
    session("S4", "T1", "rev-1", 14.5, 15, Status.ACCEPTED),
    session("S5", "T2", "dev-2", 8.5, 9, None),
    session("S6", "T3", "dev-2", 9, 10, Status.BLOCKED),
    Session("S0", "T5", "dev-1", at(9, day=1), ended_at=at(10, day=1), final_status=Status.DONE),
)


def state(sid: str, rule: str, tid: str, a: datetime, b: datetime) -> OpenState:
    return OpenState(sid, rule, a, a, b, 0, Severity.WARNING, "s", task_id=tid)


STATES = (
    state("orphan_status:T2", "orphan_status", "T2", at(9), at(13)),
    state("wait_condition:T3", "wait_condition", "T3", at(10), at(18)),
)
SPANS = (
    Span(
        "t1",
        "c1",
        "chat",
        SpanKind.LLM,
        at(8),
        at(8.01),
        task_id="T1",
        session_id="S1",
        model="model-large",
        input_tokens=100_000,
        output_tokens=2_000,
    ),
)


def _roles() -> dict[str, RoleThroughput]:
    found = throughput(TASKS, SESSIONS, OWNERS, STATES, START, END)
    return {r.role: r for r in found}


def test_stage_hours_and_per_role_numbers() -> None:
    roles = _roles()
    assert set(roles) == {"developer", "reviewer"}
    dev = roles["developer"]
    assert dev.stages == pytest.approx(
        {
            "working": 3 + 0.5 + 1,
            "review": 4,
            "queue": 1 + 4,
            "waiting": 14,
            "ownerless": 15,
        }
    )
    assert dev.completed == ("T1",) and dev.tasks_per_hour == pytest.approx(1 / 24)
    assert dev.active == ("T1", "T2", "T3", "T4")
    assert dev.reworked == ("T1",) and dev.rework_rate == 0.25 and dev.returns == 1
    assert dev.handoffs == 3 and dev.queue_now == 1
    assert dev.agents == 2 and dev.utilization == pytest.approx(4.5 / 48)
    assert dev.mean_queue == pytest.approx(5 / 24)
    rev = roles["reviewer"]
    # S2 sent T1 back without a declared status: a review all the same
    assert rev.handoffs == 2 and rev.handoffs_per_hour == pytest.approx(2 / 24)
    assert rev.utilization == pytest.approx(1.5 / 24) and rev.active == ()
    assert rev.rework_rate is None


def test_the_section_in_the_daily_report() -> None:
    inputs = ReportInputs(
        day=DAY,
        tz=UTC,
        tasks=TASKS,
        owners=OWNERS,
        sessions=SESSIONS,
        states=tuple(StateRow(s) for s in STATES),
        spans=SPANS,
        prices=PRICES,
    )
    report = build_report(inputs, ONLY)
    [section] = report.sections
    assert section.title == "Farm throughput" and section.advice == ()
    markdown = render_markdown(report)
    # 100k input and 2k output at $3 and $15 per million, one completed task
    assert (
        "| developer | 2 | 9% | 1 | 0.04 | 0.12 | 0.2 | 1 | 25% (1 return(s)) | $0.33 |" in markdown
    )
    assert "| reviewer | 1 | 6% | 0 | 0.00 | 0.08 | 0.0 | 0 | - | *none done* |" in markdown
    assert "| developer | 4.5 | 5.0 | 4.0 | 14.0 | 15.0 |" in markdown
    assert SECTIONS["throughput"] is throughput_section


def test_no_board_no_section() -> None:
    assert build_report(ReportInputs(day=DAY, tz=UTC), ONLY).sections == ()


def test_unknown_role_and_running_sessions() -> None:
    lone = Task("T9", "x", Status.IN_PROGRESS, at(10), owner_id="ghost")
    running = Session("S9", "T9", "ghost", at(10))
    [r] = throughput([lone], [running], [], [], START, END, session_roles={"S9": "writer"})
    assert r.role == "unknown" and r.stages == {"working": pytest.approx(14)}
    assert r.utilization is None and r.agents == 0
    assert session_end(running, END) == END
    late = Session("S8", "T9", "ghost", at(10), last_heartbeat=at(9))
    assert session_end(late, END) == at(10)


def test_live_stages() -> None:
    assert live_stage(task("A", Status.IN_PROGRESS, START), live=True) == "working"
    assert live_stage(task("A", Status.IN_PROGRESS, START, owner=None), live=False) == "ownerless"
    assert live_stage(task("A", Status.IN_PROGRESS, START), live=False) == "queue"
    assert live_stage(task("A", Status.REVIEW, START), live=False) == "review"
    assert live_stage(task("A", Status.WAITING, START), live=False) == "waiting"
    assert live_stage(task("A", Status.BACKLOG, START), live=False) is None
    assert live_stage(task("A", Status.DONE, START), live=False) is None


def test_metrics_export_throughput_gauges() -> None:
    now = at(21)
    snapshot = Snapshot(
        now=now,
        owners=OWNERS,
        tasks=TASKS,
        sessions=(*SESSIONS, Session("S7", "T3", "dev-2", at(20.9), last_heartbeat=now)),
    )
    metrics = Metrics()
    metrics._fleet(snapshot)
    text = metrics.render()
    assert 'kelpie_role_agents{role="developer"} 2' in text
    assert 'kelpie_role_tasks_done_24h{role="developer"} 1' in text  # T5 is older than 24h
    assert 'kelpie_role_returns_24h{role="developer"} 1' in text
    assert 'kelpie_role_ownerless_queue_tasks{role="developer"} 1' in text  # T4
    assert 'kelpie_role_agents_with_work{role="developer"} 1' in text  # dev-2: S7 live
    assert 'kelpie_tasks_by_stage{role="developer",stage="ownerless"} 1' in text
    assert 'kelpie_tasks_by_stage{role="developer",stage="working"} 1' in text  # T3 live
    assert 'kelpie_tasks_by_stage{role="reviewer",stage="queue"} 0' in text
    assert 'kelpie_role_agents{role="reviewer"} 1' in text


def test_cost_markers_and_unlisted_spend() -> None:
    lone = Span(
        "t2",
        "c2",
        "chat",
        SpanKind.LLM,
        at(9),
        at(9.01),
        session_id="S-X",
        model="mystery",
        input_tokens=5_000,
        output_tokens=100,
    )
    inputs = ReportInputs(
        day=DAY,
        tz=UTC,
        tasks=TASKS,
        owners=OWNERS,
        sessions=SESSIONS,
        spans=(*SPANS, lone),
        prices=PRICES,
    )
    markdown = render_markdown(build_report(inputs, ONLY))
    assert "Spend of roles not listed (unknown): 5,100 tokens, $0.00." in markdown
    assert "5,100 tokens of models without a price are not in the dollars." in markdown
    assert "noisy with one or two completions" in markdown
    unpriced = ReportInputs(
        day=DAY, tz=UTC, tasks=TASKS, owners=OWNERS, sessions=SESSIONS, spans=SPANS
    )
    assert "| *unpriced* |" in render_markdown(build_report(unpriced, ONLY))
    quiet = ReportInputs(day=DAY, tz=UTC, tasks=TASKS, owners=OWNERS, sessions=SESSIONS)
    assert "| *no spend* |" in render_markdown(build_report(quiet, ONLY))
