"""``[adapters.github_issues]``: parsing, defaults from ``[connectors.github]``, wiring."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.issues import IssuesAdapter
from kelpie.adapters.issues.config import (
    DEFAULT_LOOKBACK,
    NAME,
    GitHubIssuesSettings,
    load_github_issues,
)
from kelpie.adapters.issues.mapping import Agent, Board
from kelpie.app import ConfigError, build_adapters, load_settings, render_adapters
from kelpie.connectors.github.settings import load_github

GITHUB = load_github(
    {
        "repos": ["Acme/App", "acme/lib"],
        "api_url": "http://127.0.0.1:9",
        "token_env": "GH_READ",
        "timeout": "5s",
        "cache_ttl": "45s",
    }
)


def test_defaults_come_from_the_github_connector() -> None:
    settings = load_github_issues({}, GITHUB)
    assert [b.key for b in settings.boards] == ["acme/app", "acme/lib"]
    assert settings.api_url == "http://127.0.0.1:9"
    assert (settings.token_env, settings.token_env_explicit) == ("GH_READ", True)
    assert (settings.timeout, settings.cache_ttl) == (timedelta(seconds=5), timedelta(seconds=45))
    assert settings.lookback == DEFAULT_LOOKBACK and settings.enabled
    assert settings.boards[0] == Board("acme/app")
    assert settings.agents == {}


def test_without_the_connector() -> None:
    settings = load_github_issues({"repos": ["acme/app"]}, None)
    assert settings.api_url == "https://api.github.com"
    assert (settings.token_env, settings.token_env_explicit) == ("GITHUB_TOKEN", False)
    with pytest.raises(ValueError, match="repos is required"):
        load_github_issues({}, None)
    with pytest.raises(ValueError, match="token_env is required"):
        load_github_issues({"repos": ["a/b"], "api_url": "http://127.0.0.1:9"}, None)


def test_agents_and_boards() -> None:
    settings = load_github_issues(
        {
            "label": "agent",
            "blocked_labels": ["blocked", "needs info"],
            "ready_labels": ["agent-ready"],
            "role": "dev",
            "agents": {
                "copilot": {"assignee": "@Copilot", "author": "copilot-swe-agent[bot]"},
                "openhands": {"label": "fix-me", "role": "fixer"},
                "codex": {"mention": "@codex please", "author": "chatgpt-codex-connector[bot]"},
            },
            "boards": {
                "Acme/Lib": {"label": False, "agents": ["openhands"], "blocked_labels": []},
            },
        },
        GITHUB,
    )
    app, lib = settings.boards
    assert app == Board(
        "acme/app",
        label="agent",
        blocked_labels=("blocked", "needs info"),
        ready_labels=("agent-ready",),
        role="dev",
    )
    assert lib == Board(
        "acme/lib",
        label=None,
        blocked_labels=(),
        ready_labels=("agent-ready",),
        role="dev",
        agents=("openhands",),
    )
    assert settings.agents["copilot"] == Agent(
        "copilot", assignee="Copilot", author="copilot-swe-agent[bot]"
    )
    assert settings.agents["openhands"].role == "fixer"
    assert settings.agents["codex"].mention == "@codex please"


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ([], "must be a table"),
        ({"nope": 1}, "unknown"),
        ({"enabled": "yes"}, "enabled"),
        ({"repos": []}, "non-empty list"),
        ({"repos": ["no-slash"]}, "is not owner/name"),
        ({"api_url": "ftp://x"}, "api_url"),
        ({"token_env": "1BAD"}, "token_env"),
        ({"timeout": "2m"}, "timeout"),
        ({"cache_ttl": "1s"}, "cache_ttl"),
        ({"lookback": "-1h"}, "lookback"),
        ({"max_requests_per_tick": 0}, "max_requests_per_tick"),
        ({"max_requests_per_tick": True}, "max_requests_per_tick"),
        ({"label": ""}, "label"),
        ({"blocked_labels": "blocked"}, "blocked_labels"),
        ({"ready_labels": ["x" * 51]}, "ready_labels"),
        ({"agents": []}, "agents must be a table"),
        ({"agents": {"bad name": {"label": "x"}}}, "agent name"),
        ({"agents": {"a": "x"}}, "must be a table"),
        ({"agents": {"a": {"label": "x", "colour": 1}}}, "unknown"),
        ({"agents": {"a": {"mention": "@a"}}}, "needs assignee, label or author"),
        ({"agents": {"a": {"assignee": "not a login!"}}}, "GitHub login"),
        ({"boards": []}, "boards must be a table"),
        ({"boards": {"other/repo": {}}}, "is not in repos"),
        ({"boards": {"acme/app": "x"}}, "must be a table"),
        ({"boards": {"acme/app": {"colour": 1}}}, "unknown"),
        ({"boards": {"acme/app": {"agents": ["ghost"]}}}, "unknown agents"),
        ({"boards": {"acme/app": {"agents": "all"}}}, "list of agent names"),
        ({"label": "a,b"}, "comma"),
        (
            {"agents": {"a": {"assignee": "Copilot"}, "b": {"assignee": "copilot[bot]"}}},
            "already agents.a's",
        ),
        ({"agents": {"a": {"label": "fix-me"}, "b": {"label": "Fix-Me"}}}, "already agents.a's"),
        ({"label": "agent", "agents": {"a": {"label": "agent"}}}, "also a board label"),
        ({"agents": {"a": {"label": "blocked"}}}, "also a board label"),
        ({"ready_labels": ["go"], "agents": {"a": {"label": "go"}}}, "also a board label"),
    ],
)
def test_bad_sections(section: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_github_issues(section, GITHUB)


def test_token() -> None:
    settings = load_github_issues({}, GITHUB)
    assert settings.token({"GH_READ": " t0ken "}) == "t0ken"
    with pytest.raises(ValueError, match=r"\$GH_READ is not set"):
        settings.token({})
    with pytest.raises(ValueError, match="not allowed") as err:
        settings.token({"GH_READ": "bad\ntoken-SECRET"})
    assert "SECRET" not in str(err.value)
    loose = GitHubIssuesSettings(boards=(Board("a/b"),))
    assert loose.token({}) is None


def test_build(caplog: pytest.LogCaptureFixture) -> None:
    adapter = GitHubIssuesSettings(boards=(Board("a/b"),)).build({})
    assert isinstance(adapter, IssuesAdapter) and adapter.name == NAME
    assert "read anonymously" in caplog.text
    assert adapter.check() is None
    assert adapter.status_detail(None)[-1] == "issues: not read yet"  # type: ignore[arg-type]


def test_wired_into_kelpie(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_READ", "t0ken")
    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[connectors.github]\nrepos = ["acme/app"]\napi_url = "http://127.0.0.1:9"\n'
        'token_env = "GH_READ"\n'
        "[adapters.github_issues]\n"
        '[adapters.github_issues.agents.copilot]\nassignee = "Copilot"\n'
    )
    settings = load_settings(config)
    assert settings.github_issues is not None
    (adapter,) = build_adapters(settings)
    assert adapter.name == "github-issues"
    lines = render_adapters([adapter])
    assert lines[2] == "  github-issues: readable"
    assert "    agents: copilot" in lines
    monkeypatch.delenv("GH_READ")
    with pytest.raises(ConfigError, match="GH_READ"):
        build_adapters(settings)
    text = config.read_text().replace(
        "[adapters.github_issues]\n", "[adapters.github_issues]\nenabled = false\n"
    )
    config.write_text(text)
    assert build_adapters(load_settings(config)) == []
    config.write_text('[adapters.github_issues]\nrepos = "acme/app"\n')
    with pytest.raises(ConfigError, match="repos"):
        load_settings(config)


def test_an_inherited_token_never_goes_to_another_host() -> None:
    with pytest.raises(ValueError, match="token_env is required"):
        load_github_issues({"api_url": "https://ghe.example.com/api/v3"}, GITHUB)
    plain = load_github({"repos": ["acme/app"]})
    with pytest.raises(ValueError, match="token_env is required"):
        load_github_issues({"api_url": "https://ghe.example.com/api/v3"}, plain)
    ok = load_github_issues(
        {"api_url": "https://ghe.example.com/api/v3", "token_env": "GHE_TOKEN"}, plain
    )
    assert (ok.token_env, ok.token_env_explicit) == ("GHE_TOKEN", True)
    with pytest.raises(ValueError, match="token_env is required"):  # GHE token to github.com
        load_github_issues({"api_url": "https://api.github.com"}, GITHUB)
    same = load_github_issues({"api_url": "http://127.0.0.1:9"}, GITHUB)
    assert same.token_env == "GH_READ"  # the connector's own host: its token
