"""``kelpie snooze``, ``kelpie unsnooze`` and ``kelpie snoozes`` over a synthetic Hermes board."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import Settings, build_app, find_config, load_settings
from kelpie.cli import main
from kelpie.core.model import Snooze
from kelpie.core.storage import SqliteLedgerStore
from kelpie.snooze import PREDATES, _board, default_by, list_snoozes, local_board, snooze, unsnooze
from tests.adapters.kanban import Board

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)

CONFIG = """
[storage]
path = {db}

[adapters.hermes]
db_path = {board}
spool = false
git_state = false
lookback = "30d"
{extra}
"""


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture
def farm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    monkeypatch.delenv("KELPIE_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("kelpie.snooze.utc_now", lambda: NOW)  # every exact time below
    board = Board(tmp_path / "kanban.db")
    board.task("t_blocked", "blocked", ago(days=1), assignee="dev")
    board.event("t_blocked", "blocked", ago(hours=6), {"reason": "R2 for backups: CEO decides"})
    board.task("t_child", "todo", ago(days=1), assignee="dev")
    board.link("t_blocked", "t_child")
    board.task("t_marked", "blocked", ago(days=1), assignee="dev")
    board.event("t_marked", "blocked", ago(hours=2), {"reason": "manual QA until:2026-10-23"})
    board.close()
    write_config(tmp_path)
    SqliteLedgerStore(tmp_path / "kelpie.db").close()  # kelpie serve ran once
    return tmp_path


def write_config(root: Path, extra: str = "") -> None:
    (root / "kelpie.toml").write_text(
        CONFIG.format(
            db=json.dumps(str(root / "kelpie.db")),
            board=json.dumps(str(root / "kanban.db")),
            extra=extra,
        )
    )


def stored(root: Path) -> list[Snooze]:
    store = SqliteLedgerStore(root / "kelpie.db")
    try:
        return store.snoozes()
    finally:
        store.close()


def settings() -> Settings:
    return load_settings(find_config(None))


def test_snooze_stores_and_reports_leaving_the_baseline_to_serve(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The work behind the task is not counted offline (the board here has no connectors'
    conditions): kelpie serve records it on its first tick (#176)."""
    code = main(["snooze", "t_blocked", "--until", "3d", "--reason", "release", "--by", "ceo"])
    out, err = capsys.readouterr()
    assert code == 0 and err == ""
    (s,) = stored(farm)
    assert (s.task_id, s.reason, s.by, s.source, s.behind_at_snooze) == (
        "t_blocked",
        "release",
        "ceo",
        "cli",
        None,
    )
    assert (s.until, s.created_at) == (NOW + timedelta(days=3), NOW)
    assert out == "snoozed: t_blocked until 2026-10-22 12:00 UTC (release; by ceo)\n"


def test_a_bare_date_means_through_that_day_and_replaces(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["snooze", "t_blocked", "--until", "2d"]) == 0
    assert main(["snooze", "t_blocked", "--until", "2026-10-25"]) == 0
    out = capsys.readouterr().out.splitlines()[-1]
    assert out.startswith("snooze replaced: t_blocked until 2026-10-26 00:00 UTC")
    (s,) = stored(farm)
    assert s.until == datetime(2026, 10, 26, tzinfo=UTC)


