"""Daily cost-of-chaos report: a synthetic farm day written through the real SQLite
stores, read back read-only, and checked number by number."""

from __future__ import annotations

import re
import sqlite3
import tomllib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.budgets import Price, PriceTable
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression
from kelpie.core.model import Owner, OwnerKind, Session, Snapshot, Span, SpanKind, Status, Task
from kelpie.core.states import OpenState
from kelpie.core.storage import SqliteLedgerStore, SqliteSpanStore
from kelpie.report.config import ReportSettings, load_report
from kelpie.report.daily import (
    ReportInputs,
    build_report,
    day_bounds,
    render_markdown,
    render_summary,
    session_of,
)
from kelpie.report.run import ReportSources, default_day, generate, load_inputs, send, write
from kelpie.report.store import ReportStore, StateRow
from kelpie.rules import REGISTRY
from kelpie.traces.outputs import OutputSettings
from tests.incidents import load

DAY = date(2026, 3, 2)
PRICES = PriceTable((("model-large", Price(input=3.0, output=15.0, cached_input=0.3)),))
FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"


def at(hour: int, minute: int = 0, day: int = 2) -> datetime:
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


def entry(
    when: datetime,
    rule: str,
    kind: ActionKind,
    outcome: Outcome,
    task: str | None,
    mode: Mode = Mode.ACT,
    **params: Any,
) -> LedgerEntry:
    return LedgerEntry(
        at=when,
        fingerprint=f"{rule}:{kind}:{task}",
        subject=f"task:{task}",
        rule=rule,
        kind=kind,
        mode=mode,
        outcome=outcome,
        reason=f"{rule} {kind}",
        task_id=task,
        params=params,
    )


def state(
    sid: str,
    rule: str,
    since: datetime,
    last_seen: datetime,
    task: str | None = None,
    severity: Severity = Severity.WARNING,
) -> OpenState:
    return OpenState(
        id=sid,
        rule=rule,
        since=since,
        opened_at=since,
        last_seen=last_seen,
        level=0,
        severity=severity,
        summary=f"{rule} on {task or 'farm'}",
        task_id=task,
    )


def llm(
    session: str,
    task: str,
    start: datetime,
    n: int,
    every: timedelta,
    tokens: tuple[int, int, int],
    model: str = "model-large",
    tag: str = "llm",
) -> list[Span]:
    inp, out, cached = tokens
    return [
        Span(
            trace_id=f"tr-{session}",
            span_id=f"{session}-{tag}-{i}",
            name="chat",
            kind=SpanKind.LLM,
            start=start + i * every,
            end=start + i * every + timedelta(seconds=4),
            task_id=task,
            session_id=session,
            model=model,
            input_tokens=inp,
            output_tokens=out,
            cached_tokens=cached,
        )
        for i in range(n)
    ]


def xcodebuild(session: str, task: str, start: datetime, n: int) -> list[Span]:
    return [
        Span(
            trace_id=f"tr-{session}",
            span_id=f"{session}-xb-{i}",
            name="execute_tool shell",
            kind=SpanKind.TOOL,
            start=start + timedelta(minutes=10 * i),
            end=start + timedelta(minutes=10 * i + 5),
            task_id=task,
            session_id=session,
            tool="shell",
            attributes={
                "kelpie.tool.command_prefix": "xcodebuild",
                "kelpie.tool.output_chars": 160_000,
            },
        )
        for i in range(n)
    ]


