"""Budgets in the app: [[budgets]]/[prices] config, spend from the span store in each
snapshot, /metrics, kelpie status and a smoke tick."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import (
    ConfigError,
    Settings,
    build_app,
    load_rules,
    load_settings,
    merged_snapshot,
    read_budgets,
    render_status,
)
from kelpie.cli import main
from kelpie.core.budgets import BudgetState
from kelpie.core.model import LimitHit, Session, Snapshot, Span, SpanKind
from kelpie.core.spans import MemorySpanStore, SpanStore, TokenUsage
from kelpie.core.storage import SqliteSpanStore
from kelpie.metrics import Metrics
from kelpie.traces.store import SpanWindow
from tests.fakes import FakeAdapter
from tests.test_app import assert_valid_exposition

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
H = timedelta(hours=1)

CONFIG = """
[[budgets]]
name = "farm-daily"
scope = "day"
unit = "usd"
soft = 5
hard = 10

[[budgets]]
name = "per-agent"
scope = "agent"
unit = "tokens"
hard = 2_000_000
burn_window = "30m"

[[budgets]]
name = "per-task"
scope = "task"
unit = "usd"
soft = 1
mode = "subscription"

[prices."model-a"]
input = 3
output = 15
cached_input = 0.3

[prices."model-b*"]
input = 1
output = 5
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


@dataclass
class BoardAdapter(FakeAdapter):
    sessions: tuple[Session, ...] = ()
    limit_hits: tuple[LimitHit, ...] = ()

    def snapshot(self, now: datetime) -> Snapshot:
        return Snapshot(now=now, sessions=self.sessions, limit_hits=self.limit_hits)


BOARD = BoardAdapter(
    name="board",
    sessions=(
        Session("run-1", "T-1", "agent-dev-1", NOW - 3 * H),
        Session("run-2", "T-2", "agent-dev-2", NOW - 3 * H),
    ),
)


def chat(
    span_id: str,
    parent: str | None,
    tokens: int,
    *,
    trace: str = "a" * 32,
    session: str = "run-1",
    model: str = "model-a",
    at: datetime = NOW - timedelta(minutes=10),
) -> Span:
    return Span(
        trace_id=trace,
        span_id=span_id,
        name="chat",
        kind=SpanKind.LLM,
        start=at,
        end=at + timedelta(seconds=3),
        parent_id=parent,
        session_id=session,
        task_id=None,  # the session's task applies
        model=model,
        input_tokens=tokens,
        output_tokens=tokens // 10,
        cached_tokens=tokens // 2,
    )


# A framework span wrapping a provider span (one call), a sibling call, another agent's
# call on an unpriced model, and an old call outside the burn window.
SPANS = [
    chat("framework", None, 1_000_000),
    chat("provider", "framework", 1_000_000),
    chat("sibling", None, 200_000),
    chat("other", None, 100_000, trace="b" * 32, session="run-2", model="unpriced"),
    chat("old", None, 300_000, trace="c" * 32, model="model-b-mini", at=NOW - 5 * H),
]


# --- config ------------------------------------------------------------------------------


def test_budgets_config_is_loaded(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    names = [b.name for b in settings.budgets.budgets]
    assert names == ["farm-daily", "per-agent", "per-task"]
    assert settings.budgets.budgets[2].window == settings.traces.retention
    assert settings.budgets.prices.price_for("model-b-large") is not None
    assert load_settings(None).budgets.budgets == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[budget]\nname = 'x'\n", "unknown config sections: ['budget']"),
        (CONFIG + "\n[traces]\nenabled = false\n", "need [traces] enabled"),
        (CONFIG + "\n[traces]\nretention = '12h'\ntotals_window = '12h'\n", "retention"),
        ("[[budgets]]\nname = 'x'\nscope = 'day'\nunit = 'usd'\nhard = 1\n", "need a [prices]"),
        (CONFIG.replace('unit = "tokens"', 'unit = "tokens"\ncolor = 1'), "keys: ['color']"),
        ("[prices]\nm = 1\n", "must be a table with input"),
    ],
)
def test_bad_budgets_config_is_a_config_error(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings(write(tmp_path, text))
    assert message in str(exc.value)


# --- spend in the snapshot ---------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SpanStore]:
    if request.param == "memory":
        yield MemorySpanStore()
        return
    s = SqliteSpanStore(tmp_path / "kelpie.db", batch=2)
    yield s
    s.close()


def statuses(store: SpanStore, tmp_path: Path, adapter: BoardAdapter = BOARD) -> dict:
    settings = load_settings(write(tmp_path, CONFIG))
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24))
    metrics = Metrics()
    source = merged_snapshot([adapter], metrics, lambda: NOW, window, settings.budgets)
    return {(s.budget, s.subject): s for s in source().budgets}


