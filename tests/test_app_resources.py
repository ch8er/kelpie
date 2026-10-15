"""Host resources wired into the app: config, snapshot, metrics, status."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kelpie.app import (
    ConfigError,
    Settings,
    build_app,
    load_rules,
    load_settings,
    merged_snapshot,
    render_status,
    resource_interval_warnings,
)
from kelpie.core.model import Resource, ResourceKind
from kelpie.metrics import Metrics
from kelpie.resources.config import ResourceSettings
from tests.resources.fake_prometheus import FakeProm, fake_prometheus, vector

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
A = {"instance": "10.0.0.1:9100"}


def toml(prom: FakeProm | None, extra: str = "") -> str:
    url = prom.url if prom else "http://127.0.0.1:9"
    return f"""
[storage]
path = ":memory:"

[engine]
dry_run = true

[engine.rules.resources]
mode = "suggest"
disk_warning = 0.85

[resources.prometheus]
url = "{url}"
timeout = "1s"
{extra}
[resources.prometheus.hosts]
host-a = "10.0.0.1:9100"
"""


def settings_from(tmp_path: Path, text: str) -> Settings:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return load_settings(path)


def disk_answers(prom: FakeProm, settings: Settings) -> None:
    assert settings.resources.prometheus is not None
    source = settings.resources.prometheus.build(environ={})
    mount = {**A, "mountpoint": "/"}
    prom.responses = {
        source.config.query_text("disk_size"): vector((mount, str(500 * 10**9))),
        source.config.query_text("disk_avail"): vector((mount, str(5 * 10**9))),
    }


def test_resources_are_off_without_a_section(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, "")
    assert settings.resources.prometheus is None
    app = build_app(Settings(db_path=None), clock=lambda: NOW)
    assert app.loop.snapshot_source().resources == ()
    assert "kelpie_resource" not in app.metrics.render()
    assert "resources: disabled" in render_status(settings, load_rules(settings.engine))


def test_load_settings_reads_the_section(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, toml(None, 'interval = "2m"\nhost_label = "instance"'))
    p = settings.resources.prometheus
    assert p is not None
    assert p.hosts == {"host-a": "10.0.0.1:9100"}


def test_load_settings_rejects_a_bad_section(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[resources.prometheus\] interval"):
        settings_from(tmp_path, toml(None, 'interval = "1s"'))
    with pytest.raises(ConfigError, match="unknown \\[resources\\] entries"):
        settings_from(tmp_path, "[resources.elsewhere]\nurl = 'x'\n")


@pytest.mark.parametrize("traces", [True, False])
def test_readings_reach_the_rule_and_the_metrics(tmp_path: Path, traces: bool) -> None:
    with fake_prometheus() as prom:
        text = toml(prom) + f"\n[traces]\nenabled = {str(traces).lower()}\n"
        settings = settings_from(tmp_path, text)
        disk_answers(prom, settings)
        app = build_app(settings, clock=lambda: NOW)
        try:
            snapshot = app.loop.snapshot_source()
            assert [(r.name, r.kind, r.at) for r in snapshot.resources] == [
                ("host-a:/", ResourceKind.DISK, NOW)
            ]
            app.loop.tick_once()
            assert any(
                rule == "resources" and kind == "alert"
                for (rule, kind, _, _) in app.metrics.actions
            )
            rendered = app.metrics.render()
            assert "kelpie_resource_source_up 1" in rendered
            assert "kelpie_resource_readings 1" in rendered
            assert "kelpie_resource_read_failures_total" in rendered
        finally:
            app.close()


def test_an_unavailable_prometheus_degrades_to_no_readings(tmp_path: Path) -> None:
    with fake_prometheus() as prom:
        prom.status = 503
        app = build_app(settings_from(tmp_path, toml(prom)), clock=lambda: NOW)
        try:
            assert app.loop.snapshot_source().resources == ()
            app.loop.tick_once()  # does not crash
            rendered = app.metrics.render()
            assert "kelpie_resource_source_up 0" in rendered
            assert 'kelpie_resource_read_failures_total{cause="http_error"} 1' in rendered
        finally:
            app.close()


def test_explicit_token_env_must_be_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = settings_from(tmp_path, toml(None, 'token_env = "KELPIE_TEST_PROM_TOKEN"'))
    monkeypatch.delenv("KELPIE_TEST_PROM_TOKEN", raising=False)
    with pytest.raises(ConfigError, match=r"\$KELPIE_TEST_PROM_TOKEN is not set"):
        build_app(settings)
    assert "NOT ready" in render_status(settings, load_rules(settings.engine))


def test_token_reaches_prometheus_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_TEST_PROM_TOKEN", "s3cret")
    with fake_prometheus() as prom:
        settings = settings_from(tmp_path, toml(prom, 'token_env = "KELPIE_TEST_PROM_TOKEN"'))
        app = build_app(settings, clock=lambda: NOW)
        try:
            app.loop.snapshot_source()
            status = render_status(settings, load_rules(settings.engine))
        finally:
            app.close()
    assert {h["Authorization"] for _, _, h in prom.requests} == {"Bearer s3cret"}
    assert "s3cret" not in status
    assert "bearer token from $KELPIE_TEST_PROM_TOKEN" in status


def test_status_describes_the_source(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, toml(None))
    text = render_status(settings, load_rules(settings.engine))
    assert "resources: Prometheus http://127.0.0.1:9, 1 host(s) by label instance" in text
    assert "no token" in text


def test_a_plain_http_token_logs_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KELPIE_PROMETHEUS_TOKEN", "s3cret")
    text = toml(None).replace("http://127.0.0.1:9", "http://prometheus.internal:9090")
    app = build_app(settings_from(tmp_path, text), clock=lambda: NOW)
    app.close()
    assert "plain http" in caplog.text
    assert "s3cret" not in caplog.text


def test_a_reader_that_raises_is_counted_and_the_tick_goes_on() -> None:
    class Broken:
        def read(self, now: datetime) -> list[Resource]:
            raise RuntimeError("boom s3cret")

    metrics = Metrics()
    metrics.set_resources(True)
    snapshot = merged_snapshot([], metrics, lambda: NOW, resources=Broken())()
    assert snapshot.resources == ()
    assert metrics.resource_failures["unexpected"] == 1
    assert 'cause="unexpected"' in metrics.render()


def test_warns_when_the_fetch_interval_outlasts_the_reading_age(tmp_path: Path) -> None:
    text = toml(None, 'interval = "15m"')
    settings = settings_from(tmp_path, text)
    loaded = load_rules(settings.engine)
    (warning,) = resource_interval_warnings(loaded.rules, settings.resources)
    assert "interval (0:15:00)" in warning and "max_reading_age" in warning
    fine = settings_from(tmp_path, toml(None, 'interval = "5m"'))
    assert resource_interval_warnings(load_rules(fine.engine).rules, fine.resources) == []
    assert resource_interval_warnings(loaded.rules, ResourceSettings()) == []
