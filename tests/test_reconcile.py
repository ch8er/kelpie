"""Reconciling the spend priced from traces with providers' cost reports."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import CostReportStep, load_settings, read_reconciliations, reconcile_spend
from kelpie.connectors.cost_reports import load_cost_reports
from kelpie.core.model import Reconciliation, ReportedCost, Snapshot, Span, SpanKind
from kelpie.core.spans import MemorySpanStore, SpanStore
from kelpie.core.spend import GapThreshold, reconcile
from kelpie.core.storage import SqliteCostReports, SqliteSpanStore
from kelpie.metrics import Metrics

NOW = datetime(2026, 10, 19, 9, tzinfo=UTC)
D1, D2 = date(2026, 10, 17), date(2026, 10, 18)
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
            chat("a", "claude-opus-5-5", 1_000_000, noon(D1)),
            chat("b", "claude-opus-5-5", 2_000_000, noon(D2)),
            chat("c", "gpt-5.5", 1_000_000, noon(D2)),
            chat("d", "mystery-model", 500, noon(D2)),  # no provider: not estimated
            chat("e", "claude-unknown-9", 700, noon(D2)),  # anthropic, no price
            chat("f", "claude-opus-5-5", 1_000_000, NOW - timedelta(hours=1)),  # today
        ],
        NOW,
    )
    return s


def test_usage_between_is_one_closed_day(store: SpanStore) -> None:
    start = datetime.combine(D2, datetime.min.time(), UTC)
    rows = store.usage_between(start, start + timedelta(days=1))
    assert sorted(r.input_tokens for r in rows) == [500, 700, 1_000_000, 2_000_000]
    assert store.usage_between(start, start) == ()


def test_reconcile_is_per_provider_and_closed_day() -> None:
    reported = [
        ReportedCost("anthropic", D2, 6.0, "claude-opus-5-5"),
        ReportedCost("anthropic", D2, 2.0),
        ReportedCost("anthropic", D1, 4.0),
        ReportedCost("openai", D2, 0.0),
        ReportedCost("other", D2, 5.0),  # no estimate: left out
    ]
    estimated = {
        ("anthropic", D2): (8.4, 10),
        ("anthropic", D1): (1.0, 0),
        ("openai", D2): (2.5, 0),
    }
    found = reconcile(reported, estimated, {"anthropic": GapThreshold(0.2, 1.0)})
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
    assert a1.large and a1.gap_ratio == pytest.approx(-0.75)


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
    assert a2.estimated_usd == pytest.approx(8.0) and a2.unpriced_tokens == 700
    assert not a2.large
    assert found[("openai", D2)].estimated_usd == pytest.approx(5.0)
    assert found[("anthropic", D1)].estimated_usd == pytest.approx(4.0)


class Reader:
    def __init__(self, costs: tuple[ReportedCost, ...]) -> None:
        self.costs = costs

    def read(self, now: datetime) -> tuple[ReportedCost, ...]:
        return self.costs

    def statuses(self) -> tuple[Any, ...]:
        from kelpie.connectors.cost_reports import ReportStatus

        return (ReportStatus("anthropic", True), ReportStatus("openai", False, "auth"))


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
    # without a span store the snapshot passes unchanged
    bare = CostReportStep(reader, None, settings, None, metrics)
    plain = Snapshot(now=NOW)
    assert bare(plain) is plain


def test_read_reconciliations_for_the_report(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    spans = SqliteSpanStore(db)
    spans.append_spans([chat("a", "claude-opus-5-5", 1_000_000, noon(D2))], NOW)
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
