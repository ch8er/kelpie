"""Wiring of platform adapters into the app: config, snapshot merging, failures, status."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.park import DEFAULT_WAIT_KEY
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
from kelpie.core.model import Dependency, Owner, OwnerKind, Snapshot, Status, Task
from kelpie.metrics import Metrics
from kelpie.rules.bottlenecks import role_loads
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
        wait_key=(settings.db_path or Path()).parent / "hermes-wait.key",  # next to the ledger
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


def test_hermes_base_refs(tmp_path: Path) -> None:
    assert load_settings(write(tmp_path, "[adapters.hermes]\n")).hermes.base_refs == (  # type: ignore[union-attr]
        "origin/main",
        "main",
    )
    path = write(tmp_path, '[adapters.hermes]\nbase_refs = ["origin/develop"]\n')
    (adapter,) = build_adapters(load_settings(path))
    assert isinstance(adapter, HermesAdapter) and adapter.base_refs == ("origin/develop",)


@pytest.mark.parametrize(
    "value", ['"main"', '["-main"]', '["main", "main"]', '["a..b"]', "[1]", '["main^"]']
)
def test_hermes_base_refs_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ConfigError, match="base_refs"):
        load_settings(write(tmp_path, f"[adapters.hermes]\nbase_refs = {value}\n"))


def test_hermes_values(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        '[adapters.hermes]\ndb_path = "/data/k.db"\nspool = "/data/s.jsonl"\n'
        'lookback = "6h"\nenabled = true\n',
    )
    settings = load_settings(path)
    assert settings.hermes == HermesSettings(
        Path("/data/k.db"),
        Path("/data/s.jsonl"),
        timedelta(hours=6),
        True,
        wait_key=(settings.db_path or Path()).parent / "hermes-wait.key",
    )


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
        "[adapters.hermes]\ncli = ''\n",
        "[adapters.hermes]\ncli = 3\n",
    ],
)
def test_bad_adapter_config(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


def test_hermes_cli_setting(tmp_path: Path) -> None:
    path = write(tmp_path, '[adapters.hermes]\ncli = "/opt/hermes/bin/hermes"\n')
    (adapter,) = build_adapters(load_settings(path))
    assert isinstance(adapter, HermesAdapter) and adapter.cli == "/opt/hermes/bin/hermes"
    (default,) = build_adapters(load_settings(write(tmp_path, "[adapters.hermes]\n")))
    assert isinstance(default, HermesAdapter) and default.cli == "hermes"


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
    with pytest.raises(ConfigError, match="actions the Hermes adapter does not carry out"):
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


def test_act_mode_for_stop_only_rules_is_allowed_with_hermes(tmp_path: Path) -> None:
    hermes = HermesSettings(db_path=tmp_path / "kanban.db")
    only_stop = EngineConfig.from_mapping(
        {"dry_run": False, "rules": {"loop_breaker": {"mode": "act"}}}
    )
    build_app(Settings(engine=only_stop, db_path=None, hermes=hermes)).close()
    mixed = EngineConfig.from_mapping(
        {
            "dry_run": False,
            "rules": {"loop_breaker": {"mode": "act"}, "stale_blocks": {"mode": "act"}},
        }
    )
    with pytest.raises(ConfigError, match=r"\['stale_blocks'\]"):
        build_app(Settings(engine=mixed, db_path=None, hermes=hermes))


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


def test_act_allow_list_matches_what_hermes_carries_out() -> None:
    """Rules allowed to act on Hermes emit only what the adapter carries out (stop, wait,
    wake of a parked task, comment) and alerts."""
    import importlib

    from kelpie.app import HERMES_ACT_RULES

    carried_out = {"ALERT", "ESCALATE", "STOP", "WAIT", "WAKE", "COMMENT"}
    assert {
        "loop_breaker",
        "wait_condition",
        "conflict_warning",
        "dispatch_gate",
    } == HERMES_ACT_RULES
    for name in HERMES_ACT_RULES:
        source = Path(importlib.import_module(f"kelpie.rules.{name}").__file__ or "").read_text()
        kinds = set(re.findall(r"ActionKind\.(\w+)", source))
        assert kinds <= carried_out, (name, kinds - carried_out)


def test_hermes_cli_timeout_setting(tmp_path: Path) -> None:
    (default,) = build_adapters(load_settings(write(tmp_path, "[adapters.hermes]\n")))
    assert isinstance(default, HermesAdapter) and default.cli_timeout == 10.0
    path = write(tmp_path, '[adapters.hermes]\ncli_timeout = "25s"\n')
    (adapter,) = build_adapters(load_settings(path))
    assert isinstance(adapter, HermesAdapter) and adapter.cli_timeout == 25.0
    for bad in ("0", "'2m'", "'soon'", "false"):
        with pytest.raises(ConfigError, match="cli_timeout"):
            load_settings(write(tmp_path, f"[adapters.hermes]\ncli_timeout = {bad}\n"))


def test_hermes_roles_table(tmp_path: Path) -> None:
    path = write(
        tmp_path, '[adapters.hermes.roles]\nreviewer-bot = "reviewer"\nmerger = "reviewer"\n'
    )
    h = load_settings(path).hermes
    assert h is not None and h.roles == (("merger", "reviewer"), ("reviewer-bot", "reviewer"))
    (adapter,) = build_adapters(load_settings(path))
    assert isinstance(adapter, HermesAdapter) and adapter.role_of("merger") == "reviewer"


@pytest.mark.parametrize(
    "text",
    [
        '[adapters.hermes]\nroles = ["dev"]\n',
        '[adapters.hermes.roles]\ndev-1 = ""\n',
        "[adapters.hermes.roles]\ndev-1 = 3\n",
        '[adapters.hermes.roles]\nDev = "a"\ndev = "b"\n',
    ],
)
def test_hermes_roles_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="roles"):
        load_settings(write(tmp_path, text))


def test_hermes_role_from_name_setting(tmp_path: Path) -> None:
    path = write(tmp_path, "[adapters.hermes]\nrole_from_name = false\n")
    (adapter,) = build_adapters(load_settings(path))
    assert isinstance(adapter, HermesAdapter) and adapter.role_of("dev-1") == "dev-1"
    with pytest.raises(ConfigError, match="role_from_name"):
        load_settings(write(tmp_path, '[adapters.hermes]\nrole_from_name = "no"\n'))


def test_wait_key_defaults_next_to_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    text = f'[storage]\npath = "{tmp_path / "data" / "kelpie.db"}"\n[adapters.hermes]\n'
    settings = load_settings(write(tmp_path, text))
    assert settings.hermes is not None
    assert settings.hermes.wait_key == tmp_path / "data" / "hermes-wait.key"
    memory = load_settings(write(tmp_path, '[storage]\npath = ":memory:"\n[adapters.hermes]\n'))
    assert memory.hermes is not None and memory.hermes.wait_key == DEFAULT_WAIT_KEY
    explicit = load_settings(write(tmp_path, '[adapters.hermes]\nwait_key = "/k/key"\n'))
    assert explicit.hermes is not None and explicit.hermes.wait_key == Path("/k/key")
    with pytest.raises(ConfigError, match="wait_key"):
        load_settings(write(tmp_path, '[adapters.hermes]\nwait_key = ""\n'))


def test_prework_statuses_setting(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, "[adapters.hermes]\n"))
    assert settings.hermes is not None and settings.hermes.prework_statuses == (
        "triage",
        "scheduled",
    )
    custom = load_settings(write(tmp_path, '[adapters.hermes]\nprework_statuses = ["triage"]\n'))
    assert custom.hermes is not None and custom.hermes.prework_statuses == ("triage",)
    with pytest.raises(ConfigError, match="prework_statuses"):
        load_settings(write(tmp_path, '[adapters.hermes]\nprework_statuses = ["running"]\n'))


@pytest.mark.parametrize("human_first", [True, False])
def test_a_person_stays_a_person_whatever_another_adapter_says(human_first: bool) -> None:
    """Claude Code names task-list owners without a kind (AGENT by default); a human
    assignee Hermes knows must not become idle agent capacity."""
    person = Owner("alice", "owner", OwnerKind.HUMAN)
    unknown = Owner("alice", "alice")  # same id, no kind known
    dev = Owner("dev-1", "developer")
    a = SnapAdapter(name="hermes", owners=(person, dev) if human_first else (unknown, dev))
    b = SnapAdapter(name="claude-code", owners=(unknown,) if human_first else (person,))
    snap = merged_snapshot([a, b], Metrics(), clock=lambda: NOW)()
    by_id = {o.id: o for o in snap.owners}
    assert by_id["alice"] == person and by_id["dev-1"].kind is OwnerKind.AGENT  # role kept
    loads = role_loads(snap)
    assert all("alice" not in load.agents for load in loads.values())


def test_a_person_is_a_person_in_any_case() -> None:
    """``human_owners`` ignores case; so does the merge: ``Alice`` in a task list is the
    person Hermes calls ``alice``."""
    hermes = SnapAdapter(name="hermes", owners=(Owner("alice", "owner", OwnerKind.HUMAN),))
    cc = SnapAdapter(name="claude-code", owners=(Owner("Alice", "Alice"), Owner("bot", "bot")))
    snap = merged_snapshot([hermes, cc], Metrics(), clock=lambda: NOW)()
    kinds = {o.id: o.kind for o in snap.owners}
    assert kinds == {"alice": OwnerKind.HUMAN, "Alice": OwnerKind.HUMAN, "bot": OwnerKind.AGENT}
    assert all("Alice" not in load.agents for load in role_loads(snap).values())
