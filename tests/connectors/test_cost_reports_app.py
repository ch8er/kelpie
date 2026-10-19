"""Cost reports in the app: config wiring, spend mode, kelpie status, doctor, the step."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from kelpie import net
from kelpie.app import (
    ConfigError,
    CostReportStep,
    connector_steps,
    load_rules,
    load_settings,
    render_status,
)
from kelpie.connectors.cost_reports import CostReportConfig, CostReportSource, ReportKind
from kelpie.core.model import ReportedCost, Snapshot
from kelpie.core.spend import SpendMode
from kelpie.core.storage import SqliteCostReports
from kelpie.doctor import check_cost_reports
from kelpie.metrics import Metrics

NOW = datetime(2026, 10, 19, 15, tzinfo=UTC)
CONFIG = """
[connectors.cost_reports.anthropic]
kind = "anthropic"
admin_key_env = "KELPIE_TEST_ADMIN_KEY"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


def test_a_cost_report_makes_its_provider_api(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    assert settings.cost_reports is not None
    mode = settings.spend.mode_for("anthropic")
    assert (mode.mode, mode.reason) == (SpendMode.API, "its cost report is configured")
    assert settings.spend.mode_for("openai").mode is SpendMode.UNKNOWN


def test_bad_section_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="kind must be one of"):
        load_settings(write(tmp_path, '[connectors.cost_reports.x]\nkind = "y"\n'))


def test_status_says_whether_the_key_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    monkeypatch.delenv("KELPIE_TEST_ADMIN_KEY", raising=False)
    text = render_status(settings, load_rules(settings.engine))
    assert "cost reports: NOT ready: [connectors.cost_reports.anthropic]: " in text
    monkeypatch.setenv("KELPIE_TEST_ADMIN_KEY", "sk-ant-admin01-x")
    text = render_status(settings, load_rules(settings.engine))
    assert "cost reports: anthropic (anthropic https://api.anthropic.com, every 1h, last 7" in text
    assert "sk-ant-admin01-x" not in text
    plain = render_status(load_settings(None), load_rules(load_settings(None).engine))
    assert "cost reports: disabled (no [connectors.cost_reports])" in plain


def fake_get(status: int, body: Any) -> Any:
    def get(url: str, path: str, headers: Any, timeout: float, **kw: Any) -> net.Response:
        return net.Response(status, {}, json.dumps(body).encode())

    return get


def test_doctor_reads_the_last_day(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    env = {"KELPIE_TEST_ADMIN_KEY": "sk-ant-admin01-x"}
    bucket = {"starting_at": "2026-10-18T00:00:00Z", "results": [
        {"amount": "1234", "cost_type": "tokens", "model": "claude-opus-5-5"}]}  # fmt: skip
    (ok,) = check_cost_reports(settings, NOW, env, fake_get(200, {"data": [bucket]}))
    assert ok.status == "PASS" and ok.detail == "read: $12.34 on 2026-10-18"
    (refused,) = check_cost_reports(settings, NOW, env, fake_get(401, {}))
    assert refused.status == "FAIL" and "admin key" in refused.fix
    (missing,) = check_cost_reports(settings, NOW, {}, fake_get(200, {}))
    assert missing.status == "FAIL" and "is not set" in missing.detail
    assert "export KELPIE_TEST_ADMIN_KEY" in missing.fix
    assert check_cost_reports(load_settings(None), NOW) == []


def test_the_step_stores_new_readings_only(tmp_path: Path) -> None:
    calls = []

    def get(url: str, path: str, headers: Any, timeout: float, **kw: Any) -> net.Response:
        calls.append(path)
        body = {"data": [{"starting_at": "2026-10-18T00:00:00Z", "results": []}]}
        return net.Response(200, {}, json.dumps(body).encode())

    report = CostReportConfig("anthropic", ReportKind.ANTHROPIC, "https://api.test", "k", days=1)
    store = SqliteCostReports(tmp_path / "k.db")
    step = CostReportStep(CostReportSource([report], get, background=False), store)
    snap = Snapshot(now=NOW)
    assert step(snap) is snap
    assert store.read(date(2026, 1, 1)) == (ReportedCost("anthropic", date(2026, 10, 18), 0.0),)
    step(snap)
    assert len(calls) == 1  # not due again yet


def test_only_serve_reads_cost_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """kelpie why --refresh (connector_steps) never opens the database or uses the key."""
    db = tmp_path / "k.db"
    settings = replace(load_settings(write(tmp_path, CONFIG)), db_path=db)
    monkeypatch.delenv("KELPIE_TEST_ADMIN_KEY", raising=False)
    steps, _ = connector_steps(settings, Metrics(), lambda: NOW)
    assert "cost_reports" not in [name for name, _ in steps] and not db.exists()
    with pytest.raises(ConfigError, match="is not set"):
        cost_report_steps(settings)
    monkeypatch.setenv("KELPIE_TEST_ADMIN_KEY", "sk-ant-admin01-x")
    assert [name for name, _ in cost_report_steps(settings)] == ["cost_reports"]


def test_the_step_stores_only_providers_that_changed(tmp_path: Path) -> None:
    class Reader:
        costs: tuple[ReportedCost, ...] = ()

        def read(self, now: datetime) -> tuple[ReportedCost, ...]:
            return self.costs

    reader = Reader()
    store = SqliteCostReports(tmp_path / "k.db")
    step = CostReportStep(reader, store)
    a = ReportedCost("a", date(2026, 10, 18), 1.0)
    reader.costs = (a,)
    step(Snapshot(now=NOW))
    reader.costs = (a, ReportedCost("b", date(2026, 10, 18), 2.0))
    step(Snapshot(now=NOW))
    assert len(store.read(date(2026, 1, 1))) == 2
