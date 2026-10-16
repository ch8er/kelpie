"""``kelpie report``: config section, date handling, files and delivery failures."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from kelpie.alerts import Delivery, PermanentError
from kelpie.app import ConfigError, load_settings
from kelpie.cli import main
from kelpie.core.actions import Severity
from kelpie.metrics import Metrics
from kelpie.report.cli import report_settings, run, sources


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
    out, err, code = run(settings, "yesterday", False)
    assert code == 2 and "YYYY-MM-DD" in err and out == ""
    out, err, code = run(settings, "2026-03-02", True)
    assert code == 1 and "no [[alerts.channels]]" in err and out.startswith("# Kelpie")


def test_default_day_is_yesterday_in_the_report_time_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    settings = load_settings(config(tmp_path))
    text, _, _ = run(settings, None, False, clock=lambda: datetime(2026, 3, 2, 23, 30, tzinfo=UTC))
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


def test_the_report_reads_the_board_s_conditions_and_the_hold_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kelpie.core.model import Condition, ConditionKind, Snapshot
    from kelpie.report.run import load_inputs

    monkeypatch.delenv("KELPIE_DB", raising=False)
    extra = (
        '[engine.rules.bottlenecks]\nhold_labels = ["freeze"]\n'
        '[[engine.rules.bottlenecks.holds]]\nlabel = "merge-hold"\n'
    )
    src = sources(load_settings(config(tmp_path, extra)))
    assert src.hold_labels == ("freeze", "merge-hold")
    waits_on = (Condition("c-1", ConditionKind.CI_STATUS, "T-1"),)
    board = Snapshot(now=datetime(2026, 3, 3, tzinfo=UTC), conditions=waits_on)
    loaded = load_inputs(replace(src, db_path=None), date(2026, 3, 2), board)
    assert loaded.conditions == waits_on and loaded.hold_labels == ("freeze", "merge-hold")


def test_the_report_board_attaches_human_task_conditions_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.app
    import kelpie.report.cli as report_cli
    from kelpie.core.model import Block, Owner, OwnerKind, Snapshot, Status, Task

    now = datetime(2026, 3, 3, 1, tzinfo=UTC)

    class Board:
        name = "board"

        def snapshot(self, at: datetime) -> Snapshot:
            gate = Task("H-1", "approve", Status.READY, now, owner_id="person-a")
            asks = Block("waiting_human: approval in H-1", now)
            blocked = Task("T-1", "t", Status.BLOCKED, now, owner_id="dev-1", block=asks)
            owners = (Owner("person-a", "lead", OwnerKind.HUMAN), Owner("dev-1", "developer"))
            return Snapshot(now=at, tasks=(gate, blocked), owners=owners)

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the report called a network connector")

    monkeypatch.setattr(report_cli, "build_adapters", lambda settings: [Board()])
    monkeypatch.setattr(kelpie.app, "_github_steps", no_network)
    extra = (
        '[connectors.github]\nenabled = true\nrepos = ["example/app"]\n'
        "[connectors.human_tasks]\n"
        '[[connectors.human_tasks.block_patterns]]\npattern = "approval in (?P<task>\\\\S+)"\n'
    )
    settings = load_settings(config(tmp_path, extra))
    board = report_cli.report_board(settings, date(2026, 3, 2), lambda: now, Metrics())
    [c] = board.conditions
    assert c.kind.value == "human_task" and c.task_id == "T-1"


def test_unknown_report_key_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[report\]"):
        load_settings(config(tmp_path, "colour = 1\n"))


class FakeChannel:
    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.min_severity = Severity.INFO
        self.fail = fail
        self.messages: list[Any] = []

    def send(self, message: Any) -> None:
        if self.fail:
            raise PermanentError("refused")
        self.messages.append(message)


@dataclass(frozen=True)
class FakeAlerts:
    channels: tuple[FakeChannel, ...]

    def build(self, observer: Any = None) -> Delivery:
        return Delivery(list(self.channels), observer)


@pytest.mark.parametrize(("failing", "code"), [(False, 0), (True, 1)])
def test_send_reports_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: bool, code: int
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    good, other = FakeChannel("ok"), FakeChannel("other", fail=failing)
    settings = replace(load_settings(config(tmp_path)), alerts=FakeAlerts((good, other)))  # type: ignore[arg-type]
    out, err, got = run(settings, "2026-03-02", True)
    assert got == code and out.startswith("# Kelpie")
    assert len(good.messages) == 1 and "Kelpie daily report 2026-03-02" in str(good.messages[0])
    assert ("did not reach every channel" in err) is failing


def test_old_database_says_how_to_upgrade(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    db = sqlite3.connect(tmp_path / "kelpie.db")
    db.execute("PRAGMA user_version = 1")
    db.close()
    out, err, code = run(load_settings(config(tmp_path)), "2026-03-02", False)
    assert code == 1 and out == "" and "run kelpie serve" in err


def test_the_report_looks_back_as_far_as_its_day_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    hermes = f'[adapters.hermes]\ndb_path = "{tmp_path / "kanban.db"}"\nspool = false\n'
    settings = load_settings(config(tmp_path, hermes))  # [report] timezone = Europe/Berlin
    assert settings.hermes is not None and settings.hermes.lookback == timedelta(hours=24)
    # report of Mar 2 at 00:05 on Mar 3 (Berlin): back to Mar 1 00:00 minus a 1d window
    now = datetime(2026, 3, 2, 23, 5, tzinfo=UTC)
    wide = report_settings(settings, date(2026, 3, 2), now)
    assert wide.hermes is not None
    assert wide.hermes.lookback == now - datetime(
        2026, 2, 28, 0, tzinfo=ZoneInfo("Europe/Berlin")
    ) + timedelta(hours=1)
    assert timedelta(hours=73) < wide.hermes.lookback < timedelta(hours=74)
    # an older --date reaches further; a longer configured lookback is kept
    older = report_settings(settings, date(2026, 2, 20), now)
    assert older.hermes is not None and older.hermes.lookback > timedelta(days=11)
    long = config(tmp_path, hermes + 'lookback = "30d"\n')
    kept = report_settings(load_settings(long), date(2026, 3, 2), now)
    assert kept.hermes is not None and kept.hermes.lookback == timedelta(days=30)
    # serve's own settings are untouched
    assert settings.hermes.lookback == timedelta(hours=24)
    no_hermes = load_settings(config(tmp_path))
    assert report_settings(no_hermes, date(2026, 3, 2), now) is no_hermes