def test_spend_per_scope_counts_nested_calls_once(store: SpanStore, tmp_path: Path) -> None:
    store.append_spans(SPANS[1:], NOW)  # the provider span arrives before its parent
    store.append_spans(SPANS[:1], NOW)
    got = statuses(store, tmp_path)
    assert sorted(got) == [
        ("farm-daily", "farm"),
        ("per-agent", "agent-dev-1"),
        ("per-agent", "agent-dev-2"),
        ("per-task", "T-1"),
        ("per-task", "T-2"),
    ]
    a, b = (1_000_000 + 200_000), 300_000
    usd = (a / 2 * 3 + a / 2 * 0.3 + a / 10 * 15 + b * 1 + b / 10 * 5) / 1e6
    farm = got["farm-daily", "farm"]
    assert farm.spent == pytest.approx(usd)
    assert farm.cost_unknown_tokens == 110_000
    assert farm.state is BudgetState.OK and farm.remaining == pytest.approx(10 - usd)
    dev1 = got["per-agent", "agent-dev-1"]
    assert dev1.spent == 1_650_000  # (a + b) tokens in, a tenth of that out
    assert dev1.burn_rate == 1_320_000 * 2  # 30-minute burn window: per hour is twice
    assert dev1.runs_out_at == NOW + timedelta(hours=350_000 / dev1.burn_rate)
    assert dev1.session_ids == ("run-1",)
    task = got["per-task", "T-1"]
    assert task.state is BudgetState.SOFT and task.estimated
    assert got["per-task", "T-2"].spent == 0 and got["per-task", "T-2"].cost_unknown_tokens


def test_subscription_budget_forecasts_from_limit_hits(tmp_path: Path) -> None:
    store = MemorySpanStore()
    store.append_spans(SPANS, NOW)
    hits = (
        LimitHit("5h", NOW - 23 * H, NOW - 20 * H),
        LimitHit("5h", NOW - 17 * H, NOW - 10 * H),
        LimitHit("5h", NOW - 6 * H, NOW - H),
    )
    got = statuses(store, tmp_path, replace(BOARD, limit_hits=hits))
    assert got["per-task", "T-1"].runs_out_at == NOW + timedelta(hours=2.5)
    assert got["farm-daily", "farm"].runs_out_at != NOW + timedelta(hours=2.5)  # api mode


def test_usage_is_read_once_a_minute() -> None:
    store = MemorySpanStore()
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24))
    assert window.usage(NOW, [H]) == {H: ()}
    store.append_spans(SPANS[2:3], NOW)
    assert window.usage(NOW + timedelta(seconds=30), [H]) == {H: ()}  # cached
    # a window not read yet is fetched at once
    assert len(window.usage(NOW + timedelta(seconds=30), [H, 2 * H])[2 * H]) == 1
    usage = window.usage(NOW + timedelta(minutes=2), [H])[H]
    assert usage == (TokenUsage("run-1", None, "model-a", 1, 200_000, 20_000, 100_000),)


class BrokenUsage(MemorySpanStore):
    def usage_since(self, after: datetime) -> list[TokenUsage]:
        raise OSError("disk I/O error")


