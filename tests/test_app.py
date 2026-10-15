import sqlite3
import threading
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import (
    ConfigError,
    ControlLoop,
    Settings,
    build_app,
    dispatch_gate_warnings,
    find_config,
    load_rules,
    load_settings,
    render_status,
    serialize_warnings,
)
from kelpie.cli import main
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot
from kelpie.metrics import Metrics
from kelpie.rules import PlannedRule, Priority
from kelpie.rules.conflict_warning import ConflictWarning
from kelpie.server import make_server, run

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
PLANNED = (
    PlannedRule("alpha", 1, Priority.P0, "implemented"),
    PlannedRule("beta", 2, Priority.P0, "not implemented"),
)


@dataclass
class AlphaRule:
    threshold: int = 3
    name: str = "alpha"

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        return [Action(ActionKind.ALERT, "alpha", f"threshold {self.threshold}", key="a")]


def alpha_factory(config: dict[str, Any]) -> AlphaRule:
    return AlphaRule(**config)


REGISTRY = {"alpha": alpha_factory}


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


# --- rule loading -----------------------------------------------------------------------


def test_loads_registered_rules_with_their_config_and_mode() -> None:
    config = EngineConfig.from_mapping({"rules": {"alpha": {"mode": "act", "threshold": 7}}})
    loaded = load_rules(config, REGISTRY, PLANNED)
    [rule] = loaded.rules
    assert isinstance(rule, AlphaRule) and rule.threshold == 7
    assert [(s.name, s.implemented, s.mode) for s in loaded.statuses] == [
        ("alpha", True, Mode.ACT),
        ("beta", False, Mode.OBSERVE),
    ]
    assert loaded.warnings == []


def test_unknown_rule_fails_at_startup() -> None:
    config = EngineConfig.from_mapping({"rules": {"gamma": {"mode": "act"}}})
    with pytest.raises(ConfigError, match="unknown rules in config: \\['gamma'\\]"):
        load_rules(config, REGISTRY, PLANNED)


def test_planned_but_unimplemented_rule_is_skipped_with_warning() -> None:
    config = EngineConfig.from_mapping({"rules": {"beta": {"mode": "act"}}})
    loaded = load_rules(config, REGISTRY, PLANNED)
    assert [type(r) for r in loaded.rules] == [AlphaRule]
    assert loaded.warnings == [
        "rule 'beta' is planned but not implemented yet; its config is ignored"
    ]


def test_registered_rule_missing_from_plan_fails() -> None:
    with pytest.raises(ConfigError, match="not in PLANNED"):
        load_rules(EngineConfig(), {**REGISTRY, "rogue": alpha_factory}, PLANNED)


def test_bad_rule_parameters_fail() -> None:
    config = EngineConfig.from_mapping({"rules": {"alpha": {"no_such_param": 1}}})
    with pytest.raises(ConfigError, match="bad config for rule 'alpha'"):
        load_rules(config, REGISTRY, PLANNED)


def test_real_registry_and_plan_load_with_defaults() -> None:
    loaded = load_rules(EngineConfig())
    assert all(s.mode is Mode.OBSERVE for s in loaded.statuses)


# --- settings ---------------------------------------------------------------------------


def test_defaults_are_dry_run_and_observe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    settings = load_settings(None)
    assert settings.engine.dry_run
    assert settings.engine.default_mode is Mode.OBSERVE
    assert settings.db_path == Path("kelpie.db")


def test_storage_path_from_config_and_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    path = write(tmp_path, '[storage]\npath = "/data/k.db"\n[engine]\ndry_run = false\n')
    settings = load_settings(path)
    assert settings.db_path == Path("/data/k.db") and not settings.engine.dry_run
    monkeypatch.setenv("KELPIE_DB", ":memory:")
    assert load_settings(path).db_path is None


@pytest.mark.parametrize(
    "text",
    [
        "[engin]\n",
        "[engine]\ndry_run = 'no'\n",
        "not toml [",
        'storage = "x"\n',
        "engine = 5\n",
        "[storage]\npath = 5\n",
        '[storage]\npaht = "y.db"\n',
        '[storage]\npath = ""\n',
        "[engine]\ninterval = [1]\n",
    ],
    ids=[
        "section",
        "value",
        "syntax",
        "storage-not-table",
        "engine-not-table",
        "path-type",
        "storage-typo",
        "empty-path",
        "interval-type",
    ],
)
def test_bad_config_is_a_config_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(write(tmp_path, text))


def test_missing_config_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(tmp_path / "missing.toml")