BOARD = Snapshot(
    now=at(23, 59),
    owners=(
        Owner("agent-dev-1", "developer"),
        Owner("agent-ops-1", "maintenance"),
        Owner("reviewer-1", "reviewer"),
    ),
    tasks=(
        Task("T-1", "Done today", Status.DONE, at(15), role="developer", source="hermes"),
        Task("T-2", "Also done", Status.DONE, at(17), role="developer", source="hermes"),
        Task("T-0", "Done yesterday", Status.DONE, at(17, day=1), role="developer"),
        Task("T-3", "Ownerless", Status.IN_PROGRESS, at(8), role="developer"),
        Task("T-4", "Blocked", Status.BLOCKED, at(10), role="developer"),
    ),
    sessions=(
        Session("S-DEV1", "T-1", "agent-dev-1", at(8)),
        Session("S-OPS", "T-9", "agent-ops-1", at(6)),
        Session("S-EXIT", "T-6", "agent-dev-1", at(13), ended_at=at(13, 45)),
    ),
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "kelpie.db"
    ledger = SqliteLedgerStore(path)
    for e in [
        entry(
            at(14),
            "exit_contract",
            ActionKind.REQUEUE,
            Outcome.EXECUTED,
            "T-6",
            session_id="S-EXIT",
        ),
        # a repeat of the same requeue must not count the session twice
        entry(
            at(14, 30),
            "exit_contract",
            ActionKind.REQUEUE,
            Outcome.EXECUTED,
            "T-6",
            session_id="S-EXIT",
        ),
        entry(at(11), "stale_blocks", ActionKind.UNBLOCK, Outcome.EXECUTED, "T-7"),
        entry(at(9), "orphan_status", ActionKind.REASSIGN, Outcome.OBSERVED, "T-3", Mode.OBSERVE),
        entry(at(13), "wait_condition", ActionKind.WAKE, Outcome.FAILED, "T-8"),
        entry(at(12), "orphan_status", ActionKind.ALERT, Outcome.ALERTED, "T-3"),
        entry(at(12, day=1), "stale_blocks", ActionKind.UNBLOCK, Outcome.EXECUTED, "T-old"),
        entry(at(0, day=3), "stale_blocks", ActionKind.UNBLOCK, Outcome.EXECUTED, "T-next"),
    ]:
        ledger.append(e)
    for s, closed in [
        (state("orphan_status:T-3", "orphan_status", at(8), at(12), "T-3"), at(12, 10)),
        (state("stale_blocks:T-4", "stale_blocks", at(10), at(23, 50), "T-4"), None),
        (state("no_progress:S-OPS", "no_progress", at(6), at(10), "T-9"), at(10, 10)),
        (
            state(
                "resources:disk:host-a", "resources", at(20), at(23, 50), None, Severity.CRITICAL
            ),
            None,
        ),
        (
            state("orphan_status:T-old", "orphan_status", at(1, day=1), at(5, day=1), "T-old"),
            at(6, day=1),
        ),
    ]:
        ledger.save(s)
        if closed is not None:
            ledger.close_state(s.id, closed)
    ledger.close()
    spans = SqliteSpanStore(path)
    spans.append_spans(
        [
            # no_progress: 48 wake-ups resending 12k tokens, 06:00-09:55
            *llm("S-OPS", "T-9", at(6), 48, timedelta(minutes=5), (12_000, 40, 0)),
            # exit without status: a restart burned 5 calls
            *llm("S-EXIT", "T-6", at(13), 5, timedelta(minutes=10), (20_000, 500, 10_000)),
            # productive work
            *llm("S-DEV1", "T-1", at(8), 10, timedelta(minutes=30), (50_000, 2_000, 40_000)),
            *llm(
                "S-DEV1",
                "T-1",
                at(16),
                1,
                timedelta(minutes=1),
                (1_000, 0, 0),
                "mystery",
                "unpriced",
            ),
            *xcodebuild("S-DEV1", "T-1", at(9), 4),
            # another day: never counted
            *llm(
                "S-DEV1",
                "T-1",
                at(8, day=1),
                3,
                timedelta(minutes=1),
                (99_000, 0, 0),
                tag="yesterday",
            ),
        ],
        at(23, 59),
    )
    spans.close()
    return path


def sources(db: Path, **kw: Any) -> ReportSources:
    return ReportSources(
        db_path=db,
        report=ReportSettings(**kw),
        prices=PRICES,
        outputs=OutputSettings(),
        estimated=False,
        modes={"orphan_status": Mode.OBSERVE},
    )


def test_cost_of_chaos_day(db: Path) -> None:
    report, markdown = generate(sources(db), DAY, BOARD)
    chaos = {c.rule: c for c in report.chaos}
    assert set(chaos) == {"orphan_status", "stale_blocks", "no_progress", "exit_contract"}
    assert chaos["orphan_status"].hours == pytest.approx(4.0)
    assert chaos["stale_blocks"].hours == pytest.approx(13 + 50 / 60)
    assert chaos["no_progress"].hours == pytest.approx(4.0)
    assert chaos["no_progress"].spend.tokens == 48 * 12_040
    assert chaos["no_progress"].spend.usd == pytest.approx(48 * 0.0366)
    assert chaos["exit_contract"].hours == 0
    assert chaos["exit_contract"].spend.tokens == 5 * 20_500  # counted once
    assert chaos["exit_contract"].spend.usd == pytest.approx(5 * 0.0405)
    assert chaos["exit_contract"].tasks == ("T-6",)
    assert report.chaos_usd == pytest.approx(48 * 0.0366 + 5 * 0.0405)
    # spend and cost per completed task
    assert report.completed == ("T-1", "T-2")
    total = 48 * 0.0366 + 5 * 0.0405 + 10 * 0.072
    assert report.spend.usd == pytest.approx(total)
    assert report.spend.unpriced_tokens == 1_000
    assert report.per_completed_task == pytest.approx(total / 2)
    # fixed, would fix, not done, needs a human
    assert [(f.rule, f.kind, f.tasks) for f in report.fixed] == [
        ("exit_contract", ActionKind.REQUEUE, ("T-6",)),
        ("stale_blocks", ActionKind.UNBLOCK, ("T-7",)),
    ]
    assert [(f.rule, f.tasks) for f in report.would_fix] == [("orphan_status", ("T-3",))]
    assert [(f.rule, f.outcome) for f in report.not_done] == [("wait_condition", Outcome.FAILED)]
    assert [o.state.id for o in report.open_states] == [
        "resources:disk:host-a",
        "stale_blocks:T-4",
    ]
    # top 3, alternating dollars and hours without hour_usd
    titles = [a.title for a in report.recommendations]
    assert titles == [
        "Reduce activity without progress",
        "Reduce blocks that outlived their cause",
        "Filter `xcodebuild` output (developer) with xcbeautify",
    ]
    xcode = report.recommendations[2]
    assert xcode.usd == pytest.approx(4 * 40_000 * 0.7 * 3 / 1_000_000)
    assert "T-1" in xcode.tasks
    # the Markdown carries every section and links numbers back to tasks
    for heading in ("## Cost of chaos", "## Fixed by Kelpie", "## Needs a human", "## Top 3"):
        assert heading in markdown
    assert "`T-4`" in markdown and "1,000 tokens are of models without a price" in markdown
    assert "Would have done in act mode" in markdown


def test_report_is_deterministic(db: Path) -> None:
    first = generate(sources(db), DAY, BOARD)[1]
    assert generate(sources(db), DAY, BOARD)[1] == first


def test_hour_usd_ranks_hours_against_dollars(db: Path) -> None:
    report, markdown = generate(sources(db, hour_usd=10.0), DAY, BOARD)
    titles = [a.title for a in report.recommendations]
    assert titles == [
        "Reduce blocks that outlived their cause",  # 13.8h x $10
        "Reduce activity without progress",  # 4h x $10 + $1.76 burned
        "Let orphan_status act",  # 4h x $10
    ]
    assert report.chaos_usd == pytest.approx(48 * 0.0366 + 5 * 0.0405 + 10 * (4 + 4 + 13 + 50 / 60))
    assert "Stuck hours at $10.00/hour" in markdown


def test_task_links_and_estimates(db: Path) -> None:
    _, markdown = generate(sources(db, task_url="https://board.example.test/t/{id}"), DAY, BOARD)
    assert "[T-4](https://board.example.test/t/T-4)" in markdown
    estimated = build_report(ReportInputs(day=DAY, tz=UTC, prices=PRICES, estimated=True, spans=()))
    assert "(estimate)" in render_markdown(estimated)


def test_no_prices_means_no_dollars(db: Path) -> None:
    src = ReportSources(db, ReportSettings(), PriceTable(), OutputSettings(), False, {})
    report, markdown = generate(src, DAY, BOARD)
    assert not report.priced and "n/a" in markdown and "$" not in report_headline(report)


def report_headline(report: Any) -> str:
    return render_summary(report).splitlines()[1]


def test_missing_database_is_an_empty_day(tmp_path: Path) -> None:
    report, markdown = generate(sources(tmp_path / "none.db"), DAY, BOARD)
    assert report.chaos == () and report.completed == ("T-1", "T-2")
    assert "Nothing stuck" in markdown


def test_store_reads_only_the_day(db: Path) -> None:
    start, end = day_bounds(DAY, UTC)
    with ReportStore(db) as store:
        assert {e.task_id for e in store.actions(start, end)} >= {"T-6", "T-7"}
        assert not {"T-old", "T-next"} & {e.task_id for e in store.actions(start, end)}
        assert "orphan_status:T-old" not in {r.state.id for r in store.states(start, end)}
        assert all(start <= (s.end or s.start) < end for s in store.spans(start, end))


def test_store_never_writes(db: Path) -> None:
    before = db.stat().st_mtime_ns
    with ReportStore(db) as store:
        store.actions(*day_bounds(DAY, UTC))
        store.spans(*day_bounds(DAY, UTC))
    assert db.stat().st_mtime_ns == before


def test_day_bounds_follow_the_time_zone() -> None:
    berlin = ZoneInfo("Europe/Berlin")
    start, end = day_bounds(date(2026, 3, 29), berlin)  # DST starts: a 23-hour day
    assert start == datetime(2026, 3, 28, 23, tzinfo=UTC) and end - start == timedelta(hours=23)
    assert (
        default_day(datetime(2026, 3, 2, 23, 30, tzinfo=UTC), ReportSettings("Europe/Berlin"))
        == DAY
    )


def test_session_states_match_what_the_rules_emit() -> None:
    incident = load(FIXTURES / "field-02-idle-context-resend.yaml")
    actions = list(REGISTRY["no_progress"](incident.config).evaluate(incident.snapshot))
    [alert] = [a for a in actions if a.state]
    assert session_of(state(alert.state or "", "no_progress", at(0), at(1))) == "S-F201"
    incident = load(FIXTURES / "field-01-heartbeat-runaway.yaml")
    actions = list(REGISTRY["loop_breaker"](incident.config).evaluate(incident.snapshot))
    found = {
        session_of(state(a.state, "loop_breaker", at(0), at(1)))
        for a in actions
        if a.state and a.params.get("session_id")
    }
    sessions = {a.params["session_id"] for a in actions if a.state and a.params.get("session_id")}
    assert found == sessions and None not in found
    assert session_of(state("loop_breaker:budget:agent", "loop_breaker", at(0), at(1))) is None


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def send(self, text: str, severity: str, channels: Any = None) -> None:
        self.sent.append((text, severity, channels))


def test_summary_is_sent_and_markdown_written(db: Path, tmp_path: Path) -> None:
    settings = ReportSettings(dir=tmp_path / "reports", channels=("ops",), severity="info")
    report, markdown = generate(sources(db), DAY, BOARD)
    path = write(markdown, settings, DAY)
    assert path is not None and path.name == "kelpie-report-2026-03-02.md"
    assert path.read_text() == markdown
    assert write(markdown, ReportSettings(), DAY) is None
    sender = FakeSender()
    send(report, settings, sender)
    [(text, severity, channels)] = sender.sent
    assert severity == "info" and channels == ("ops",) and len(text) < 4000
    assert text.startswith("Kelpie daily report 2026-03-02") and "1. Reduce" in text


def test_report_config() -> None:
    settings = load_report(
        {
            "timezone": "Europe/Berlin",
            "dir": "~/reports",
            "task_url": "https://b.example.test/{id}",
            "hour_usd": 4,
            "channels": ["ops"],
            "severity": "info",
        }
    )
    assert settings.hour_usd == 4.0 and settings.channels == ("ops",)
    assert settings.dir is not None and "~" not in str(settings.dir)
    assert load_report(None) == ReportSettings()
    tables = load_report({"sections": {"model-fit": {"routine_roles": ["reviewer"]}}})
    assert tables.sections == {"model-fit": {"routine_roles": ["reviewer"]}}


@pytest.mark.parametrize(
    "section",
    [
        [],
        {"bogus": 1},
        {"timezone": "Mars/Base"},
        {"timezone": ""},
        {"dir": ""},
        {"task_url": "https://no-placeholder"},
        {"hour_usd": 0},
        {"hour_usd": True},
        {"hour_usd": float("inf")},
        {"channels": []},
        {"channels": [""]},
        {"severity": "loud"},
        {"sections": []},
        {"sections": {"model-fit": 1}},
    ],
)
def test_report_config_errors(section: Any) -> None:
    with pytest.raises(ValueError, match=r"\[report\]"):
        load_report(section)


# --- accounting edge cases (review findings) ------------------------------------------


def opened(sid: str, rule: str, since: datetime, opened_at: datetime, last: datetime) -> StateRow:
    return StateRow(OpenState(sid, rule, since, opened_at, last, 0, Severity.WARNING, "s", "T-9"))


def day_inputs(**kw: Any) -> ReportInputs:
    base: dict[str, Any] = {"day": DAY, "tz": UTC, "prices": PRICES}
    return ReportInputs(**{**base, **kw})


def test_budget_and_forecast_states_are_not_loops() -> None:
    rows = [
        opened("loop_breaker:budget:farm-day:farm", "loop_breaker", at(9), at(9), at(23)),
        opened("loop_breaker:forecast:farm-day:farm", "loop_breaker", at(9), at(9), at(23)),
    ]
    assert build_report(day_inputs(states=rows)).chaos == ()


def test_a_session_is_burned_once_across_overlapping_causes() -> None:
    spans = llm("S-X", "T-9", at(6), 12, timedelta(minutes=10), (10_000, 0, 0))
    rows = [
        opened("no_progress:S-X", "no_progress", at(6), at(6), at(8)),
        opened("loop_breaker:identical_calls:S-X", "loop_breaker", at(6), at(6), at(8)),
        opened("loop_breaker:error_streak:S-X", "loop_breaker", at(7), at(7), at(8)),
    ]
    requeue = entry(
        at(9), "exit_contract", ActionKind.REQUEUE, Outcome.EXECUTED, "T-9", session_id="S-X"
    )
    report = build_report(day_inputs(spans=spans, states=rows, actions=[requeue]))
    assert report.chaos_spend.tokens == report.spend.tokens == 12 * 10_000
    assert sum(c.spend.tokens for c in report.chaos) == 120_000


def test_nothing_to_do_requeue_is_no_restart() -> None:
    spans = llm("S-Y", "T-9", at(6), 3, timedelta(minutes=10), (10_000, 0, 0))
    noop = entry(
        at(9), "exit_contract", ActionKind.REQUEUE, Outcome.NOTHING_TO_DO, "T-9", session_id="S-Y"
    )
    assert build_report(day_inputs(spans=spans, actions=[noop])).chaos == ()


def test_hours_count_from_when_kelpie_first_reported() -> None:
    # blocked since 06:00, reported stale at 10:00, last seen 12:00: 2 hours lost
    row = opened("stale_blocks:T-4", "stale_blocks", at(6), at(10), at(12))
    [line] = build_report(day_inputs(states=[row])).chaos
    assert line.hours == pytest.approx(2.0)


def test_suggested_actions_count_as_would_fix() -> None:
    suggested = entry(
        at(9), "orphan_status", ActionKind.REASSIGN, Outcome.SUGGESTED, "T-3", Mode.SUGGEST
    )
    row = opened("orphan_status:T-3", "orphan_status", at(8), at(8), at(12))
    report = build_report(
        day_inputs(actions=[suggested], states=[row], modes={"orphan_status": Mode.SUGGEST})
    )
    assert [f.rule for f in report.would_fix] == ["orphan_status"]
    assert report.recommendations[0].title == "Let orphan_status act"


def test_headline_splits_tokens_and_hours_and_names_unpriced() -> None:
    spans = [
        *llm("S-Z", "T-9", at(6), 2, timedelta(minutes=10), (1_000, 0, 0), "mystery", "u"),
    ]
    row = opened("orphan_status:T-3", "orphan_status", at(8), at(8), at(10))
    report = build_report(day_inputs(spans=spans, states=[row], hour_usd=5.0))
    headline = render_summary(report).splitlines()[1]
    assert "$0.00 in 0 tokens burned + $10.00 in stuck hours" in headline
    assert "2,000 tokens of unpriced models" in headline


def test_needs_a_human_shows_local_times() -> None:
    row = opened("stale_blocks:T-4", "stale_blocks", at(10), at(10), at(23, 50))
    report = build_report(day_inputs(states=[row], tz=ZoneInfo("Europe/Berlin")))
    assert "since 2026-03-02 11:00 CET" in render_markdown(report)


def test_store_opens_without_writing_anything(db: Path) -> None:
    wal = db.with_name(db.name + "-wal")
    before = (db.stat().st_mtime_ns, db.read_bytes(), wal.stat().st_size if wal.exists() else 0)
    with ReportStore(db) as store:
        start, end = day_bounds(DAY, UTC)
        store.actions(start, end)
        store.states(start, end)
        store.spans(start, end)
        store.attribution()
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store._db.execute("CREATE TABLE x (y)")
    # a reader may create an empty -wal/-shm pair, but writes no frames and no pages
    after = (db.stat().st_mtime_ns, db.read_bytes(), wal.stat().st_size if wal.exists() else 0)
    assert after == before


# --- forgotten holds (rule 6) --------------------------------------------------------------


def hold_alert(
    when: datetime, idle_minutes: int, agents: list[str], label: str = "merge-hold"
) -> LedgerEntry:
    return entry(
        when,
        "bottlenecks",
        ActionKind.ALERT,
        Outcome.OBSERVED,
        None,
        Mode.OBSERVE,
        hold=label,
        held_tasks=["T-21", "T-22"],
        idle_agents=agents,
        idle_agent_minutes=idle_minutes,
    )


def test_forgotten_hold_counts_idle_agent_hours() -> None:
    # expired 10:00, reported 10:00, last seen 14:00; at 12:00 two agents had been idle
    # since the expiry: 240 agent-minutes over 120 minutes = 2 agents, for 4 hours
    hold = opened("bottlenecks:hold:merge-hold", "bottlenecks", at(10), at(10), at(14))
    alert = hold_alert(at(12), 240, ["agent-dev-1", "agent-dev-2"])
    report = build_report(day_inputs(states=[hold], actions=[alert], hour_usd=3.0))
    [line] = report.chaos
    assert line.rule == "bottlenecks:hold" and line.hours == pytest.approx(8.0)
    assert line.tasks == ("T-21", "T-22")
    assert report.chaos_usd == pytest.approx(24.0)
    markdown = render_markdown(report)
    assert "idle agents behind forgotten holds" in markdown and "agent-hours" in markdown


def test_hold_alert_from_an_earlier_day_and_without_data() -> None:
    hold = opened("bottlenecks:hold:merge-hold", "bottlenecks", at(20, day=1), at(20, day=1), at(6))
    # the latest alert (opened, at the expiry): idle agents counted directly
    alert = hold_alert(at(20, day=1), 0, ["agent-dev-1"])
    [line] = build_report(day_inputs(states=[hold], actions=[alert])).chaos
    assert line.hours == pytest.approx(6.0)  # 1 agent, 00:00-06:00 of the day
    assert build_report(day_inputs(states=[hold])).chaos == ()  # no alert: nothing known


def test_holds_are_not_also_counted_as_bottlenecks() -> None:
    hold = opened("bottlenecks:hold:h", "bottlenecks", at(10), at(10), at(12))
    idle = opened("bottlenecks:idle-ready", "bottlenecks", at(10), at(10), at(12))
    alert = hold_alert(at(10), 0, ["a", "b", "c"], label="h")
    chaos = {
        c.rule: c.hours
        for c in build_report(day_inputs(states=[hold, idle], actions=[alert])).chaos
    }
    assert chaos == {"bottlenecks": pytest.approx(2.0), "bottlenecks:hold": pytest.approx(6.0)}


def test_hold_state_and_params_match_rule_6() -> None:
    incident = load(FIXTURES / "14-forgotten-merge-hold.yaml")
    actions = list(REGISTRY["bottlenecks"](incident.config).evaluate(incident.snapshot))
    holds = [a for a in actions if a.state and a.state.startswith("bottlenecks:hold:")]
    assert holds
    for a in holds:
        assert a.state == f"bottlenecks:hold:{a.params['hold']}"
        assert isinstance(a.params["idle_agent_minutes"], int)
        assert isinstance(a.params["idle_agents"], list)
        assert isinstance(a.params["held_tasks"], list)


def test_hold_samples_are_a_step_function() -> None:
    # 3 idle from 00:00 (sample the evening before), 0 at 12:00, 2 at 18:00, till 24:00
    hold = opened(
        "bottlenecks:hold:merge-hold",
        "bottlenecks",
        at(20, day=1),
        at(20, day=1),
        at(23, 59, day=2),
    )
    samples = [
        hold_alert(at(20, day=1), 0, ["a", "b", "c"]),
        hold_alert(at(12), 0, []),
        hold_alert(at(18), 0, ["a", "b"]),
        hold_alert(at(0, day=3), 0, ["a", "b", "c", "d"]),  # after the day: ignored
    ]
    [line] = build_report(day_inputs(states=[hold], actions=samples)).chaos
    assert line.hours == pytest.approx(3 * 12 + 0 * 6 + 2 * (5 + 59 / 60))


def test_hold_samples_of_an_earlier_episode_are_ignored() -> None:
    hold = opened("bottlenecks:hold:merge-hold", "bottlenecks", at(12), at(12), at(14))
    old = hold_alert(at(8), 0, ["a", "b", "c", "d", "e"])  # before this episode's expiry
    assert build_report(day_inputs(states=[hold], actions=[old])).chaos == ()


def test_before_after_counts_holds_like_the_report() -> None:
    from kelpie.report.fixes import MANUAL, FixEvent

    hold = opened("bottlenecks:hold:merge-hold", "bottlenecks", at(0, day=1), at(0, day=1), at(23))
    samples = [hold_alert(at(0, day=1), 0, ["a", "b", "c", "d"]), hold_alert(at(12), 0, [])]
    fix = FixEvent(at(12), MANUAL, "lifted the merge hold", rule="bottlenecks")
    report = build_report(day_inputs(states=[hold], actions=samples, fixes=(fix,)))
    [v] = report.verifications
    lost = next(m for m in v.metrics if m.name.startswith("hours lost"))
    assert lost.name == "hours lost per day (bottlenecks)"
    assert (lost.before, lost.after) == (pytest.approx(4 * 24), pytest.approx(0))
    assert "Cost of chaos: 48.0 hours lost" in render_summary(report)


def test_a_fix_scoped_to_the_hold_cause() -> None:
    from kelpie.report.fixes import MANUAL, FixEvent

    hold = opened("bottlenecks:hold:merge-hold", "bottlenecks", at(0, day=1), at(0, day=1), at(23))
    idle = opened("bottlenecks:idle-ready", "bottlenecks", at(0, day=1), at(0, day=1), at(23))
    samples = [hold_alert(at(0, day=1), 0, ["a", "b"])]
    fix = FixEvent(at(12), MANUAL, "hold expiries", rule="bottlenecks:hold")
    report = build_report(day_inputs(states=[hold, idle], actions=samples, fixes=(fix,)))
    lost = next(m for m in report.verifications[0].metrics if m.name.startswith("hours lost"))
    assert lost.name == "hours lost per day (bottlenecks:hold)"
    assert lost.before == pytest.approx(2 * 24)  # the hold only, in agent-hours
    assert "hours lost)" in render_markdown(report)


# --- agents waiting on people (rule 13) and cards stuck before work (rule 3) -------------


def inbox_sample(
    when: datetime, waiting: list[str], person: str | None = "human-owner"
) -> LedgerEntry:
    return entry(
        when, "needs_you", ActionKind.ESCALATE, Outcome.ALERTED, None,
        escalate_to=person, cost_hours=1.0, blocked_agent_tasks=len(waiting),
        waiting_agent_tasks=len(waiting), waiting=waiting, items=[{"task": "H-1"}],
    )  # fmt: skip


def test_agents_waiting_on_people_count_agent_hours() -> None:
    # 3 agent tasks wait on the person from 09:00, 1 from 15:00, the inbox closes at 17:00
    inbox = opened("needs_you:human-owner", "needs_you", at(9), at(9), at(17))
    samples = [
        inbox_sample(at(9), ["A-1", "A-2", "A-3"]),
        inbox_sample(at(15), ["A-3"]),
        inbox_sample(at(10), ["Z-1"], person="human-lead"),  # someone else's inbox
    ]
    report = build_report(day_inputs(states=[inbox], actions=samples, hour_usd=2.0))
    [line] = report.chaos
    assert line.rule == "needs_you" and line.hours == pytest.approx(3 * 6 + 1 * 2)
    assert line.tasks == ("A-1", "A-2", "A-3")
    assert line.cause == "agents waiting on people (approvals, human tasks)"
    markdown = render_markdown(report)
    assert "agents waiting on people" in markdown
    assert "Reduce agents waiting on people" in markdown


def test_the_unassigned_inbox_and_alerts_without_the_count() -> None:
    inbox = opened("needs_you:unassigned", "needs_you", at(10), at(10), at(12))
    [line] = build_report(
        day_inputs(states=[inbox], actions=[inbox_sample(at(10), ["A-1", "A-2"], person=None)])
    ).chaos
    assert line.hours == pytest.approx(4.0)
    old = entry(
        at(10), "needs_you", ActionKind.ESCALATE, Outcome.ALERTED, None,
        escalate_to=None, cost_hours=1.0, waiting_agent_tasks=True,
    )  # fmt: skip
    assert build_report(day_inputs(states=[inbox], actions=[old])).chaos == ()


def test_cards_stuck_before_work_are_their_own_line() -> None:
    stuck = opened(
        "orphan_status:stuck-before-work:T-5:2026-03-02T06:00:00+00:00",
        "orphan_status",
        at(6),
        at(8),
        at(12),
    )
    ownerless = opened("orphan_status:T-3", "orphan_status", at(9), at(9), at(10))
    chaos = {c.rule: c for c in build_report(day_inputs(states=[stuck, ownerless])).chaos}
    assert chaos["orphan_status:prework"].hours == pytest.approx(4.0)
    assert chaos["orphan_status:prework"].cause == "cards stuck before work (triage, scheduled)"
    assert chaos["orphan_status"].hours == pytest.approx(1.0)


def test_stuck_before_work_state_matches_rule_3() -> None:
    from kelpie.report.daily import PREWORK, cause_of

    incident = load(FIXTURES / "17-card-stuck-scheduled.yaml")
    actions = list(REGISTRY[incident.rule](incident.config).evaluate(incident.snapshot))
    [a] = [a for a in actions if a.state and ":stuck-before-work:" in a.state]
    state = OpenState(a.state, "orphan_status", at(1), at(1), at(2), 0, Severity.WARNING, "s")
    assert cause_of(state) == PREWORK


def test_before_after_counts_people_waits_like_the_report() -> None:
    from kelpie.report.daily import state_hours

    inbox = opened("needs_you:human-owner", "needs_you", at(9), at(9), at(17)).state
    samples = [inbox_sample(at(9), ["A-1", "A-2"])]
    assert state_hours(samples, inbox, at(9), at(17)) == pytest.approx(16.0)


def test_section_tables_reach_the_report_inputs(db: Path) -> None:
    raw = tomllib.loads('[report.sections.tokens]\nroutine_roles = ["reviewer"]\n')["report"]
    src = sources(db, sections=load_report(raw).sections)
    assert load_inputs(src, DAY, BOARD).section_config == {
        "tokens": {"routine_roles": ["reviewer"]}
    }


def test_needs_you_inbox_heads_needs_a_human_by_cost() -> None:
    def inbox(person: str | None, cost: float, items: list[str], when: datetime) -> LedgerEntry:
        return entry(
            when, "needs_you", ActionKind.ESCALATE, Outcome.ALERTED, None,
            escalate_to=person, cost_hours=cost, blocked_agent_tasks=len(items),
            items=[{"task": t} for t in items],
        )  # fmt: skip

    states = [
        opened("needs_you:human-owner", "needs_you", at(9), at(9), at(23)),
        opened("needs_you:human-lead", "needs_you", at(9), at(9), at(23)),
    ]
    actions = [
        inbox("human-owner", 2.0, ["T-1"], at(9)),
        inbox("human-owner", 6.5, ["T-2", "T-1"], at(12)),  # the latest wins
        inbox("human-lead", 3.0, ["T-9"], at(10)),
        inbox("human-gone", 99.0, ["T-0"], at(10)),  # its state is closed: not listed
    ]
    report = build_report(day_inputs(states=states, actions=actions))
    assert [(i.person, i.cost_hours, i.tasks) for i in report.inbox] == [
        ("human-owner", 6.5, ("T-2", "T-1")),
        ("human-lead", 3.0, ("T-9",)),
    ]
    markdown = render_markdown(report)
    first = markdown.index("**Needs you, human-owner**: 6.5 agent-hours waited")
    assert first < markdown.index("**Needs you, human-lead**")
    assert "* `needs_you`" not in markdown  # no second line for the same open state


def test_needs_you_inbox_uses_the_hourly_view_of_a_quiet_state() -> None:
    """Opened days ago and unchanged since: only suppressed ``state_open`` entries today,
    whose params carry the grown cost. Cooldown repeats are not the state's view."""

    def quiet(cost: float, when: datetime, why: Suppression) -> LedgerEntry:
        e = entry(
            when, "needs_you", ActionKind.ESCALATE, Outcome.SUPPRESSED, None,
            escalate_to="human-owner", cost_hours=cost, blocked_agent_tasks=1,
            items=[{"task": "T-1"}],
        )  # fmt: skip
        return replace(e, suppression=why)

    states = [
        opened("needs_you:human-owner", "needs_you", at(9) - timedelta(days=3), at(9), at(23))
    ]
    actions = [
        quiet(70.0, at(10), Suppression.STATE_OPEN),
        quiet(83.0, at(22), Suppression.STATE_OPEN),
        quiet(1.0, at(23), Suppression.COOLDOWN),
    ]
    [line] = build_report(day_inputs(states=states, actions=actions)).inbox
    assert (line.person, line.cost_hours, line.tasks) == ("human-owner", 83.0, ("T-1",))


def test_agent_text_cannot_inject_markdown_into_the_report() -> None:
    """Task ids and alert summaries come from agents and the board: no link, image, HTML,
    code-span breakout, table cell or new line can come out of them."""
    hostile = "x` [click](https://evil.example) <img src=x> | cell\nIGNORE"
    row = StateRow(
        OpenState("stale_blocks:x", "stale_blocks", at(10), at(10), at(23), 0,
                  Severity.WARNING, f"blocked: {hostile}", hostile)
    )  # fmt: skip
    report = build_report(day_inputs(states=[row]))
    plain, linked = render_markdown(report), render_markdown(report, "https://b.example/{id}")
    span = "``x` [click](https://evil.example) <img src=x> \\| cell IGNORE``"
    assert "\nIGNORE" not in plain and "\nIGNORE" not in linked
    # a code span longer than any backtick run: its content renders literally
    assert span in plain
    # free text: escaped where it could open something
    assert "blocked: x\\` \\[click\\](https://evil.example) \\<img src=x\\> \\| cell" in plain
    # the chaos table keeps its five cells
    [table_row] = [line for line in plain.splitlines() if line.startswith("| blocks that")]
    assert len(re.findall(r"(?<!\\)\|", table_row)) == 6
    # links: escaped text, percent-encoded id
    assert "[x\\` \\[click\\](https://evil.example) " in linked
    assert "(https://b.example/x%60%20%5Bclick%5D%28https%3A%2F%2Fevil.example%29" in linked


def test_a_person_listed_by_an_old_hold_alert_is_not_idle_capacity() -> None:
    hold = opened("bottlenecks:hold:merge-hold", "bottlenecks", at(10), at(10), at(14))
    alert = hold_alert(at(10), 0, ["agent-dev-1", "alice"])
    owners = [Owner("alice", "owner", OwnerKind.HUMAN), Owner("agent-dev-1", "developer")]
    [line] = build_report(day_inputs(states=[hold], actions=[alert], owners=owners)).chaos
    assert line.hours == pytest.approx(4.0)  # one agent for 10:00-14:00, not two