def test_usage_failure_keeps_the_tick(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    metrics = Metrics()
    metrics.set_traces(True)
    window = SpanWindow(BrokenUsage(), timedelta(minutes=30), timedelta(hours=24))
    snap = merged_snapshot([BOARD], metrics, lambda: NOW, window, settings.budgets)()
    assert snap.budgets == () and snap.sessions == BOARD.sessions
    assert "kelpie_span_store_errors_total 1" in metrics.render()
    assert "failed to read token usage" in caplog.text


# --- metrics, status and a smoke tick ----------------------------------------------------


def test_budget_metrics(tmp_path: Path) -> None:
    store = MemorySpanStore()
    store.append_spans(SPANS, NOW)
    settings = load_settings(write(tmp_path, CONFIG))
    metrics = Metrics()
    metrics.set_budgets_enabled(True)
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24))
    merged_snapshot([BOARD], metrics, lambda: NOW, window, settings.budgets)()
    text = metrics.render()
    assert_valid_exposition(text)
    farm = '{budget="farm-daily",subject="farm",unit="usd"}'
    assert f"kelpie_budget_spent{farm} " in text
    assert f"kelpie_budget_remaining{farm} " in text
    assert f"kelpie_budget_burn_rate_per_hour{farm} " in text
    dev1 = '{budget="per-agent",subject="agent-dev-1",unit="tokens"}'
    assert f"kelpie_budget_spent{dev1} 1650000.000000" in text
    assert f"kelpie_budget_runs_out_seconds{dev1} 477" in text  # 350k left at 2.64M/h
    assert 'kelpie_budget_runs_out_seconds{budget="per-task",subject="T-1",unit="usd"} 0' in text
    assert 'kelpie_budget_state{budget="per-task",subject="T-1",state="soft"} 1' in text
    assert 'kelpie_budget_cost_unknown_tokens{budget="farm-daily",subject="farm"} 110000' in text
    # without budgets nothing is rendered
    assert "kelpie_budget_" not in Metrics().render()


def test_status_shows_budgets_and_their_state(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    store = SqliteSpanStore(db)
    store.append_spans(SPANS, NOW)
    store.close()
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=db)
    current = read_budgets(settings, [BOARD], clock=lambda: NOW)
    assert current is not None and len(current) == 5
    text = render_status(settings, load_rules(settings.engine), [], current, now=NOW)
    assert "budgets:" in text
    assert "  farm-daily: day, usd, api, window 1 day, 0:00:00" in text
    assert "soft 5.00, hard 10.00" in text
    assert "    agent-dev-1: ok, spent 1,650,000 tokens, remaining 350,000, 2,640,000/h" in text
    assert "runs out 2026-03-02 12:07 UTC" in text
    assert "    T-1: soft, spent " in text and "usd (estimate)" in text
    assert "runs out now" in text
    assert "110,000 tokens without a price" in text
    unreadable = render_status(settings, load_rules(settings.engine), [], None, now=NOW)
    assert "spend unavailable: the span store cannot be read" in unreadable
    empty = render_status(settings, load_rules(settings.engine), [], (), now=NOW)
    assert "(no spend in the window)" in empty


def test_status_without_a_database_shows_no_spend(tmp_path: Path) -> None:
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=tmp_path / "none.db")
    current = read_budgets(settings, clock=lambda: NOW)
    assert current is not None and {s.spent for s in current} == {0}
    assert not (tmp_path / "none.db").exists()
    assert read_budgets(Settings()) == ()
    broken = replace(settings, db_path=tmp_path)  # a directory: cannot be opened
    assert read_budgets(broken, clock=lambda: NOW) is None


def test_cli_status_prints_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, CONFIG + f'\n[storage]\npath = "{tmp_path / "kelpie.db"}"\n')
    monkeypatch.delenv("KELPIE_DB", raising=False)
    assert main(["--config", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "budgets:" in out and "farm-daily" in out and "farm: ok, spent 0.00 usd" in out


def test_smoke_tick_with_budgets(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=tmp_path / "kelpie.db")
    clock = [NOW]
    app = build_app(settings, clock=lambda: clock[0])
    try:
        assert app.span_store is not None
        app.span_store.append_spans(SPANS, NOW)
        caplog.set_level(logging.INFO)
        app.loop.tick_once()
        snapshot = app.loop.snapshot_source()
        assert [s.subject for s in snapshot.budgets] == ["farm"]  # no adapter: no agents/tasks
        assert snapshot.budgets[0].spent > 0
        text = app.metrics.render()
        assert 'kelpie_budget_spent{budget="farm-daily",subject="farm",unit="usd"}' in text
        assert_valid_exposition(text)
    finally:
        app.close()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
