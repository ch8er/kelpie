"""Budgets in the app: [[budgets]]/[prices] config, spend from the span store in each
snapshot, /metrics, kelpie status and a smoke tick."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import (
    ConfigError,
    Settings,
    build_app,
    load_rules,
    load_settings,
    merged_snapshot,
    read_budgets,
    read_models,
    read_unpriced,
    render_spend_modes,
    render_status,
)
from kelpie.cli import main
from kelpie.core import storage
from kelpie.core.budgets import (
    BudgetEvaluation,
    BudgetScope,
    BudgetState,
    BudgetStatus,
    BudgetUnit,
)
from kelpie.core.model import LimitHit, Session, Snapshot, Span, SpanKind
from kelpie.core.spans import MemorySpanStore, SpanStore, TokenUsage
from kelpie.core.storage import SqliteSpanStore
from kelpie.doctor import check_prices, check_spend_modes
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
        (
            "[spend]\ndefault_prices = false\n[[budgets]]\nname = 'x'\nscope = 'day'\n"
            "unit = 'usd'\nhard = 1\n",
            "need prices",
        ),
        ("[spend]\ndefault_prices = 1\n", "default_prices must be true or false"),
        ("[spend]\nprices = 1\n", "unknown [spend] keys: ['prices']"),
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
    assert got["per-task", "T-1"].next_limit_at == NOW + timedelta(hours=2.5)
    assert got["farm-daily", "farm"].next_limit_at is None  # api mode


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


class FlakyUsage(MemorySpanStore):
    broken = False

    def usage_since(self, now: datetime, windows: Sequence[timedelta]) -> list:
        if self.broken:
            raise OSError("disk I/O error")
        return super().usage_since(now, windows)

    def record_attribution(self, *args: Any) -> int:
        if self.broken:
            raise OSError("database is locked")
        return super().record_attribution(*args)


def test_usage_failure_keeps_the_last_budgets_marked_stale(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    metrics = Metrics()
    metrics.set_traces(True)
    metrics.set_budgets_enabled(True)
    store = FlakyUsage()
    store.append_spans(SPANS, NOW)
    clock = [NOW]
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24))
    source = merged_snapshot([BOARD], metrics, lambda: clock[0], window, settings.budgets)
    good = source()
    assert good.budgets_ok and len(good.budgets) == 5
    assert "kelpie_budget_stale 0" in metrics.render()
    store.broken = True
    clock[0] = NOW + timedelta(minutes=2)  # past the once-a-minute cache
    snap = source()
    assert not snap.budgets_ok and snap.budgets == good.budgets  # last good, stale
    assert snap.sessions == BOARD.sessions
    text = metrics.render()
    assert "kelpie_span_store_errors_total 2" in text  # the write and the read
    assert "kelpie_budget_stale 1" in text
    assert 'kelpie_budget_spent{budget="farm-daily",subject="farm",unit="usd"}' in text
    assert "failed to read spend" in caplog.text
    assert "failed to record session owners" in caplog.text
    store.broken = False
    clock[0] = NOW + timedelta(minutes=4)
    assert source().budgets_ok and "kelpie_budget_stale 0" in metrics.render()


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_spend_stays_attributed_after_the_adapter_drops_a_session(
    tmp_path: Path, kind: str
) -> None:
    store: SpanStore = (
        MemorySpanStore() if kind == "memory" else SqliteSpanStore(tmp_path / "kelpie.db")
    )
    store.append_spans(SPANS, NOW)
    settings = load_settings(write(tmp_path, CONFIG))
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24))
    clock = [NOW]
    board = BoardAdapter(name="board", sessions=BOARD.sessions)
    metrics = Metrics()
    metrics.set_budgets_enabled(True)
    source = merged_snapshot([board], metrics, lambda: clock[0], window, settings.budgets)
    first = {(s.budget, s.subject): s.spent for s in source().budgets}
    board.sessions = ()  # finished runs leave the board
    clock[0] = NOW + timedelta(minutes=2)
    later = {(s.budget, s.subject): s.spent for s in source().budgets}
    assert later["per-agent", "agent-dev-1"] == first["per-agent", "agent-dev-1"] > 0
    assert later["per-task", "T-1"] == first["per-task", "T-1"] > 0
    assert 'kelpie_budget_unattributed_tokens{budget="per-agent"} 0' in metrics.render()
    close = getattr(store, "close", None)
    if callable(close):
        close()


def test_unattributed_tokens_metric(tmp_path: Path) -> None:
    store = MemorySpanStore()
    store.append_spans(SPANS, NOW)
    settings = load_settings(write(tmp_path, CONFIG))
    metrics = Metrics()
    metrics.set_budgets_enabled(True)
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24))
    merged_snapshot([], metrics, lambda: NOW, window, settings.budgets)()  # no owners known
    text = metrics.render()
    assert 'kelpie_budget_unattributed_tokens{budget="per-agent"} 1760000' in text
    assert 'kelpie_budget_unattributed_tokens{budget="farm-daily"}' not in text


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
    assert not isinstance(current, str) and len(current) == 5
    text = render_status(settings, load_rules(settings.engine), [], current, now=NOW)
    assert "budgets:" in text
    assert "  farm-daily: day, usd, api, window 1 day, 0:00:00" in text
    assert "soft 5.00, hard 10.00" in text
    assert "    agent-dev-1: ok, spent 1,650,000 tokens, remaining 350,000, 2,640,000/h" in text
    assert "runs out 2026-03-02 12:07 UTC" in text
    assert "    T-1: soft, spent " in text and "usd (estimate)" in text
    assert "runs out now" in text
    assert "110,000 tokens without a price" in text
    unreadable = render_status(settings, load_rules(settings.engine), [], "it broke", now=NOW)
    assert "spend unavailable: it broke" in unreadable
    empty = render_status(settings, load_rules(settings.engine), [], (), now=NOW)
    assert "(no spend in the window)" in empty


def test_status_without_a_database_shows_no_spend(tmp_path: Path) -> None:
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=tmp_path / "none.db")
    current = read_budgets(settings, clock=lambda: NOW)
    assert not isinstance(current, str) and {s.spent for s in current} == {0}
    assert not (tmp_path / "none.db").exists()
    assert read_budgets(Settings()) == ()
    broken = replace(settings, db_path=tmp_path)  # a directory: cannot be opened
    assert "cannot open the span store" in str(read_budgets(broken, clock=lambda: NOW))


def test_status_reads_an_old_database_without_upgrading_it(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    db = sqlite3.connect(db_path)
    db.executescript("".join(storage.MIGRATIONS[v] for v in (1, 2, 3)) + "PRAGMA user_version = 3;")
    db.close()
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=db_path)
    problem = read_budgets(settings, clock=lambda: NOW)
    assert problem == "the database predates budgets; run kelpie serve once to upgrade it"
    db = sqlite3.connect(db_path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3


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


def test_task_series_are_bounded() -> None:
    def status(subject: str, spent: float, scope: BudgetScope = BudgetScope.TASK) -> Any:
        state = BudgetState.SOFT if subject == "T-hot" else BudgetState.OK
        return BudgetStatus("b", scope, subject, BudgetUnit.TOKENS, spent, state)

    tasks = [status(f"T-{i:02}", 100 + i) for i in range(30)]
    agents = [status(f"agent-{i:02}", i, BudgetScope.AGENT) for i in range(30)]
    metrics = Metrics()
    metrics.set_budgets_enabled(True)
    result = BudgetEvaluation((*tasks, status("T-hot", 1), *agents))
    metrics.set_budgets(result, NOW)
    text = metrics.render()
    spent = [line for line in text.splitlines() if line.startswith("kelpie_budget_spent{")]
    assert len(spent) == 20 + 1 + 30  # top 20 tasks, the one past a threshold, all agents
    assert 'subject="T-29"' in text and 'subject="T-09"' not in text
    assert 'subject="T-hot"' in text and 'subject="agent-00"' in text


def test_status_and_doctor_name_models_without_a_price(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    store = SqliteSpanStore(db)
    store.append_spans(
        [*SPANS, chat("cc", None, 5_000, trace="d" * 32, model="claude-haiku-4-5-20251001")],
        NOW,
    )
    store.close()
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=db)
    assert read_unpriced(settings, NOW) == (("unpriced", 110_000),)  # haiku: default table
    text = render_status(settings, load_rules(settings.engine), unpriced=(("unpriced", 110_000),))
    assert "prices:   2 [prices] entries, default table of " in text
    assert "checked 2026-10-19" in text
    assert "warning: models without a price (their cost is unknown): unpriced (110,000" in text
    check = check_prices(settings, NOW)
    assert check.status == "WARN" and "unpriced (110,000 tokens)" in check.detail
    assert '[prices."unpriced"]' in check.fix
    # priced in config: nothing to warn about
    priced = replace(
        settings,
        budgets=load_settings(
            write(tmp_path, CONFIG + '\n[prices."unpriced"]\ninput = 1\noutput = 2\n')
        ).budgets,
    )
    assert read_unpriced(priced, NOW) == ()
    assert check_prices(priced, NOW).status == "PASS"
    # without the default table the Claude model has no price either
    off = load_settings(write(tmp_path, "[spend]\ndefault_prices = false\n" + CONFIG))
    off = replace(off, db_path=db)
    assert [m for m, _ in read_unpriced(off, NOW)] == ["unpriced", "claude-haiku-4-5"]  # family
    assert "default table off ([spend] default_prices = false)" in render_status(
        off, load_rules(off.engine)
    )
    # nothing to read
    assert read_unpriced(replace(settings, db_path=tmp_path / "none.db"), NOW) == ()
    assert check_prices(replace(settings, db_path=tmp_path / "none.db"), NOW).status == "SKIP"
    assert check_prices(replace(settings, db_path=None), NOW).status == "SKIP"
    assert check_prices(replace(settings, db_path=tmp_path), NOW).status == "SKIP"  # unreadable
    assert "cannot open" in str(read_unpriced(replace(settings, db_path=tmp_path), NOW))
    assert "cannot be listed: x" in render_status(
        settings, load_rules(settings.engine), unpriced="x"
    )


def test_unpriced_models_on_an_old_database(tmp_path: Path) -> None:
    db_path = tmp_path / "old.db"
    db = sqlite3.connect(db_path)
    db.executescript("".join(storage.MIGRATIONS[v] for v in (1, 2, 3)) + "PRAGMA user_version = 3;")
    db.close()
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=db_path)
    assert "predates budgets" in str(read_unpriced(settings, NOW))
    assert check_prices(settings, NOW).status == "SKIP"


def test_status_and_doctor_show_spend_modes(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    store = SqliteSpanStore(db)
    store.append_spans(
        [*SPANS, chat("cc", None, 5_000, trace="d" * 32, model="claude-haiku-4-5-20251001")],
        NOW,
    )
    store.close()
    text = (
        CONFIG.replace('mode = "subscription"\n', "") + '\n[spend.providers.openai]\nmode = "api"\n'
    )
    settings = replace(load_settings(write(tmp_path, text)), db_path=db)
    models = read_models(settings, NOW)
    assert dict(models)["claude-haiku-4-5-20251001"] == 5_500
    lines = "\n".join(render_spend_modes(settings, models))
    assert "  openai: api (mode set in [spend.providers.openai])" in lines
    assert "  anthropic: api, 5,500 tokens in the last 7d" in lines
    assert "budget 'farm-daily' counts usd as a bill" in lines
    assert "(no provider known): model-a, model-b-mini, unpriced" in lines
    check = check_spend_modes(settings, NOW)
    assert check.status == "WARN" and "no provider known for the models model-a" in check.detail
    assert '[spend.providers.<name>] models = ["model-a"]' in check.fix
    # a provider whose mode nothing tells
    bare = replace(load_settings(write(tmp_path, "[traces]\nenabled = true\n")), db_path=db)
    check = check_spend_modes(bare, NOW)
    assert check.status == "WARN" and "no spend mode for anthropic" in check.detail
    assert '[spend.providers.anthropic] mode = "api"' in check.fix
    # all known
    known = replace(
        load_settings(
            write(
                tmp_path,
                '[spend.providers.anthropic]\nmode = "subscription"\nlimit_tokens = 1000000\n'
                'limit_window = "5h"\n[spend.providers.lab]\nmode = "api"\n'
                'models = ["model-*", "unpriced"]\n',
            )
        ),
        db_path=db,
    )
    check = check_spend_modes(known, NOW)
    assert check.status == "PASS" and "anthropic: subscription" in check.detail
    assert "limit 1,000,000 tokens per 5h" in "\n".join(render_spend_modes(known, models))
    # nothing configured, nothing seen
    assert check_spend_modes(Settings(), NOW).status == "SKIP"
    assert render_spend_modes(Settings()) == []
    full = render_status(settings, load_rules(settings.engine), models=models)
    assert "spend modes:" in full
