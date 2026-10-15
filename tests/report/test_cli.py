"""``kelpie report``: config section, date handling, files and delivery failures."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kelpie.app import ConfigError, load_settings
from kelpie.cli import main
from kelpie.report.cli import run, sources


def config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n'
        f'[report]\ntimezone = "Europe/Berlin"\ndir = "{tmp_path / "reports"}"\n{extra}'
    )
    return path


def test_report_command_writes_markdown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    assert main(["--config", str(config(tmp_path)), "report", "--date", "2026-03-02"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Kelpie daily report: 2026-03-02")
    assert (tmp_path / "reports" / "kelpie-report-2026-03-02.md").read_text() == out


def test_bad_date_and_no_channels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    settings = load_settings(config(tmp_path))
    text, code = run(settings, "yesterday", False)
    assert code == 2 and "YYYY-MM-DD" in text
    text, code = run(settings, "2026-03-02", True)
    assert code == 1 and "no [[alerts.channels]]" in text


def test_default_day_is_yesterday_in_the_report_time_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    settings = load_settings(config(tmp_path))
    text, _ = run(settings, None, False, clock=lambda: datetime(2026, 3, 2, 23, 30, tzinfo=UTC))
    assert text.startswith("# Kelpie daily report: 2026-03-02")  # 00:30 on the 3rd in Berlin


def test_sources_follow_dry_run_and_budget_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    extra = (
        '[engine]\ndry_run = true\n[engine.rules.orphan_status]\nmode = "act"\n'
        '[prices]\n"m" = {input = 1.0, output = 2.0}\n'
        '[[budgets]]\nname = "farm"\nscope = "day"\nunit = "usd"\nsoft = 5.0\n'
        'mode = "subscription"\n'
    )
    src = sources(load_settings(config(tmp_path, extra)))
    assert src.estimated and src.modes["orphan_status"].value == "observe"


def test_unknown_report_key_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[report\]"):
        load_settings(config(tmp_path, "colour = 1\n"))
