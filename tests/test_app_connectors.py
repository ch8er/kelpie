"""Wiring of the GitHub connector and [[block_conditions]] into the app."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import (
    ConfigError,
    HermesSettings,
    Settings,
    build_app,
    load_rules,
    load_settings,
    merged_snapshot,
    render_status,
)
from kelpie.connectors.github import ConditionSync, GitHubSettings
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig
from kelpie.core.model import Snapshot
from kelpie.metrics import Metrics
from tests.connectors.fake_github import TOKEN

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)

CONFIG = """
[storage]
path = ":memory:"

[traces]
enabled = false

[connectors.github]
repos = ["example/repo"]

[[block_conditions]]
pattern = 'waiting for ci/merge of (?P<pr>\\S+)'
kind = "pr_merged"
repo = "example/repo"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


def test_load_github_and_block_conditions(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    assert settings.github == GitHubSettings(repos=("example/repo",))
    (pattern,) = settings.block_conditions
    assert pattern.kind == "pr_merged" and pattern.repo == "example/repo"


def test_github_options(tmp_path: Path) -> None:
    text = """
[connectors.github]
repos = ["Example/Repo"]
token_env = "KELPIE_GH"
api_url = "https://ghe.example.test/api/v3/"
timeout = "5s"
cache_ttl = "2m"
max_requests_per_tick = 20
tick_budget = "5s"
enabled = false
"""
    github = load_settings(write(tmp_path, text)).github
    assert github == GitHubSettings(
        repos=("example/repo",),
        api_url="https://ghe.example.test/api/v3",
        token_env="KELPIE_GH",
        token_env_explicit=True,
        timeout=timedelta(seconds=5),
        cache_ttl=timedelta(minutes=2),
        max_requests_per_tick=20,
        tick_budget=timedelta(seconds=5),
        enabled=False,
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[connectors]\nother = {}\n", "unknown \\[connectors\\] entries"),
        ("connectors = 1\n", "must be a table"),
        ("[connectors.github]\n", "repos must be"),
        ('[connectors.github]\nrepos = ["x"]\n', "repos must be"),
        ('[connectors.github]\nrepos = ["a/b"]\nnope = 1\n', "unknown"),
        ('[connectors.github]\nrepos = ["a/b"]\ntoken_env = "A-B"\n', "token_env"),
        ('[connectors.github]\nrepos = ["a/b"]\napi_url = "http://gh.example.test"\n', "https"),
        ('[connectors.github]\nrepos = ["a/b"]\ntimeout = "5m"\n', "at most 60s"),
        ('[connectors.github]\nrepos = ["a/b"]\ntimeout = "soon"\n', "timeout"),
        ('[connectors.github]\nrepos = ["a/b"]\ncache_ttl = "10s"\n', "at least 30s"),
        (
            '[connectors.github]\nrepos = ["a/b"]\napi_url = "https://ghe.example.test"\n',
            "token_env must be set",
        ),
        (
            '[connectors.github]\nrepos = ["a/b"]\nmax_requests_per_tick = 0\n',
            "max_requests_per_tick",
        ),
        (
            '[connectors.github]\nrepos = ["a/b"]\nmax_requests_per_tick = true\n',
            "max_requests_per_tick",
        ),
        ('[connectors.github]\nrepos = ["a/b"]\ntick_budget = "2m"\n', "tick_budget"),
        ('[connectors.github]\nrepos = ["a/b"]\nenabled = "yes"\n', "enabled"),
        ('[[block_conditions]]\npattern = "(?P<pr>.+)"\nkind = "pr_merged"\n', "needs"),
    ],
)
def test_bad_connector_config(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_settings(write(tmp_path, text))


def test_token_comes_from_the_named_env_var_only() -> None:
    settings = GitHubSettings(repos=("a/b",), token_env="KELPIE_GH", token_env_explicit=True)
    assert settings.token({"KELPIE_GH": f" {TOKEN} ", "GITHUB_TOKEN": "other"}) == TOKEN
    with pytest.raises(ValueError, match="not set") as err:
        settings.token({"GITHUB_TOKEN": TOKEN})
    assert TOKEN not in str(err.value)
    with pytest.raises(ValueError, match="not allowed") as bad:
        settings.token({"KELPIE_GH": "abc\ndef"})
    assert "abc" not in str(bad.value)
    assert GitHubSettings(repos=("a/b",)).token({}) is None  # default variable: optional


def test_build_app_wires_the_github_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    app = build_app(load_settings(write(tmp_path, CONFIG)))
    try:
        rendered = app.metrics.render()
        assert "kelpie_github_rate_limit_remaining -1" in rendered
        assert 'kelpie_block_conditions{outcome="attached"} 0' in rendered
        status = render_status(app.settings, app.loaded)
        assert "github:   https://api.github.com, token from $GITHUB_TOKEN" in status
        assert TOKEN not in status and TOKEN not in caplog.text
    finally:
        app.close()


def test_build_app_refuses_an_explicit_token_env_that_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_GH", raising=False)
    text = CONFIG.replace(
        'repos = ["example/repo"]', 'repos = ["example/repo"]\ntoken_env = "KELPIE_GH"'
    )
    settings = load_settings(write(tmp_path, text))
    with pytest.raises(ConfigError, match="KELPIE_GH"):
        build_app(settings)
    assert "NOT ready" in render_status(settings, load_rules(settings.engine))


def test_anonymous_and_disabled_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    app = build_app(load_settings(write(tmp_path, CONFIG)))
    app.close()
    assert "read anonymously" in caplog.text
    off = Settings(github=GitHubSettings(repos=("a/b",), enabled=False))
    assert "github:   disabled" in render_status(off, load_rules(off.engine))


def test_a_failing_connector_step_is_counted_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    def broken(snapshot: Snapshot) -> Snapshot:
        raise RuntimeError("boom")

    metrics = Metrics()
    metrics.set_github(True)
    source = merged_snapshot([], metrics, lambda: NOW, enrich=[("github", broken)])
    assert source() == Snapshot(now=NOW)
    assert 'kelpie_connector_errors_total{connector="github"} 1' in metrics.render()
    assert "connector github failed" in caplog.text


def test_metrics_for_github() -> None:
    metrics = Metrics()
    metrics.set_github(True)
    metrics.github_request("ok")
    metrics.github_rate_limit(4999, 5000)
    metrics.block_conditions({"attached": 2, "unresolved": 1})
    rendered = metrics.render()
    assert 'kelpie_github_requests_total{outcome="ok"} 1' in rendered
    assert "kelpie_github_rate_limit_remaining 4999" in rendered
    assert "kelpie_github_rate_limit 5000" in rendered
    assert 'kelpie_block_conditions{outcome="unresolved"} 1' in rendered


def test_condition_sync_is_a_snapshot_step(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, CONFIG))
    assert settings.github is not None
    connector = settings.github.build(None, lambda: NOW)
    step = ConditionSync(connector, settings.block_conditions)
    assert step(Snapshot(now=NOW)) == Snapshot(now=NOW)