def test_report_timezone_reads_dates_and_times(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(farm, '[report]\ntimezone = "Asia/Tokyo"\n')
    assert main(["snooze", "t_blocked", "--until", "2026-10-25"]) == 0
    assert stored(farm)[0].until == datetime(2026, 10, 25, 15, tzinfo=UTC)  # 26th, 00:00 JST
    out = capsys.readouterr().out
    assert "until 2026-10-26 00:00 JST (2026-10-25 15:00 UTC)" in out
    assert main(["snooze", "t_blocked", "--until", "2026-10-25T09:00"]) == 0
    assert stored(farm)[0].until == datetime(2026, 10, 25, 0, tzinfo=UTC)
    assert main(["snooze", "t_blocked", "--until", "2026-10-25T09:00+00:00"]) == 0
    assert stored(farm)[0].until == datetime(2026, 10, 25, 9, tzinfo=UTC)
    assert "2026-10-25 18:00 JST (2026-10-25 09:00 UTC)" in capsys.readouterr().out


def test_by_defaults_to_the_os_user(farm: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kelpie.snooze.getpass.getuser", lambda: "operator")
    assert main(["snooze", "t_blocked", "--until", "1d"]) == 0
    assert stored(farm)[0].by == "operator"

    def no_user() -> str:
        raise OSError("no user")

    monkeypatch.setattr("kelpie.snooze.getpass.getuser", no_user)
    assert default_by() is None


def test_an_unknown_task_is_snoozed_with_a_warning(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["snooze", "t_gone", "--until", "1d"]) == 0
    err = capsys.readouterr().err
    assert "warning: t_gone is not on the board; snoozed anyway" in err
    assert stored(farm)[0].behind_at_snooze is None


def test_an_unreadable_board_skips_the_check(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (farm / "kanban.db").unlink()
    assert main(["snooze", "t_gone", "--until", "1d"]) == 0
    assert capsys.readouterr().err == ""
    assert stored(farm)[0].behind_at_snooze is None


def test_the_board_is_read_board_only_and_never_through_a_hub_or_the_network(
    farm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kelpie.adapters.hermes.adapter import HermesAdapter

    seen: list[HermesAdapter] = []
    real = HermesAdapter.snapshot

    def spy(self: HermesAdapter, now: datetime) -> object:
        seen.append(self)
        return real(self, now)

    monkeypatch.setattr(HermesAdapter, "snapshot", spy)
    base = settings()
    assert _board(base, NOW) is not None
    (adapter,) = seen
    assert (adapter.git_state, adapter.spool, adapter.tools) == (False, None, None)
    assert local_board(Settings(db_path=base.db_path), NOW) == "no local adapter"
    hub = object.__new__(type("FakeHub", (), {}))
    assert "[hub]" in str(local_board(Settings(hub=hub), NOW))  # type: ignore[arg-type]
    issues = object.__new__(type("FakeIssues", (), {"enabled": True}))
    assert _board(replace_settings(base, github_issues=issues), NOW) is None
    assert _board(replace_settings(base, linear=issues), NOW) is None


def replace_settings(base: Settings, **kw: object) -> Settings:
    from dataclasses import replace

    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("until", "message"),
    [
        ("soon", "--until must be a date"),
        ("2020-01-01", "is not in the future"),
        ("2026-10-18", "is not in the future"),
        ("91d", "is more than 90 days ahead ([snooze] max_days)"),
        ("9999-12-31T23:00-05:00", "is out of range"),
    ],
)
def test_a_bad_until_exits_2(
    farm: Path, capsys: pytest.CaptureFixture[str], until: str, message: str
) -> None:
    assert main(["snooze", "t_blocked", "--until", until]) == 2
    assert message in capsys.readouterr().err
    assert stored(farm) == []


def test_the_date_exactly_max_days_ahead_is_the_last_allowed(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["snooze", "t_blocked", "--until", "2027-01-18"]) == 2
    err = capsys.readouterr().err
    assert "--until 2027-01-18 (through that day) is more than 90 days ahead" in err
    assert err.endswith("the last date allowed is 2027-01-17\n")
    assert main(["snooze", "t_blocked", "--until", "2027-01-17"]) == 0  # 90 days after today
    assert stored(farm)[0].until == datetime(2027, 1, 18, tzinfo=UTC)
    assert main(["snooze", "t_blocked", "--until", "91d"]) == 2
    assert "the latest allowed is 2027-01-17 12:00 UTC" in capsys.readouterr().err


def test_board_only_reads_an_openclaw_board_without_its_spool(tmp_path: Path) -> None:
    from kelpie.adapters.openclaw import OpenClawAdapter
    from kelpie.app import board_only

    assert board_only(OpenClawAdapter(state_dir=tmp_path, spool=tmp_path / "s")).spool is None  # type: ignore[attr-defined]


def test_today_is_still_in_the_future(farm: Path) -> None:
    assert main(["snooze", "t_blocked", "--until", "2026-10-19"]) == 0  # through today
    assert stored(farm)[0].until == datetime(2026, 10, 20, tzinfo=UTC)


def test_max_days_comes_from_the_config(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_config(farm, "[snooze]\nmax_days = 7\n")
    assert main(["snooze", "t_blocked", "--until", "8d"]) == 2
    assert "more than 7 days ahead" in capsys.readouterr().err
    assert main(["snooze", "t_blocked", "--until", "7d"]) == 0


def test_without_a_database_exit_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    monkeypatch.delenv("KELPIE_DB", raising=False)
    (tmp_path / "kelpie.toml").write_text(
        f"[storage]\npath = {json.dumps(str(tmp_path / 'k.db'))}\n"
    )
    s = settings()
    _out, err, code = snooze(s, "T-1", "1d", clock=lambda: NOW)
    assert code == 2 and "no database" in err and "run kelpie serve first" in err
    assert unsnooze(s, "T-1")[2] == 2
    assert list_snoozes(s) == ("no active snoozes\n", "", 0)
    memory = Settings(db_path=None)
    for result in (
        snooze(memory, "T-1", "1d", clock=lambda: NOW),
        unsnooze(memory, "T-1"),
        list_snoozes(memory),
    ):
        assert result[2] == 2 and ":memory:" in result[1]
    assert not (tmp_path / "k.db").exists()


def test_an_empty_task_id_exits_2(farm: Path) -> None:
    assert snooze(settings(), "  ", "1d")[2] == 2


def test_config_error_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    (tmp_path / "kelpie.toml").write_text("[snooze]\nmax_days = 400\n")
    for argv in (["snooze", "T-1", "--until", "1d"], ["unsnooze", "T-1"], ["snoozes"]):
        assert main(argv) == 2


def test_a_database_that_cannot_be_written_exits_1(
    farm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(SqliteLedgerStore, "put_snooze", broken)
    _out, err, code = snooze(settings(), "t_blocked", "1d")
    assert code == 1 and "cannot write" in err and "database is locked" in err
    monkeypatch.setattr(SqliteLedgerStore, "delete_snooze", broken)
    assert unsnooze(settings(), "t_blocked")[2] == 1
    monkeypatch.setattr("kelpie.snooze.read_snoozes", broken)
    _out, err, code = list_snoozes(settings())
    assert code == 1 and "cannot read" in err


def test_a_snooze_removed_meanwhile_is_reported(
    farm: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = SqliteLedgerStore.prune_snoozes

    def prune_and_unsnooze(self: SqliteLedgerStore, now: datetime) -> int:
        self.delete_snooze("t_blocked")  # another kelpie unsnooze got in between
        return real(self, now)

    monkeypatch.setattr(SqliteLedgerStore, "prune_snoozes", prune_and_unsnooze)
    out, err, code = snooze(settings(), "t_blocked", "1d", by="ceo")
    assert code == 0 and "removed again meanwhile" in err
    assert out.startswith("snoozed: t_blocked until 2026-10-20 12:00 UTC")


def test_unsnooze(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["snooze", "t_blocked", "--until", "1d"]) == 0
    capsys.readouterr()
    assert main(["unsnooze", "t_blocked"]) == 0
    assert capsys.readouterr().out == "unsnoozed t_blocked\n"
    assert stored(farm) == []
    assert main(["unsnooze", "t_blocked"]) == 1
    assert "t_blocked has no snooze" in capsys.readouterr().err


def test_snoozes_lists_active_and_with_all_the_expired(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = SqliteLedgerStore(farm / "kelpie.db")
    store.put_snooze(Snooze("T-old", ago(days=2), "QA pass", "bob", ago(days=4), "chat"))
    store.put_snooze(Snooze("T-2", NOW + timedelta(days=4), None, None, ago(hours=1)))
    store.put_snooze(Snooze("T-1", NOW + timedelta(days=2), "release\nnotes", "ceo", NOW))
    store.close()
    before = (farm / "kelpie.db").stat().st_mtime_ns
    assert main(["snoozes"]) == 0
    assert capsys.readouterr().out == (
        "T-1 until 2026-10-21 12:00 UTC (release notes; by ceo)\nT-2 until 2026-10-23 12:00 UTC\n"
    )
    assert main(["snoozes", "--all"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == "T-old expired 2026-10-17 12:00 UTC (QA pass; by bob; from chat)"
    assert main(["snoozes", "--json", "--all"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1 and "note" not in data
    assert [(s["task_id"], s["active"]) for s in data["snoozes"]] == [
        ("T-old", False),
        ("T-1", True),
        ("T-2", True),
    ]
    assert data["snoozes"][1] == {
        "task_id": "T-1",
        "until": "2026-10-21T12:00:00+00:00",
        "until_text": "2026-10-21 12:00 UTC",
        "active": True,
        "reason": "release\nnotes",
        "by": "ceo",
        "source": "cli",
        "created_at": NOW.isoformat(),
        "behind_at_snooze": None,
        "grown_at": None,
        "grown_by": None,
    }
    assert (farm / "kelpie.db").stat().st_mtime_ns == before  # read-only


def test_snoozes_when_there_are_none(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["snoozes"]) == 0
    assert capsys.readouterr().out == "no active snoozes\n"
    assert main(["snoozes", "--all"]) == 0
    assert capsys.readouterr().out == "no snoozes\n"
    assert main(["snoozes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["snoozes"] == []


def test_snoozes_on_a_database_that_predates_them(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = sqlite3.connect(farm / "kelpie.db")
    db.execute("PRAGMA user_version = 7")
    db.close()
    assert main(["snoozes"]) == 0
    assert capsys.readouterr().out == PREDATES + "\n"
    assert main(["snoozes", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["snoozes"] == [] and data["note"] == PREDATES
    db = sqlite3.connect(farm / "kelpie.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] == 7  # not upgraded
    db.close()


def test_serve_puts_a_stored_snooze_and_a_marker_into_the_snapshot(farm: Path) -> None:
    write_config(farm, "[snooze]\nmarker = true\n")
    assert main(["snooze", "t_blocked", "--until", "2d", "--by", "ceo"]) == 0
    app = build_app(settings(), clock=lambda: NOW)
    try:
        snap = app.loop.snapshot_source()
    finally:
        app.close()
    assert snap.snoozes["t_blocked"].by == "ceo"
    assert snap.snoozes["t_marked"].source == "marker"
    assert snap.snoozes["t_marked"].until == datetime(2026, 10, 24, tzinfo=UTC)
    write_config(farm)  # markers are off by default
    app = build_app(settings(), clock=lambda: NOW)
    try:
        assert list(app.loop.snapshot_source().snoozes) == ["t_blocked"]
    finally:
        app.close()
