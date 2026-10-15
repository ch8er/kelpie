"""[[alerts.channels]] wired into the app: config, startup validation, engine, metrics, status."""

import time
from pathlib import Path

import pytest

from kelpie.app import ConfigError, build_app, load_rules, load_settings, render_status
from tests.alerts.fake_telegram import fake_telegram

ENV = "KELPIE_TEST_TG_TOKEN"
TOKEN = "987654:ZZtop_SECRET-value-0123456789abcdef"


def write(tmp_path: Path, url: str = "http://127.0.0.1:9", extra: str = "") -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(f"""
[storage]
path = ":memory:"

[traces]
enabled = false

[alerts]
{extra}

[[alerts.channels]]
type = "telegram"
token_env = "{ENV}"
chat_id = "-100777"
api_url = "{url}"
min_severity = "info"
""")
    return path


def test_no_section_means_no_alerter_and_status_says_so(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text('[storage]\npath = ":memory:"\n[traces]\nenabled = false\n')
    settings = load_settings(path)
    assert build_app(settings).engine.alerter is None
    assert "alerts:   disabled" in render_status(settings, load_rules(settings.engine))


def test_a_missing_token_stops_startup_naming_the_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    settings = load_settings(write(tmp_path))
    status = render_status(settings, load_rules(settings.engine))
    assert f"alerts:   NOT ready: [alerts] channel telegram: ${ENV} is not set" in status
    with pytest.raises(ConfigError, match=ENV):
        build_app(settings)


def test_bad_alert_config_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="queue_size"):
        load_settings(write(tmp_path, extra="queue_size = 0"))


def test_the_engine_delivers_through_the_channel_and_metrics_count_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    with fake_telegram() as fake:
        settings = load_settings(write(tmp_path, fake.url))
        status = render_status(settings, load_rules(settings.engine))
        assert (
            f"alerts:   telegram: telegram (chat -100777, from info, token from ${ENV})" in status
        )
        assert TOKEN not in status
        app = build_app(settings)
        try:
            assert app.engine.alerter is not None and app.alerts is not None
            app.engine.alerter.send("[r] suggest requeue task-1", "Requeue it.", "warning")  # type: ignore[union-attr]
            end = time.monotonic() + 3
            while not app.metrics.alerts_sent["telegram"] and time.monotonic() < end:
                time.sleep(0.01)
            rendered = app.metrics.render()
        finally:
            app.close()
    (path, _, body) = fake.requests[0]
    assert path == f"/bot{TOKEN}/sendMessage"
    assert "[WARNING] r: requeue task-1" in body["text"]
    assert "Kelpie did: nothing, mode suggest" in body["text"]
    assert TOKEN not in rendered
    assert 'kelpie_alerts_sent_total{channel="telegram"} 1' in rendered