def test_find_config_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    assert find_config(None) is None
    write(tmp_path, "")
    assert find_config(None) == Path("kelpie.toml")
    monkeypatch.setenv("KELPIE_CONFIG", "/etc/kelpie.toml")
    assert find_config(None) == Path("/etc/kelpie.toml")
    assert find_config("x.toml") == Path("x.toml")


# --- control loop and metrics ----------------------------------------------------------


def loop_for(rules: list[Any], config: EngineConfig | None = None) -> ControlLoop:
    engine = Engine(rules, config=config or EngineConfig())
    return ControlLoop(engine, lambda: Snapshot(now=NOW), Metrics())


def test_tick_feeds_metrics() -> None:
    loop = loop_for([AlphaRule()])
    loop.tick_once()
    loop.tick_once()
    text = loop.metrics.render()
    assert "kelpie_engine_ticks_total 2" in text
    assert (
        'kelpie_actions_total{rule="alpha",kind="alert",mode="observe",outcome="dry_run"} 1' in text
    )
    assert (
        'kelpie_actions_total{rule="alpha",kind="alert",mode="observe",outcome="suppressed"} 1'
        in text
    )
    assert "kelpie_ledger_up 1" in text


class Exploding:
    name = "boom"

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        raise RuntimeError("bug")


def test_rule_errors_and_tick_failures_are_counted() -> None:
    loop = loop_for([Exploding()])
    loop.tick_once()
    assert 'kelpie_rule_errors_total{rule="boom"} 1' in loop.metrics.render()
    loop.snapshot_source = lambda: Snapshot(now=datetime(2026, 3, 2))  # naive: tick raises
    loop.tick_once()
    assert "kelpie_engine_tick_failures_total 1" in loop.metrics.render()


def test_loop_runs_in_background_and_stops() -> None:
    loop = loop_for([AlphaRule()], EngineConfig(interval=EngineConfig().interval / 3000))
    loop.start()
    with pytest.raises(RuntimeError, match="already started"):
        loop.start()
    try:
        for _ in range(200):
            if loop.metrics.ticks >= 2:
                break
            threading.Event().wait(0.01)
    finally:
        loop.stop()
    assert loop.metrics.ticks >= 2
    assert not loop.running


def test_metrics_escape_label_values() -> None:
    metrics = Metrics()
    metrics.rule_errors['we"ird\\rule\n'] = 1
    assert 'kelpie_rule_errors_total{rule="we\\"ird\\\\rule\\n"} 1' in metrics.render()