CI_CONFIG = CONFIG.replace(
    'repos = ["example/repo"]\n',
    'repos = ["example/repo"]\n\n[connectors.github.ci]\nallow_cancel = true\n'
    'cancel_repos = ["example/repo"]\n',
    1,
)


def test_build_app_wires_the_ci_source_and_cancel_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    text = CI_CONFIG + '\n[engine]\ndry_run = false\n\n[engine.rules.ci_economy]\nmode = "act"\n'
    settings = load_settings(write(tmp_path, text))
    assert settings.github is not None and settings.github.ci is not None
    assert settings.github.ci.allow_cancel
    app = build_app(settings)
    try:
        assert "github" in app.engine.executors
        status = render_status(app.settings, app.loaded)
        assert "github ci: builds of example/repo, cancel_build on example/repo" in status
    finally:
        app.close()


def test_ci_source_read_only_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    text = CONFIG.replace(
        'repos = ["example/repo"]\n', 'repos = ["example/repo"]\n\n[connectors.github.ci]\n', 1
    )
    app = build_app(load_settings(write(tmp_path, text)))
    try:
        action = Action(ActionKind.CANCEL_BUILD, "ci_economy", "r", params={"build_id": "x"})
        assert app.engine.executors["github"].execute(action) is False
        assert "read-only (allow_cancel = false)" in render_status(app.settings, app.loaded)
    finally:
        app.close()


def test_bad_ci_config(tmp_path: Path) -> None:
    text = CONFIG.replace(
        'repos = ["example/repo"]\n',
        'repos = ["example/repo"]\n\n[connectors.github.ci]\nallow_cancel = true\n',
        1,
    )
    with pytest.raises(ConfigError, match="cancel_repos"):
        load_settings(write(tmp_path, text))


def test_ci_economy_may_act_with_only_hermes_when_cancel_is_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    hermes = HermesSettings(db_path=tmp_path / "kanban.db")
    engine = EngineConfig.from_mapping({"dry_run": False, "rules": {"ci_economy": {"mode": "act"}}})
    github = load_settings(write(tmp_path, CI_CONFIG)).github
    assert github is not None and github.ci is not None
    app = build_app(Settings(engine=engine, db_path=None, hermes=hermes, github=github))
    app.close()
    read_only = replace(github, ci=replace(github.ci, allow_cancel=False, cancel_repos=()))
    with pytest.raises(ConfigError, match="Hermes actions are not implemented"):
        build_app(Settings(engine=engine, db_path=None, hermes=hermes, github=read_only))


def test_cancel_token_env_must_be_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.delenv("KELPIE_GH_CANCEL", raising=False)
    text = CI_CONFIG.replace(
        'cancel_repos = ["example/repo"]\n',
        'cancel_repos = ["example/repo"]\ncancel_token_env = "KELPIE_GH_CANCEL"\n',
    )
    settings = load_settings(write(tmp_path, text))
    with pytest.raises(ConfigError, match="KELPIE_GH_CANCEL"):
        build_app(settings)
    monkeypatch.setenv("KELPIE_GH_CANCEL", "ghp_SyntheticCancelToken987654321")
    app = build_app(settings)
    app.close()


def test_rule_window_must_fit_the_connector_window(tmp_path: Path) -> None:
    text = CI_CONFIG + '\n[engine.rules.ci_economy]\nwindow = "48h"\n'
    with pytest.raises(ConfigError, match="window"):
        build_app(load_settings(write(tmp_path, text)))
