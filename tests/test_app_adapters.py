"""Wiring of platform adapters into the app: config, snapshot merging, failures, status."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.app import (
    ConfigError,
    HermesSettings,
    Settings,
    build_adapters,
    build_app,
    load_rules,
    load_settings,
    merged_snapshot,
    render_status,
)
from kelpie.cli import main
from kelpie.core.config import EngineConfig
from kelpie.core.model import Dependency, Owner, Snapshot, Status, Task
from kelpie.metrics import Metrics
from tests.adapters.kanban import Board
from tests.fakes import FakeAdapter

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


# --- config -----------------------------------------------------------------------------


def test_no_adapters_section() -> None:
    assert load_settings(None).hermes is None
    assert build_adapters(load_settings(None)) == []


def test_hermes_defaults(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, "[adapters.hermes]\n"))
    assert settings.hermes == HermesSettings(
        db_path=Path("~/.hermes/kanban.db"),
        spool=Path("~/.hermes/kelpie/spool.jsonl"),
        lookback=timedelta(hours=24),
        enabled=True,
        git_state=True,
        git_timeout=timedelta(seconds=5),
    )
    (adapter,) = build_adapters(settings)
    assert isinstance(adapter, HermesAdapter)
    assert adapter.db_path == Path.home() / ".hermes" / "kanban.db"  # ~ expanded
    assert adapter.git_state is True and adapter.git_timeout == timedelta(seconds=5)


def test_hermes_git_settings(tmp_path: Path) -> None:
    path = write(tmp_path, '[adapters.hermes]\ngit_state = false\ngit_timeout = "1m30s"\n')
    h = load_settings(path).hermes
    assert h is not None and h.git_state is False and h.git_timeout == timedelta(seconds=90)
    (adapter,) = build_adapters(load_settings(path))
    assert isinstance(adapter, HermesAdapter)
    assert adapter.git_state is False and adapter.git_timeout == timedelta(seconds=90)


def test_hermes_values(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[adapters.hermes]\ndb_path = "/data/k.db"\nspool = "/data/s.jsonl"\n'
        'lookback = "6h"\nenabled = true\n',
    )
    h = load_settings(path).hermes
    assert h == HermesSettings(Path("/data/k.db"), Path("/data/s.jsonl"), timedelta(hours=6), True)


def test_hermes_disabled_builds_no_adapter(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, "[adapters.hermes]\nenabled = false\n"))
    assert settings.hermes is not None and build_adapters(settings) == []


def test_spool_false_disables_spool_reading(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, "[adapters.hermes]\nspool = false\n"))
    assert settings.hermes is not None and settings.hermes.spool is None
    (adapter,) = build_adapters(settings)
    assert isinstance(adapter, HermesAdapter) and adapter.spool is None


@pytest.mark.parametrize(
    "text",
    [
        "[adapters.hermes]\nspool = true\n",
        "adapters = 5\n",
        "[adapters.other]\n",
        "[adapters]\nhermes = 'x'\n",
        "[adapters.hermes]\ndb = 'x'\n",
        "[adapters.hermes]\ndb_path = 5\n",
        "[adapters.hermes]\ndb_path = ''\n",
        "[adapters.hermes]\nspool = ['x']\n",
        "[adapters.hermes]\nlookback = 'soon'\n",
        "[adapters.hermes]\nlookback = [1]\n",
        "[adapters.hermes]\nlookback = 0\n",
        "[adapters.hermes]\nenabled = 'yes'\n",
        "[adapters.hermes]\ngit_state = 'yes'\n",
        "[adapters.hermes]\ngit_state = 1\n",
        "[adapters.hermes]\ngit_timeout = 'soon'\n",
        "[adapters.hermes]\ngit_timeout = 0\n",
        "[adapters.hermes]\ngit_timeout = [1]\n",
        "[adapters.hermes]\ngit_timeout = false\n",
    ],
)
def test_bad_adapter_config(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


# --- snapshot merging -------------------------------------------------------------------


def task(task_id: str, source: str) -> Task:
    return Task(id=task_id, title=task_id, status=Status.READY, status_since=NOW, source=source)


@dataclass
class SnapAdapter(FakeAdapter):
    tasks: tuple[Task, ...] = ()
    owners: tuple[Owner, ...] = ()
    seen: list[datetime] | None = None

    def snapshot(self, now: datetime) -> Snapshot:
        if self.seen is not None:
            self.seen.append(now)
        return Snapshot(
            now=now,
            tasks=self.tasks,
            owners=self.owners,
            dependencies=tuple(Dependency(t.id, "root") for t in self.tasks),
        )


class Broken(FakeAdapter):
    def snapshot(self, now: datetime) -> Snapshot:
        raise RuntimeError("db is gone")


def test_snapshots_are_merged_with_the_app_clock() -> None:
    seen: list[datetime] = []
    dev = Owner("dev", "dev")
    a = SnapAdapter(name="a", tasks=(task("a1", "a"),), owners=(dev,), seen=seen)
    b = SnapAdapter(name="b", tasks=(task("b1", "b"), task("b2", "b")), owners=(dev,))
    plain = FakeAdapter(name="plain")  # no snapshot capability: ignored
    source = merged_snapshot([a, plain, b], Metrics(), clock=lambda: NOW)
    snap = source()
    assert snap.now == NOW and seen == [NOW]
    assert [t.id for t in snap.tasks] == ["a1", "b1", "b2"]
    assert snap.owners == (dev,)  # de-duplicated
    assert len(snap.dependencies) == 3


def test_failing_adapter_is_counted_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    metrics = Metrics()
    metrics.set_adapters(["broken", "ok"])
    ok = SnapAdapter(name="ok", tasks=(task("t1", "ok"),))
    source = merged_snapshot([Broken(name="broken"), ok], metrics, clock=lambda: NOW)
    assert [t.id for t in source().tasks] == ["t1"]
    source()
    body = metrics.render()
    assert 'kelpie_adapter_errors_total{adapter="broken"} 2' in body
    assert 'kelpie_adapter_errors_total{adapter="ok"} 0' in body
    assert "db is gone" in caplog.text


def test_no_adapters_gives_an_empty_snapshot() -> None:
    assert merged_snapshot([], Metrics(), clock=lambda: NOW)() == Snapshot(now=NOW)


# --- build_app --------------------------------------------------------------------------


def test_build_app_wires_hermes_and_survives_a_missing_db(tmp_path: Path) -> None:
    settings = Settings(
        db_path=None,
        hermes=HermesSettings(db_path=tmp_path / "absent.db", spool=tmp_path / "s.jsonl"),
    )
    app = build_app(settings, clock=lambda: NOW)
    assert [a.name for a in app.engine.adapters] == ["hermes"]
    app.loop.tick_once()  # does not raise
    body = app.metrics.render()
    assert 'kelpie_adapter_errors_total{adapter="hermes"} 1' in body
    assert "kelpie_engine_tick_failures_total 0" in body
    app.close()


def test_build_app_runs_rules_over_the_hermes_board(tmp_path: Path) -> None:
    board = Board(tmp_path / "kanban.db")
    board.task("t_1", "running", NOW - timedelta(hours=3), assignee="dev")
    board.close()
    settings = Settings(db_path=None, hermes=HermesSettings(db_path=tmp_path / "kanban.db"))
    app = build_app(settings, clock=lambda: NOW)
    snap = app.loop.snapshot_source()
    assert [t.id for t in snap.tasks] == ["t_1"]
    app.loop.tick_once()
    assert 'kelpie_adapter_errors_total{adapter="hermes"} 0' in app.metrics.render()
    app.close()


# --- status -----------------------------------------------------------------------------


def test_render_status_lists_adapters(tmp_path: Path) -> None:
    Board(tmp_path / "kanban.db").close()
    settings = Settings(
        db_path=None,
        hermes=HermesSettings(db_path=tmp_path / "kanban.db", spool=tmp_path / "s.jsonl"),
    )
    text = render_status(settings, load_rules(settings.engine), build_adapters(settings))
    assert f"hermes ({tmp_path / 'kanban.db'}): readable" in text
    gone = Settings(db_path=None, hermes=HermesSettings(db_path=tmp_path / "gone.db"))
    text = render_status(gone, load_rules(gone.engine), build_adapters(gone))
    assert "hermes" in text and "NOT readable" in text and "not found" in text
    assert "(none configured)" in render_status(Settings(db_path=None), load_rules(gone.engine))


def test_cli_status_shows_adapters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, f'[adapters.hermes]\ndb_path = "{tmp_path / "nope.db"}"\n')
    assert main(["--config", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "adapters:" in out and "NOT readable" in out


def test_act_mode_with_only_hermes_is_refused(tmp_path: Path) -> None:
    config = EngineConfig.from_mapping(
        {"dry_run": False, "rules": {"exit_contract": {"mode": "act"}}}
    )
    hermes = HermesSettings(db_path=tmp_path / "kanban.db")
    with pytest.raises(ConfigError, match="Hermes actions are not implemented"):
        build_app(Settings(engine=config, db_path=None, hermes=hermes))
    # Allowed: dry-run on, observe/suggest modes, or no Hermes at all.
    for ok in (
        EngineConfig.from_mapping({"dry_run": True, "rules": {"exit_contract": {"mode": "act"}}}),
        EngineConfig.from_mapping(
            {"dry_run": False, "rules": {"exit_contract": {"mode": "suggest"}}}
        ),
    ):
        build_app(Settings(engine=ok, db_path=None, hermes=hermes)).close()
    build_app(Settings(engine=config, db_path=None)).close()


def test_status_shows_spool_age(tmp_path: Path) -> None:
    Board(tmp_path / "kanban.db").close()
    spool = tmp_path / "spool.jsonl"
    hermes = HermesSettings(db_path=tmp_path / "kanban.db", spool=spool)
    settings = Settings(db_path=None, hermes=hermes)
    text = render_status(settings, load_rules(settings.engine), build_adapters(settings))
    assert "spool: missing" in text
    spool.write_text(json.dumps({"type": "tool", "at": time.time() - 120}) + "\n")
    text = render_status(settings, load_rules(settings.engine), build_adapters(settings))
    assert "spool: last line 2m ago" in text
