from datetime import timedelta
from typing import Any

import pytest

from kelpie.resources.config import (
    DEFAULT_TOKEN_ENV,
    PrometheusSettings,
    ResourceSettings,
    load_resources,
)

MINIMAL: dict[str, Any] = {"url": "http://prometheus:9090", "hosts": {"host-a": "host-a:9100"}}


def section(**overrides: Any) -> dict[str, Any]:
    return {"prometheus": {**MINIMAL, **overrides}}


def prometheus(**overrides: Any) -> PrometheusSettings:
    settings = load_resources(section(**overrides)).prometheus
    assert settings is not None
    return settings


def test_off_by_default() -> None:
    assert load_resources(None) == ResourceSettings()
    assert load_resources({}) == ResourceSettings()
    assert load_resources({}).prometheus is None


def test_defaults() -> None:
    p = prometheus()
    assert p.url == "http://prometheus:9090"
    assert dict(p.hosts) == {"host-a": "host-a:9100"}
    assert p.host_label == "instance"
    assert p.timeout == timedelta(seconds=5)
    assert p.interval == timedelta(seconds=60)
    assert p.token_env == DEFAULT_TOKEN_ENV
    assert not p.token_env_explicit
    assert dict(p.queries) == {}


def test_every_key() -> None:
    p = prometheus(
        host_label="nodename",
        timeout="10s",
        interval="2m",
        token_env="MY_PROM_TOKEN",
        queries={"load1": " node_load1 "},
    )
    assert p.host_label == "nodename"
    assert p.timeout == timedelta(seconds=10)
    assert p.interval == timedelta(minutes=2)
    assert p.token_env == "MY_PROM_TOKEN" and p.token_env_explicit
    assert dict(p.queries) == {"load1": "node_load1"}


def test_token_comes_from_the_environment() -> None:
    p = prometheus()
    assert p.token({}) is None  # default variable unset: no token needed
    assert p.token({DEFAULT_TOKEN_ENV: " s3cret "}) == "s3cret"
    explicit = prometheus(token_env="MY_PROM_TOKEN")
    with pytest.raises(ValueError, match=r"\$MY_PROM_TOKEN is not set"):
        explicit.token({})
    with pytest.raises(ValueError, match=r"\$MY_PROM_TOKEN is not set"):
        explicit.token({"MY_PROM_TOKEN": "  "})


def test_a_token_with_control_characters_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError, match="control character") as caught:
        prometheus().token({DEFAULT_TOKEN_ENV: "abc\r\nX-Evil: 1"})
    assert "abc" not in str(caught.value)


def test_build_resolves_the_token_and_passes_the_settings_on() -> None:
    source = prometheus(timeout="3s", interval="30s").build(environ={DEFAULT_TOKEN_ENV: "tok"})
    assert source.config.token == "tok"
    assert source.config.timeout == 3.0
    assert source.config.interval == timedelta(seconds=30)
    assert source.config.hosts == {"host-a": "host-a:9100"}
    assert prometheus().build(environ={}).config.token is None


def test_plain_http_with_a_token_is_flagged_unless_it_is_loopback() -> None:
    env = {DEFAULT_TOKEN_ENV: "tok"}
    assert prometheus().sends_token_in_clear(env)
    assert not prometheus().sends_token_in_clear({})
    assert not prometheus(url="https://prometheus:9090").sends_token_in_clear(env)
    assert not prometheus(url="http://127.0.0.1:9090").sends_token_in_clear(env)
    assert not prometheus(url="http://localhost:9090").sends_token_in_clear(env)
    assert not prometheus(url="http://[::1]:9090").sends_token_in_clear(env)


def test_display_url_hides_the_path_query() -> None:
    assert (
        prometheus(url="https://prom.example.org/p/").display_url == "https://prom.example.org/p/"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "prometheus",
        {"unknown": {}},
        {"prometheus": "http://x"},
        {"prometheus": {"hosts": {"a": "b"}}},  # no url
        {"prometheus": {"url": "http://x"}},  # no hosts
        {"prometheus": {"url": "http://x", "hosts": {}}},
        {"prometheus": {"url": "http://x", "hosts": ["a"]}},
    ],
)
def test_structure_errors(raw: Any) -> None:
    with pytest.raises(ValueError):
        load_resources(raw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"url": ""},
        {"url": 5},
        {"url": "ftp://prometheus"},
        {"url": "prometheus:9090"},
        {"url": "http://"},
        {"url": "http://user:pw@prometheus:9090"},
        {"url": "http://user@prometheus:9090"},
        {"url": "http://prometheus:9090?x=1"},
        {"url": "http://prometheus:9090#frag"},
        {"url": "http://prometheus :9090"},
        {"url": "http://prometheus:0"},
        {"url": "http://prometheus:notaport"},
        {"hosts": {"host-a": ""}},
        {"hosts": {"host-a": 5}},
        {"hosts": {"host-a": "a\nb"}},
        {"hosts": {"host-a": "x:9100", "host-b": "x:9100"}},
        {"host_label": "bad-label"},
        {"host_label": ""},
        {"timeout": "0s"},
        {"timeout": "2m"},
        {"timeout": "soon"},
        {"interval": "1s"},
        {"token_env": "not a var"},
        {"token_env": ""},
        {"queries": "up"},
        {"queries": {"nope": "up"}},
        {"queries": {"load1": ""}},
        {"queries": {"load1": 5}},
        {"queries": {"load1": "x" * 2001}},
        {"unknown": 1},
    ],
)
def test_value_errors(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        load_resources(section(**overrides))
