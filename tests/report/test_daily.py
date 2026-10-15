"""Daily cost-of-chaos report: a synthetic farm day written through the real SQLite
stores, read back read-only, and checked number by number."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.budgets import Price, PriceTable
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import Owner, Session, Snapshot, Span, SpanKind, Status, Task
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
from kelpie.report.run import ReportSources, default_day, generate, send, write
from kelpie.report.store import ReportStore
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
    ],
)
def test_report_config_errors(section: Any) -> None:
    with pytest.raises(ValueError, match=r"\[report\]"):
        load_report(section)
