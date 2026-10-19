"""Reconciling the spend priced from traces with providers' cost reports."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import kelpie.app as app_module
from kelpie.app import (
    RECONCILE_EVERY,
    RECONCILE_RETRY,
    CostReportStep,
    build_app,
    load_settings,
    read_reconciliations,
    reconcile_spend,
)
from kelpie.connectors.cost_reports import load_cost_reports
from kelpie.core.model import Reconciliation, ReportedCost, Snapshot, Span, SpanKind
from kelpie.core.spans import MemorySpanStore, SpanStore
from kelpie.core.spend import Estimate, GapThreshold, reconcile
from kelpie.core.storage import SqliteCostReports, SqliteSpanStore
from kelpie.metrics import Metrics

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)
D0, D1, D2 = date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18)
CONFIG = """
[traces]
enabled = true

[connectors.cost_reports.anthropic]
kind = "anthropic"
admin_key_env = "KELPIE_TEST_ADMIN_KEY"
warn_gap = 0.2
warn_min_usd = 1

[connectors.cost_reports.openai]
kind = "openai"
admin_key_env = "KELPIE_TEST_ADMIN_KEY"
"""


def chat(span_id: str, model: str, tokens: int, at: datetime) -> Span:
    return Span("t" + span_id, span_id, "chat", SpanKind.LLM, at, at + timedelta(seconds=2),
                session_id="s", model=model, input_tokens=tokens, output_tokens=0)  # fmt: skip


def noon(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=12)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> SpanStore:
    s: SpanStore = (
        MemorySpanStore() if request.param == "memory" else SqliteSpanStore(tmp_path / "k.db")
    )
    # claude-opus-5-5: $4 per 1M input; gpt-5.5: $5 per 1M input
    s.append_spans(
        [
            chat("z", "claude-opus-5-5", 10, noon(D0)),  # the store holds D1 whole
            chat("a", "claude-opus-5-5", 1_000_000, noon(D1)),
            chat("b", "claude-opus-5-5", 2_000_000, noon(D2)),
            chat("c", "gpt-5.5", 1_000_000, noon(D2)),
            chat("d", "mystery-model", 500, noon(D2)),  # no provider: not estimated
            chat("e", "claude-unknown-9", 700, noon(D2)),  # anthropic, no price
            chat("g", "us.anthropic.claude-opus-5-5-v1:0", 1_000_000, noon(D2)),  # Bedrock's
            chat("h", "openrouter/gpt-5.5", 1_000_000, noon(D2)),  # a router's
            chat("f", "claude-opus-5-5", 1_000_000, NOW - timedelta(hours=1)),  # today
        ],
        NOW,
    )
    return s


def test_usage_between_is_one_closed_day(store: SpanStore) -> None:
    start = datetime.combine(D2, datetime.min.time(), UTC)
    rows = store.usage_between(start, start + timedelta(days=1))
    assert sorted(r.input_tokens for r in rows) == [500, 700] + [1_000_000] * 3 + [2_000_000]
    assert store.usage_between(start, start) == ()
    assert store.oldest_at() == noon(D0) + timedelta(seconds=2)


def test_a_call_over_midnight_is_the_day_it_ended(store: SpanStore) -> None:
    midnight = datetime.combine(D2, datetime.min.time(), UTC)
    late = replace(chat("m", "gpt-5.5", 42, midnight - timedelta(seconds=1)), session_id="late")
    store.append_spans([late], NOW)  # started on D1, ended on D2
    day1 = store.usage_between(midnight - timedelta(days=1), midnight)
    day2 = store.usage_between(midnight, midnight + timedelta(days=1))
    assert [r.input_tokens for r in day2 if r.session_id == "late"] == [42]
    assert not [r for r in day1 if r.session_id == "late"]


def test_an_empty_store_holds_no_day() -> None:
    assert MemorySpanStore().oldest_at() is None


def test_reconcile_is_per_provider_and_closed_day() -> None:
    reported = [
        ReportedCost("anthropic", D2, 6.0, "claude-opus-5-5"),
        ReportedCost("anthropic", D2, 2.0),
        ReportedCost("anthropic", D1, 4.0),
        ReportedCost("openai", D2, 0.0),
        ReportedCost("other", D2, 5.0),  # no estimate: left out
    ]
    estimated = {
        ("anthropic", D2): Estimate(8.4, 10),
        ("anthropic", D1): Estimate(1.0),
        ("openai", D2): Estimate(2.5, 0, 7),
    }
    found = reconcile(
        reported, estimated, {"anthropic": GapThreshold(0.2, 1.0)}, {"openai": "total"}
    )
    assert [(r.provider, r.day.date()) for r in found] == [
        ("anthropic", D2),
        ("openai", D2),
        ("anthropic", D1),
    ]  # newest day first, then by provider
    a2, o2, a1 = found
    assert (a2.provider, a2.day, a2.reported_usd, a2.gap_usd) == (
        "anthropic", datetime(2026, 10, 18, tzinfo=UTC), 8.0, pytest.approx(0.4))  # fmt: skip
    assert a2.gap_ratio == pytest.approx(0.05) and not a2.large and a2.unpriced_tokens == 10
    assert o2.gap_ratio is None and not o2.large  # $2.50 under the default $5 minimum
    assert (o2.bill_scope, o2.unknown_tokens, a2.bill_scope) == ("total", 7, "tokens")
    assert a1.large and a1.gap_ratio == pytest.approx(-0.75)


def test_no_gap_and_credits_never_warn() -> None:
    anything = {"p": GapThreshold(0.01, 0.0)}
    (same,) = reconcile([ReportedCost("p", D2, 3.0)], {("p", D2): Estimate(3.0)}, anything)
    assert same.gap_usd == 0 and not same.large
    (credit,) = reconcile([ReportedCost("p", D2, -50.0)], {("p", D2): Estimate(1.0)}, anything)
    assert credit.gap_usd == pytest.approx(51.0) and credit.gap_ratio is None
    assert not credit.large
    (free,) = reconcile([ReportedCost("p", D2, 0.0)], {("p", D2): Estimate(9.0)}, anything)
    assert free.large and free.gap_ratio is None  # billed nothing, traces show $9


def settings_for(tmp_path: Path, db: Path | None = None) -> Any:
    path = tmp_path / "kelpie.toml"
    path.write_text(CONFIG)
    return replace(load_settings(path), db_path=db)


def test_reconcile_spend_prices_closed_days_within_retention(
    tmp_path: Path, store: SpanStore
) -> None:
    settings = settings_for(tmp_path)
    costs = [
        ReportedCost("anthropic", D2, 8.0),
        ReportedCost("anthropic", D1, 4.0),
        ReportedCost("openai", D2, 5.0),
        ReportedCost("anthropic", NOW.date(), 1.0),  # today: not closed
        ReportedCost("anthropic", date(2026, 9, 1), 3.0),  # beyond the retention
    ]
    found = {(r.provider, r.day.date()): r for r in reconcile_spend(settings, store, costs, NOW)}
    assert set(found) == {("anthropic", D2), ("anthropic", D1), ("openai", D2)}
    a2 = found[("anthropic", D2)]
    # Bedrock's and the router's calls are billed there, not in these reports
    assert a2.estimated_usd == pytest.approx(8.0) and a2.unpriced_tokens == 700
    assert a2.unknown_tokens == 500 and not a2.large
    o2 = found[("openai", D2)]
    assert o2.estimated_usd == pytest.approx(5.0) and o2.bill_scope == "total"
    assert found[("anthropic", D1)].estimated_usd == pytest.approx(4.0)
    assert found[("anthropic", D1)].bill_scope == "tokens"


def test_a_day_is_reconciled_once_it_settled(tmp_path: Path, store: SpanStore) -> None:
    settings = settings_for(tmp_path)
    costs = [ReportedCost("anthropic", D2, 8.0)]
    midnight = datetime.combine(NOW.date(), datetime.min.time(), UTC)
    assert reconcile_spend(settings, store, costs, midnight + timedelta(hours=5)) == ()
    assert len(reconcile_spend(settings, store, costs, midnight + timedelta(hours=6))) == 1


def test_days_before_the_oldest_span_are_not_held_whole(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    store = MemorySpanStore()
    store.append_spans([chat("a", "claude-opus-5-5", 1_000_000, noon(D2))], NOW)
    costs = [ReportedCost("anthropic", D1, 4.0), ReportedCost("anthropic", D2, 9.0)]
    assert reconcile_spend(settings, store, costs, NOW) == ()  # D2 began before it
    assert reconcile_spend(settings, MemorySpanStore(), costs, NOW) == ()


def test_configured_models_count_another_platforms_calls(tmp_path: Path, store: SpanStore) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(CONFIG + '[spend.providers.anthropic]\nmodels = ["us.anthropic.*"]\n')
    settings = load_settings(path)
    (r,) = reconcile_spend(settings, store, [ReportedCost("anthropic", D2, 12.0)], NOW)
    assert r.estimated_usd == pytest.approx(12.0)  # the Bedrock id is this provider's here


class Reader:
    def __init__(self, costs: tuple[ReportedCost, ...], ok: bool | None = True) -> None:
        self.costs = costs
        self.ok = ok

    def read(self, now: datetime) -> tuple[ReportedCost, ...]:
        return self.costs

    def statuses(self) -> tuple[Any, ...]:
        from kelpie.connectors.cost_reports import ReportStatus

        return (ReportStatus("anthropic", self.ok), ReportStatus("openai", False, "auth"))


class CountingStore(MemorySpanStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.broken = False

    def usage_between(self, start: datetime, end: datetime) -> Any:
        self.reads += 1
        if self.broken:
            raise OSError("disk gone")
        return super().usage_between(start, end)


def test_the_step_attaches_reconciliations_and_metrics(tmp_path: Path, store: SpanStore) -> None:
    settings = settings_for(tmp_path)
    metrics = Metrics()
    reader = Reader((ReportedCost("anthropic", D2, 20.0),))
    step = CostReportStep(reader, None, settings, store, metrics)
    snap = step(Snapshot(now=NOW))
    (r,) = snap.reconciliations
    assert r.large and r.gap_usd == pytest.approx(-12.0)
    text = metrics.render()
    assert 'kelpie_cost_report_up{provider="anthropic"} 1' in text
    assert 'kelpie_cost_report_up{provider="openai"} 0' in text
    assert 'kelpie_spend_reported_usd{provider="anthropic"} 20.000000' in text
    assert 'kelpie_spend_estimated_usd{provider="anthropic"} 8.000000' in text
    assert 'kelpie_spend_gap_ratio{provider="anthropic"} -0.600000' in text
    # the same figures within the hour: not recomputed, still attached
    again = step(Snapshot(now=NOW + timedelta(minutes=5)))
    assert again.reconciliations == snap.reconciliations
    # without a span store the snapshot passes unchanged, and the gauges say nothing
    bare_metrics = Metrics()
    bare = CostReportStep(reader, None, settings, None, bare_metrics)
    plain = Snapshot(now=NOW)
    assert bare(plain) is plain
    text = bare_metrics.render()
    assert 'kelpie_cost_report_up{provider="anthropic"} 1' in text
    assert "kelpie_spend_reported_usd" not in text


def test_a_provider_not_read_yet_has_no_up_gauge(tmp_path: Path) -> None:
    metrics = Metrics()
    CostReportStep(Reader((), ok=None), None, settings_for(tmp_path), None, metrics)(
        Snapshot(now=NOW)
    )
    text = metrics.render()
    assert 'kelpie_cost_report_up{provider="anthropic"}' not in text
    assert 'kelpie_cost_report_up{provider="openai"} 0' in text


def test_the_step_recomputes_on_new_figures_and_hourly(tmp_path: Path) -> None:
    store = CountingStore()
    store.append_spans(
        [chat("z", "gpt-5.5", 1, noon(D0)), chat("a", "claude-opus-5-5", 1_000_000, noon(D2))],
        NOW,
    )
    reader = Reader((ReportedCost("anthropic", D2, 4.0),))
    step = CostReportStep(reader, None, settings_for(tmp_path), store)
    first = step(Snapshot(now=NOW)).reconciliations
    assert store.reads == 1 and first[0].gap_usd == 0
    step(Snapshot(now=NOW + timedelta(minutes=30)))
    assert store.reads == 1  # same figures within the hour
    reader.costs = (ReportedCost("anthropic", D2, 40.0),)
    changed = step(Snapshot(now=NOW + timedelta(minutes=31))).reconciliations
    assert store.reads == 2 and changed[0].large
    step(Snapshot(now=NOW + timedelta(minutes=31) + RECONCILE_EVERY))
    assert store.reads == 3  # an hour on: new spans may have arrived


def test_a_failed_reconciliation_keeps_the_last_and_retries_later(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = CountingStore()
    store.append_spans(
        [chat("z", "gpt-5.5", 1, noon(D0)), chat("a", "claude-opus-5-5", 1_000_000, noon(D2))],
        NOW,
    )
    reader = Reader((ReportedCost("anthropic", D2, 4.0),))
    step = CostReportStep(reader, None, settings_for(tmp_path), store)
    good = step(Snapshot(now=NOW)).reconciliations
    store.broken = True
    later = NOW + RECONCILE_EVERY
    with caplog.at_level("ERROR"):
        assert step(Snapshot(now=later)).reconciliations == good
        reader.costs = (ReportedCost("anthropic", D2, 5.0),)  # new figures: still waits
        assert step(Snapshot(now=later + timedelta(minutes=1))).reconciliations == good
        assert store.reads == 2
        step(Snapshot(now=later + RECONCILE_RETRY))
        assert store.reads == 3
    assert sum("could not be reconciled" in r.message for r in caplog.records) == 1
    store.broken = False
    again = step(Snapshot(now=later + 2 * RECONCILE_RETRY)).reconciliations
    assert again[0].reported_usd == 5.0


def test_serve_wires_the_span_store_and_metrics_into_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_TRACES_TOKEN", "synthetic-token")
    monkeypatch.setenv("KELPIE_TEST_ADMIN_KEY", "synthetic-admin-key")
    from kelpie.connectors.cost_reports import CostReportsSettings

    monkeypatch.setattr(CostReportsSettings, "build", lambda self, environ=None: Reader(()))
    made: list[CostReportStep] = []

    class Recording(CostReportStep):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(app_module, "CostReportStep", Recording)
    settings = settings_for(tmp_path, tmp_path / "kelpie.db")
    app = build_app(settings)
    try:
        (step,) = made
        assert step.spans is app.span_store and step.metrics is app.metrics
        assert step.settings is settings
    finally:
        app.close()


def test_read_reconciliations_for_the_report(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    spans = SqliteSpanStore(db)
    spans.append_spans(
        [chat("z", "gpt-5.5", 1, noon(D0)), chat("a", "claude-opus-5-5", 1_000_000, noon(D2))],
        NOW,
    )
    spans.close()
    reports = SqliteCostReports(db)
    reports.replace([ReportedCost("anthropic", D2, 4.0, "claude-opus-5-5")], NOW)
    reports.close()
    settings = settings_for(tmp_path, db)
    (r,) = read_reconciliations(settings, D1, D2, NOW)
    assert isinstance(r, Reconciliation) and r.gap_usd == pytest.approx(0.0)
    assert read_reconciliations(settings, D1, D1, NOW) == ()
    assert read_reconciliations(replace(settings, db_path=tmp_path / "none.db"), D1, D2) == ()
    assert "cannot open" in str(read_reconciliations(replace(settings, db_path=tmp_path), D1, D2))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("warn_gap = 0", "warn_gap must be a positive number"),
        ("warn_gap = true", "warn_gap must be a positive number"),
        ("warn_min_usd = -1", "warn_min_usd must be a non-negative number"),
    ],
)
def test_bad_thresholds(extra: str, message: str) -> None:
    import tomllib

    section = tomllib.loads(f'[a]\nkind = "openai"\nadmin_key_env = "K"\n{extra}\n')
    with pytest.raises(ValueError, match=message):
        load_cost_reports(section)


def test_thresholds_default() -> None:
    loaded = load_cost_reports({"a": {"kind": "openai", "admin_key_env": "K"}})
    assert loaded is not None and loaded.thresholds == {"a": GapThreshold(0.25, 5.0)}


def test_a_family_glob_does_not_claim_another_platforms_calls(
    tmp_path: Path, store: SpanStore
) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(CONFIG + '[spend.providers.anthropic]\nmodels = ["claude-*"]\n')
    (r,) = reconcile_spend(load_settings(path), store, [ReportedCost("anthropic", D2, 8.0)], NOW)
    assert r.estimated_usd == pytest.approx(8.0)  # not the Bedrock call


def test_the_retention_bounds_the_days_on_its_own(tmp_path: Path, store: SpanStore) -> None:
    settings = settings_for(tmp_path)
    settings = replace(settings, traces=replace(settings.traces, retention=timedelta(days=2)))
    store.append_spans([chat("old", "gpt-5.5", 1, NOW - timedelta(days=40))], NOW)
    costs = [ReportedCost("anthropic", D1, 4.0), ReportedCost("anthropic", D2, 8.0)]
    assert [r.day.date() for r in reconcile_spend(settings, store, costs, NOW)] == [D2]


def test_a_provider_keeps_its_last_day_until_a_newer_one_settles(tmp_path: Path) -> None:
    store = MemorySpanStore()
    store.append_spans(
        [chat("z", "gpt-5.5", 1, noon(D0)), chat("a", "claude-opus-5-5", 1_000_000, noon(D2))],
        NOW,
    )
    reader = Reader((ReportedCost("anthropic", D2, 40.0),))  # days = 1
    step = CostReportStep(reader, None, settings_for(tmp_path), store)
    (first,) = step(Snapshot(now=NOW)).reconciliations
    assert first.large
    today = NOW.date()
    reader.costs = (ReportedCost("anthropic", today, 1.0),)
    early = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC)
    early += timedelta(hours=3)
    assert step(Snapshot(now=early)).reconciliations == (first,)  # today has not settled
    (settled,) = step(Snapshot(now=early + timedelta(hours=4))).reconciliations
    assert settled.day.date() == today


def test_after_a_restart_the_stored_figures_stand_in(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    store = MemorySpanStore()
    store.append_spans(
        [chat("z", "gpt-5.5", 1, noon(D0)), chat("a", "claude-opus-5-5", 1_000_000, noon(D2))],
        NOW,
    )
    reports = SqliteCostReports(db)
    reports.replace([ReportedCost("anthropic", D2, 40.0), ReportedCost("elsewhere", D2, 1.0)], NOW)
    step = CostReportStep(Reader((), ok=None), reports, settings_for(tmp_path), store)
    (r,) = step(Snapshot(now=NOW)).reconciliations  # not read yet: the stored figure
    assert (r.provider, r.reported_usd, r.large) == ("anthropic", 40.0, True)
    reports.close()


def test_spend_gauges_show_without_an_up_gauge(tmp_path: Path) -> None:
    metrics = Metrics()
    metrics.set_cost_reports({}, [Reconciliation("p", noon(D2), 1.0, 2.0, -1.0, -0.5)])
    text = metrics.render()
    assert 'kelpie_spend_reported_usd{provider="p"} 2.000000' in text
    assert "kelpie_cost_report_up" not in text
