"""Before/after verification of fixes, on the farm example from the plan: after a manual
fix, review time dropped from 48 to 27 minutes and failures from 9 of 29 to 1 of 11."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import load_settings
from kelpie.cli import main
from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import Owner, Return, Session, Span, SpanKind, Status, Task
from kelpie.core.states import OpenState
from kelpie.core.storage import SqliteLedgerStore
from kelpie.report.cli import note_fix
from kelpie.report.config import load_report
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.fixes import CONFIG, MANUAL, FixEvent, FixStore, read_fixes, record_config_change
from kelpie.report.store import ReportStore, StateRow
from kelpie.report.verify import Evidence, mode_events, verify
from kelpie.traces.outputs import Call

FIX = datetime(2026, 3, 1, 8, tzinfo=UTC)  # the day before the report's day
REPORT_DAY = date(2026, 3, 2)
OWNERS = (Owner("reviewer-1", "reviewer"), Owner("agent-dev-1", "developer"))


def reviews(start: datetime, n: int, minutes: int, failed: int) -> list[Session]:
    every = timedelta(days=1) / n
    out = []
    for i in range(n):
        began = start + i * every
        out.append(
            Session(
                id=f"R-{start:%d%H}-{i}",
                task_id=f"T-R{i}",
                owner_id="reviewer-1",
                started_at=began,
                ended_at=began + timedelta(minutes=minutes),
                final_status=Status.FAILED if i < failed else Status.DONE,
            )
        )
    return out


SESSIONS = (
    *reviews(FIX - timedelta(days=1), 29, 48, 9),
    *reviews(FIX + timedelta(minutes=1), 11, 27, 1),
)


def inputs(**kw: Any) -> ReportInputs:
    base: dict[str, Any] = {
        "day": REPORT_DAY,
        "tz": UTC,
        "owners": OWNERS,
        "sessions": SESSIONS,
        "fixes": (FixEvent(FIX, MANUAL, "reviewers read only the diff since the last round"),),
    }
    return ReportInputs(**{**base, **kw})


def metric(report: Any, name: str) -> Any:
    [v] = report.verifications
    return next(m for m in v.metrics if m.name.startswith(name))


def test_farm_example_review_time_and_failures() -> None:
    report = build_report(inputs())
    review = metric(report, "review time")
    assert (review.before, review.after) == (pytest.approx(48), pytest.approx(27))
    assert (review.n_before, review.n_after) == (29, 11)
    assert review.improved and not review.small
    failed = metric(report, "sessions failed")
    assert failed.before == pytest.approx(9 / 29) and failed.after == pytest.approx(1 / 11)
    assert "9 of 29 -> 1 of 11" in failed.name and failed.improved
    markdown = render_markdown(report)
    assert "## Before/after of recent fixes" in markdown
    assert "reviewers read only the diff" in markdown
    assert "| review time | 48.0 min | 27.0 min | -44% better | 29 / 11 |" in markdown


def test_small_samples_are_labelled() -> None:
    report = build_report(inputs(min_sample=20))
    assert metric(report, "review time").small
    assert report.verifications[0].small_sample
    assert "small sample: a direction, not a result" in render_markdown(report)


def test_short_after_window_is_a_small_sample() -> None:
    late = datetime(2026, 3, 2, 22, tzinfo=UTC)  # 2 hours before the report day ends
    spans = [
        Span("tr", f"s{i}", "chat", SpanKind.LLM, at, at, session_id="D-1", model="m",
             input_tokens=100)
        for i, at in enumerate((late - timedelta(hours=1), late + timedelta(hours=1)))
    ]  # fmt: skip
    report = build_report(inputs(fixes=(FixEvent(late, MANUAL, "late fix"),), spans=spans))
    [v] = report.verifications
    assert v.after == timedelta(hours=2)
    assert metric(report, "tokens per day").small
    assert not any(m.name.startswith("task returns") for m in v.metrics)  # no data: no row


def test_only_recent_fixes_are_verified() -> None:
    old = FixEvent(FIX - timedelta(days=2), MANUAL, "too old")
    future = FixEvent(datetime(2026, 3, 3, 1, tzinfo=UTC), MANUAL, "after the day")
    assert build_report(inputs(fixes=(old, future))).verifications == ()
    assert "No fixes on this day" in render_markdown(build_report(inputs(fixes=())))


def _entry(at: datetime, rule: str, mode: Mode, outcome: Outcome = Outcome.OBSERVED) -> LedgerEntry:
    return LedgerEntry(at, "fp", "task:T-1", rule, ActionKind.UNBLOCK, mode, outcome, "r", "T-1")


def test_mode_changes_come_from_the_ledger() -> None:
    entries = [
        _entry(FIX - timedelta(hours=5), "stale_blocks", Mode.OBSERVE),
        _entry(FIX - timedelta(hours=1), "stale_blocks", Mode.OBSERVE),
        _entry(FIX, "stale_blocks", Mode.ACT, Outcome.EXECUTED),
        _entry(FIX + timedelta(hours=1), "stale_blocks", Mode.ACT, Outcome.SUPPRESSED),
        _entry(FIX + timedelta(hours=2), "orphan_status", Mode.ACT, Outcome.EXECUTED),
    ]
    [event] = mode_events(entries)
    assert (event.at, event.rule, event.note) == (
        FIX,
        "stale_blocks",
        "stale_blocks: observe -> act",
    )


def _state(rule: str, since: datetime, last: datetime) -> StateRow:
    return StateRow(
        OpenState(f"{rule}:x{since:%H}", rule, since, since, last, 0, Severity.WARNING, "s", "T-1")
    )


def test_stuck_hours_of_the_fixed_rule() -> None:
    states = (
        _state("stale_blocks", FIX - timedelta(hours=12), FIX),  # 12h before
        _state("stale_blocks", FIX + timedelta(hours=2), FIX + timedelta(hours=3)),  # 1h after
        _state("orphan_status", FIX + timedelta(hours=1), FIX + timedelta(hours=9)),
    )
    event = FixEvent(FIX, MANUAL, "conditions for CI blocks", rule="stale_blocks")
    report = build_report(inputs(fixes=(event,), states=states))
    stuck = metric(report, "stuck condition-hours")
    assert stuck.name == "stuck condition-hours per day (stale_blocks)"
    assert (stuck.before, stuck.after) == (pytest.approx(12), pytest.approx(1))
    everything = build_report(inputs(states=states))
    assert metric(everything, "stuck condition-hours").after == pytest.approx(9)


def test_tokens_returns_and_command_output() -> None:
    def chat(i: int, at: datetime, tokens: int) -> Span:
        return Span(
            "tr", f"s{i}", "chat", SpanKind.LLM, at, at + timedelta(seconds=1),
            session_id="D-1", model="m", input_tokens=tokens,
        )  # fmt: skip

    spans = [
        chat(1, FIX - timedelta(hours=3), 10_000),
        chat(2, FIX + timedelta(hours=3), 4_000),
    ]
    sessions = (*SESSIONS, Session("D-1", "T-9", "agent-dev-1", FIX - timedelta(hours=4)))
    tasks = (
        Task(
            "T-9",
            "t",
            Status.IN_PROGRESS,
            FIX,
            returns=(
                Return(FIX - timedelta(hours=2), "conflict"),
                Return(FIX - timedelta(hours=1), "conflict"),
                Return(FIX + timedelta(hours=1), "conflict"),
            ),
        ),
    )
    event = FixEvent(FIX, MANUAL, "role prompt trimmed", role="developer")
    report = build_report(inputs(fixes=(event,), spans=spans, sessions=sessions, tasks=tasks))
    tokens = metric(report, "tokens per day (developer)")
    assert (tokens.before, tokens.after) == (10_000, 4_000)
    returns = metric(report, "task returns")
    assert (returns.n_before, returns.n_after) == (2, 1)


def test_command_fix_compares_output_tokens_per_call() -> None:
    def call(minutes: int, tokens: int) -> Call:
        return Call(
            FIX + timedelta(minutes=minutes),
            "developer",
            "npm test",
            tokens,
            False,
            None,
            None,
            None,
            None,
        )

    calls = [call(-60 + i, 8_000) for i in range(6)] + [call(10 + i, 2_000) for i in range(6)]
    event = FixEvent(FIX, MANUAL, "npm test --silent", command="npm test", role="developer")
    result = verify(event, Evidence(end=FIX + timedelta(days=1), calls=calls, min_sample=5))
    out = next(m for m in result.metrics if m.name.startswith("output tokens"))
    assert (out.before, out.after, out.change) == (8_000, 2_000, pytest.approx(-0.75))
    assert not out.small


def test_filter_introductions_become_fix_events() -> None:
    def tool(i: int, at: datetime, chars: int, filt: str | None) -> Span:
        attrs: dict[str, Any] = {
            "kelpie.tool.command_prefix": "git diff",
            "kelpie.tool.output_chars": chars,
        }
        if filt:
            attrs["kelpie.tool.filter"] = filt
        return Span(
            "tr", f"t{i}", "execute_tool", SpanKind.TOOL, at, at, session_id="D-1", attributes=attrs
        )

    spans = [tool(i, FIX - timedelta(hours=6 - i), 40_000, None) for i in range(6)]
    spans += [tool(10 + i, FIX + timedelta(hours=i), 4_000, "rtk") for i in range(6)]
    report = build_report(inputs(fixes=(), spans=spans, min_sample=5))
    [v] = report.verifications
    assert v.event.kind == "filter" and v.event.command == "git diff"
    out = next(m for m in v.metrics if m.name.startswith("output tokens"))
    assert (out.before, out.after) == (10_000, 1_000)


# --- storage and CLI ---------------------------------------------------------------------


def test_fix_store_and_config_changes(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    SqliteLedgerStore(db).close()  # the schema kelpie serve creates
    config = tmp_path / "kelpie.toml"
    config.write_text("[engine]\n")
    assert not record_config_change(db, config, FIX - timedelta(days=3))  # the baseline
    assert not record_config_change(db, config, FIX - timedelta(days=2))  # unchanged
    config.write_text("[engine]\ndry_run = false\n")
    assert record_config_change(db, config, FIX)
    with FixStore(db) as store:
        store.add(FixEvent(FIX + timedelta(hours=1), MANUAL, "  hold lifted  ", rule="bottlenecks"))
        with pytest.raises(ValueError, match="note"):
            store.add(FixEvent(FIX, MANUAL, "  "))
    with ReportStore(db) as store:
        found = store.fixes(FIX - timedelta(days=5), FIX + timedelta(days=1))
    assert [(f.kind, f.note) for f in found] == [
        (CONFIG, "config changed"),
        (MANUAL, "hold lifted"),
    ]
    assert not record_config_change(tmp_path / "missing" / "x.db", config, FIX)  # logged, no raise


def test_missing_fix_table_reads_as_no_fixes(tmp_path: Path) -> None:
    import sqlite3

    db = sqlite3.connect(tmp_path / "empty.db")
    assert read_fixes(db, FIX, FIX + timedelta(days=1)) == []


def test_kelpie_fix_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n')
    SqliteLedgerStore(tmp_path / "kelpie.db").close()
    args = ["--config", str(path), "fix", "lifted the merge hold", "--rule", "bottlenecks"]
    assert main([*args, "--at", "2026-03-01T08:00:00+00:00", "--command", "make test"]) == 0
    assert "fix noted at 2026-03-01T08:00+00:00" in capsys.readouterr().out
    settings = load_settings(path)
    assert note_fix(settings, "x", rule="no_such_rule")[1] == 2
    assert note_fix(settings, "x", when="yesterday")[1] == 2
    assert note_fix(settings, " ")[1] == 2
    with ReportStore(tmp_path / "kelpie.db") as store:
        [fix] = store.fixes(FIX - timedelta(hours=1), FIX + timedelta(hours=1))
    assert (fix.rule, fix.command, fix.note) == (
        "bottlenecks",
        "make test",
        "lifted the merge hold",
    )
    memory = tmp_path / "memory.toml"
    memory.write_text('[storage]\npath = ":memory:"\n')
    assert note_fix(load_settings(memory), "x")[1] == 2


def test_verify_config() -> None:
    settings = load_report({"verify_window": "12h", "review_roles": ["qa"], "min_sample": 5})
    assert settings.verify_window == timedelta(hours=12) and settings.review_roles == ("qa",)
    for bad in (
        {"verify_window": 5},
        {"verify_window": "0m"},
        {"review_roles": "qa"},
        {"min_sample": 0},
    ):
        with pytest.raises(ValueError, match=r"\[report\]"):
            load_report(bad)


# --- second review -----------------------------------------------------------------------


def test_count_metrics_label_small_samples() -> None:
    tasks = (
        Task(
            "T-9",
            "t",
            Status.IN_PROGRESS,
            FIX,
            returns=(
                Return(FIX - timedelta(hours=2), "conflict"),
                Return(FIX - timedelta(hours=1), "conflict"),
            ),
        ),
    )
    report = build_report(inputs(tasks=tasks))
    returns = metric(report, "task returns")
    assert (returns.n_before, returns.n_after) == (2, 0) and returns.small
    assert "new, worse" not in render_markdown(report)  # 2 -> 0 is better


def test_a_fix_without_data_says_so() -> None:
    report = build_report(inputs(sessions=(), fixes=(FixEvent(FIX, MANUAL, "quiet fix"),)))
    [v] = report.verifications
    assert v.metrics == ()
    assert "quiet fix. No data on either side yet (after-window: 24.0h)." in render_markdown(report)


def test_budget_states_are_not_stuck_hours() -> None:
    budget = StateRow(
        OpenState(
            "loop_breaker:budget:daily:farm",
            "loop_breaker",
            FIX - timedelta(hours=9),
            FIX - timedelta(hours=9),
            FIX + timedelta(hours=4),
            0,
            Severity.WARNING,
            "s",
        )
    )
    report = build_report(inputs(sessions=(), states=(budget,)))
    assert report.verifications[0].metrics == ()


def test_new_regressions_are_flagged_and_notes_escaped() -> None:
    states = (_state("stale_blocks", FIX + timedelta(hours=1), FIX + timedelta(hours=5)),)
    event = FixEvent(FIX, MANUAL, "line one\n# injected | heading", rule="ci_economy")
    markdown = render_markdown(build_report(inputs(fixes=(event,), states=states)))
    assert "new, worse" in markdown
    assert "\n# injected" not in markdown and "line one # injected \\| heading" in markdown
    assert "all causes; ci_economy has none" in markdown


def test_offender_history_is_bounded_by_repeat_days() -> None:
    from kelpie.traces.outputs import OutputSettings

    def xcode(day: int) -> list[Span]:
        start = datetime(2026, 3, day, 9, tzinfo=UTC)
        return [
            Span(
                "tr", f"x{day}-{i}", "execute_tool", SpanKind.TOOL, start + timedelta(minutes=i),
                start + timedelta(minutes=i), session_id="D-1",
                attributes={"kelpie.tool.command_prefix": "xcodebuild",
                            "kelpie.tool.output_chars": 160_000},
            )
            for i in range(3)
        ]  # fmt: skip

    spans = xcode(2)
    old = [
        Span(
            "tr", f"old-{i}", "execute_tool", SpanKind.TOOL,
            datetime(2026, 2, 28, 9, i, tzinfo=UTC), datetime(2026, 2, 28, 9, i, tzinfo=UTC),
            session_id="D-1",
            attributes={"kelpie.tool.command_prefix": "xcodebuild",
                        "kelpie.tool.output_chars": 160_000},
        )
        for i in range(3)
    ]  # fmt: skip
    report = build_report(
        inputs(spans=[*spans, *old], fixes=(), outputs=OutputSettings(repeat_days=2))
    )
    assert report.offenders == ()  # D-2 is outside repeat_days = 2


def test_kelpie_fix_times_and_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    path = tmp_path / "kelpie.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n[report]\ntimezone = "Asia/Tokyo"\n'
    )
    settings = load_settings(path)
    text, code = note_fix(settings, "too early")
    assert code == 2 and "run kelpie serve first" in text
    SqliteLedgerStore(tmp_path / "kelpie.db").close()
    now = lambda: datetime(2026, 3, 1, 12, tzinfo=UTC)  # noqa: E731
    assert note_fix(settings, "naive", when="2026-03-01T09:00", clock=now)[1] == 0
    text, code = note_fix(settings, "later", when="2026-03-02T09:00+00:00", clock=now)
    assert code == 2 and "future" in text
    with ReportStore(tmp_path / "kelpie.db") as store:
        [fix] = store.fixes(FIX - timedelta(days=1), FIX + timedelta(days=1))
    assert fix.at == datetime(2026, 3, 1, 0, tzinfo=UTC)  # 09:00 in Tokyo


def test_serve_records_a_config_change_after_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.server

    monkeypatch.delenv("KELPIE_DB", raising=False)
    monkeypatch.setattr(kelpie.server, "serve", lambda *a, **k: None)
    db = tmp_path / "kelpie.db"
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{db}"\n[traces]\nenabled = false\n')
    assert main(["--config", str(path), "serve"]) == 0
    path.write_text(f'[storage]\npath = "{db}"\n[traces]\nenabled = false\n# changed\n')
    assert main(["--config", str(path), "serve"]) == 0
    with ReportStore(db) as store:
        found = store.fixes(datetime(2020, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))
    assert [f.kind for f in found] == [CONFIG]