def test_build_app_serves_engine_metrics(tmp_path: Path) -> None:
    app = build_app(Settings(db_path=tmp_path / "kelpie.db"), clock=lambda: NOW)
    app.loop.tick_once()
    server = make_server("127.0.0.1", 0, app.metrics)
    thread = threading.Thread(target=run, args=(app, server), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with urllib.request.urlopen(f"{base}/metrics") as resp:
        body = resp.read().decode()
    with urllib.request.urlopen(f"{base}/healthz") as resp:
        assert resp.read() == b"ok\n"
    assert app.loop.running
    server.shutdown()
    thread.join(5)
    assert not thread.is_alive() and not app.loop.running
    with pytest.raises(sqlite3.ProgrammingError):  # the ledger was closed on shutdown
        app.store.since(NOW)
    assert "kelpie_engine_ticks_total" in body
    assert "kelpie_dry_run 1" in body
    assert 'kelpie_rule_loaded{rule="compaction_gate",mode=""} 0' in body
    assert_valid_exposition(body)


def assert_valid_exposition(text: str) -> None:
    """Every sample belongs to a family declared once with HELP and TYPE."""
    declared: list[str] = []
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            declared.append(line.split()[2])
        elif line and not line.startswith("#"):
            name = line.split("{")[0].split()[0]
            assert any(name == d or name.startswith(d + "_") for d in declared[-1:]), line
    assert len(declared) == len(set(declared))
    assert text.count("# HELP ") == len(declared)


# --- status and CLI ---------------------------------------------------------------------


def test_render_status_lists_rules_with_modes() -> None:
    config = EngineConfig.from_mapping({"rules": {"alpha": {"mode": "act"}, "beta": {}}})
    text = render_status(
        Settings(engine=config, db_path=None), load_rules(config, REGISTRY, PLANNED)
    )
    assert "dry-run:  on (nothing leaves Kelpie)" in text
    assert "  1  alpha             P0    loaded           act (dry-run)" in text
    assert "  2  beta              P0    not implemented  -" in text
    assert "warning: rule 'beta' is planned but not implemented yet" in text


def test_cli_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, '[engine.rules.stale_blocks]\nmode = "suggest"\n')
    assert main(["--config", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "stale_blocks" in out and "loop_breaker" in out


def test_cli_rejects_unknown_rule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, '[engine.rules.nope]\nmode = "act"\n')
    assert main(["--config", str(path), "serve"]) == 2
    assert "unknown rules in config: ['nope']" in capsys.readouterr().err


def test_unopenable_ledger_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot open ledger"):
        build_app(Settings(db_path=tmp_path / "missing-dir" / "kelpie.db"))


def test_rules_load_in_plan_order() -> None:
    registry = {"beta": alpha_factory, "alpha": alpha_factory}
    loaded = load_rules(EngineConfig(), registry, PLANNED)
    assert [s.name for s in loaded.statuses if s.implemented] == ["alpha", "beta"]
    assert len(loaded.rules) == 2


def test_factory_crash_is_a_config_error() -> None:
    def broken(config: dict[str, Any]) -> AlphaRule:
        raise KeyError("x")

    with pytest.raises(ConfigError, match="bad config for rule 'alpha'"):
        load_rules(EngineConfig(), {"alpha": broken}, PLANNED)


def test_rule_errors_start_at_zero_and_ledger_down_is_reported() -> None:
    from kelpie.core.ledger import MemoryLedgerStore

    class WriteFails(MemoryLedgerStore):
        def append(self, entry: Any) -> None:
            raise sqlite3.OperationalError("disk full")

    metrics = Metrics()
    metrics.set_rules(load_rules(EngineConfig(), REGISTRY, PLANNED).statuses, dry_run=True)
    assert 'kelpie_rule_errors_total{rule="alpha"} 0' in metrics.render()
    engine = Engine([AlphaRule()], config=EngineConfig(), ledger_store=WriteFails())
    loop = ControlLoop(engine, lambda: Snapshot(now=NOW), metrics)
    loop.tick_once()
    assert "kelpie_ledger_up 0" in metrics.render()


def test_loop_breaker_inherits_poll_patterns_from_wait_condition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    patterns = {"ci_status": ["gh pr checks"]}
    rules = {"wait_condition": {"poll_patterns": patterns}, "loop_breaker": {}}
    loaded = load_rules(EngineConfig(rules=rules))
    breaker = next(r for r in loaded.rules if r.name == "loop_breaker")
    assert breaker.poll_patterns == {"ci_status": ("gh pr checks",)}  # type: ignore[attr-defined]
    assert not caplog.records

    # Its own patterns win; a difference is logged.
    own = {
        "wait_condition": {"poll_patterns": patterns},
        "loop_breaker": {"poll_patterns": {"x": ["y"]}},
    }
    with caplog.at_level("WARNING"):
        loaded = load_rules(EngineConfig(rules=own))
    breaker = next(r for r in loaded.rules if r.name == "loop_breaker")
    assert breaker.poll_patterns == {"x": ("y",)}  # type: ignore[attr-defined]
    assert "differs from wait_condition" in caplog.text

    # The same patterns on both sides are fine, and nothing is invented without rule 2's.
    caplog.clear()
    same = {
        "wait_condition": {"poll_patterns": patterns},
        "loop_breaker": {"poll_patterns": patterns},
    }
    load_rules(EngineConfig(rules=same))
    assert not caplog.records
    bare = next(r for r in load_rules(EngineConfig()).rules if r.name == "loop_breaker")
    assert bare.poll_patterns == {}  # type: ignore[attr-defined]


def test_serialize_in_act_mode_needs_rule_2_in_act_mode() -> None:
    def engine(**modes: Mode) -> EngineConfig:
        return EngineConfig(dry_run=False, modes=modes)

    act, on = Mode.ACT, [ConflictWarning(serialize=True)]
    assert serialize_warnings(engine(conflict_warning=act), on)
    assert serialize_warnings(engine(conflict_warning=act, wait_condition=act), on) == []
    assert serialize_warnings(engine(conflict_warning=Mode.SUGGEST), on) == []
    assert serialize_warnings(engine(conflict_warning=act), [ConflictWarning()]) == []
    dry = EngineConfig(dry_run=True, modes={"conflict_warning": act})
    assert serialize_warnings(dry, on) == []


def test_dispatch_gate_act_mode_needs_rule_2_in_act_mode() -> None:
    act = Mode.ACT
    assert dispatch_gate_warnings(EngineConfig(dry_run=False, modes={"dispatch_gate": act}))
    both = {"dispatch_gate": act, "wait_condition": act}
    assert dispatch_gate_warnings(EngineConfig(dry_run=False, modes=both)) == []
    assert dispatch_gate_warnings(EngineConfig(dry_run=True, modes={"dispatch_gate": act})) == []
    assert dispatch_gate_warnings(EngineConfig(dry_run=False)) == []
