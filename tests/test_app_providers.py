"""LLM provider quotas wired into the app: config, snapshot, rule, metrics, status."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kelpie.app import (
    ConfigError,
    Settings,
    budget_provider_warnings,
    build_app,
    load_rules,
    load_settings,
    merged_snapshot,
    provider_interval_warnings,
    render_status,
)
from kelpie.core.model import LimitHit, ProviderStatus
from kelpie.metrics import Metrics
from tests.connectors.fake_provider import fake_provider

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
KEY_ENV = "KELPIE_TEST_PROVIDER_KEY"
KEY = "sk-app-test-SECRET"


def toml(url: str = "http://127.0.0.1:9", extra: str = "", traces: bool = False) -> str:
    return f"""
[storage]
path = ":memory:"

[engine]
dry_run = true

[engine.rules.resources]
mode = "suggest"

[traces]
enabled = {str(traces).lower()}

[[connectors.llm_quotas.providers]]
name = "main"
kind = "headers"
api_url = "{url}"
path = "/v1/models"
token_env = "{KEY_ENV}"
timeout = "1s"
unit = "requests"
{extra}
"""


def settings_from(tmp_path: Path, text: str) -> Settings:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return load_settings(path)


@pytest.fixture(autouse=True)
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_ENV, KEY)


def test_off_without_a_section(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, '[storage]\npath = ":memory:"\n')
    assert settings.llm_quotas is None
    text = render_status(settings, load_rules(settings.engine))
    assert "providers: disabled (no [connectors.llm_quotas])" in text


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[connectors]\nci = {}\n", "unknown \\[connectors\\] entries"),
        ('connectors = "x"\n', "\\[connectors\\] must be a table"),
        ("[connectors.llm_quotas]\nproviders = []\n", "at least one"),
    ],
)
def test_bad_sections_are_refused(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        settings_from(tmp_path, text)


@pytest.mark.parametrize("traces", [True, False])
def test_probes_reach_the_snapshot_the_rule_and_the_metrics(tmp_path: Path, traces: bool) -> None:
    with fake_provider() as server:
        server.status = 429
        server.headers = {"Retry-After": "3600", "RateLimit-Limit": "100"}
        settings = settings_from(tmp_path, toml(server.url, traces=traces))
        app = build_app(settings, clock=lambda: NOW)
        try:
            snapshot = app.loop.snapshot_source()
            (status,) = snapshot.providers
            assert status.name == "main" and status.remaining == 0
            assert snapshot.limit_hits == (LimitHit("main", NOW, status.resets_at),)
            app.loop.tick_once()
            assert any(
                rule == "resources" and kind == "alert"
                for (rule, kind, _, _) in app.metrics.actions
            )
            rendered = app.metrics.render()
        finally:
            app.close()
    assert {h["Authorization"] for _, h in server.requests} == {f"Bearer {KEY}"}
    assert 'kelpie_llm_provider_up{provider="main"} 1' in rendered
    assert 'kelpie_llm_provider_auth_ok{provider="main"} 1' in rendered
    assert 'kelpie_llm_provider_remaining{provider="main"} 0' in rendered
    assert 'kelpie_llm_provider_limit{provider="main"} 100' in rendered
    assert 'kelpie_llm_provider_limit_hits_total{provider="main"} 1' in rendered
    assert KEY not in rendered


def test_an_unreachable_provider_is_unknown_and_counted(tmp_path: Path) -> None:
    app = build_app(settings_from(tmp_path, toml()), clock=lambda: NOW)
    try:
        (status,) = app.loop.snapshot_source().providers
        app.loop.tick_once()  # does not crash
        rendered = app.metrics.render()
    finally:
        app.close()
    assert not status.ok and status.error_kind == "connection"
    assert 'kelpie_llm_provider_up{provider="main"} 0' in rendered
    assert "kelpie_llm_provider_auth_ok{" not in rendered
    assert (
        'kelpie_llm_provider_probe_failures_total{provider="main",cause="connection"} 1' in rendered
    )


def test_a_missing_key_stops_the_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_ENV)
    settings = settings_from(tmp_path, toml())
    with pytest.raises(ConfigError, match=rf"\${KEY_ENV} is not set"):
        build_app(settings)
    assert "providers: NOT ready" in render_status(settings, load_rules(settings.engine))


def test_status_describes_the_providers_without_the_key(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, toml())
    text = render_status(settings, load_rules(settings.engine))
    assert "providers: main (headers http://127.0.0.1:9/v1/models, every 0:05:00)" in text
    assert KEY not in text


def test_a_reader_that_raises_is_counted_and_the_tick_goes_on() -> None:
    class Broken:
        def read(self, now: datetime) -> tuple[list[ProviderStatus], list[LimitHit]]:
            raise RuntimeError("boom")

    metrics = Metrics()
    metrics.set_providers(["main"])
    snapshot = merged_snapshot([], metrics, lambda: NOW, providers=Broken())()
    assert snapshot.providers == () and snapshot.limit_hits == ()
    assert metrics.provider_failures[("*", "unexpected")] == 1


def test_warns_when_the_probe_interval_outlasts_the_reading_age(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, toml(extra='interval = "15m"'))
    loaded = load_rules(settings.engine)
    (warning,) = provider_interval_warnings(loaded.rules, settings.llm_quotas)
    assert "provider main interval (0:15:00)" in warning
    fine = settings_from(tmp_path, toml())
    assert provider_interval_warnings(load_rules(fine.engine).rules, fine.llm_quotas) == []
    assert provider_interval_warnings(loaded.rules, None) == []


def test_warns_when_a_budget_names_an_unknown_provider(tmp_path: Path) -> None:
    budgets = """
[[budgets]]
name = "sub"
scope = "day"
unit = "tokens"
hard = 1000
mode = "subscription"
providers = ["main", "elsewhere"]
"""
    settings = settings_from(tmp_path, toml(traces=True) + budgets)
    (warning,) = budget_provider_warnings(settings.budgets, settings.llm_quotas)
    assert "'sub'" in warning and "'elsewhere'" in warning
    assert len(budget_provider_warnings(settings.budgets, None)) == 2
