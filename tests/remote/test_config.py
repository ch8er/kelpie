from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import ConfigError, load_settings
from kelpie.core.actions import ActionKind
from kelpie.remote.config import (
    AgentSettings,
    load_agent,
    load_hub,
    private_address,
    read_token,
)

TOKEN = "x" * 40


def agent(**kw: Any) -> AgentSettings:
    settings = load_agent({"name": "laptop", "server": "https://kelpie.example.internal", **kw})
    assert settings is not None
    return settings


def hub(**kw: Any) -> dict[str, Any]:
    return {
        "tls_cert": "/etc/kelpie/tls.crt",
        "tls_key": "/etc/kelpie/tls.key",
        "agents": [{"name": "laptop", "token_env": "KELPIE_AGENT_LAPTOP_TOKEN"}],
        **kw,
    }


def test_agent_defaults() -> None:
    settings = agent()
    assert settings.server == "https://kelpie.example.internal:9465"
    assert settings.token_env == "KELPIE_AGENT_TOKEN"
    assert settings.interval == timedelta(seconds=15)
    assert settings.actions is None
    assert not settings.plain_http
    settings.check_server(resolve=lambda host: ["8.8.4.4"])  # https: any address


def test_agent_actions_narrow_the_kinds() -> None:
    assert agent(actions=["comment", "stop"]).actions == {ActionKind.COMMENT, ActionKind.STOP}


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"name": "Laptop"}, "name"),
        ({"name": "a:b"}, "name"),
        ({"server": "ftp://x"}, "https://host"),
        ({"server": "https://x/kelpie"}, "without a path"),
        ({"server": "https://user:pw@x"}, "https://host"),
        ({"server": "https://x:99999"}, "invalid port"),
        ({"server": "http://10.0.0.5:9465"}, "allow_plain_http"),
        ({"server": "http://10.0.0.5", "allow_plain_http": True, "ca_file": "/ca.pem"}, "ca_file"),
        ({"actions": ["run_shell"]}, "actions"),
        ({"actions": ["cancel_build"]}, "actions"),
        ({"interval": "0s"}, "interval"),
        ({"token_env": "not a name"}, "token_env"),
        ({"max_body_bytes": 10}, "max_body_bytes"),
        ({"surprise": 1}, "unknown"),
    ],
)
def test_bad_agent_config(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        agent(**kw)


def test_plain_http_only_to_private_addresses() -> None:
    settings = agent(server="http://kelpie.lan", allow_plain_http=True)
    assert settings.plain_http
    settings.check_server(resolve=lambda host: ["192.168.1.10", "fd00::1"])
    with pytest.raises(ValueError, match=r"8\.8\.4\.4"):
        settings.check_server(resolve=lambda host: ["192.168.1.10", "8.8.4.4"])
    with pytest.raises(ValueError, match="nothing"):
        settings.check_server(resolve=lambda host: [])


def test_hub_with_tls() -> None:
    settings = load_hub(hub())
    assert settings is not None and settings.tls
    assert (settings.host, settings.port) == ("0.0.0.0", 9465)
    assert settings.tokens({"KELPIE_AGENT_LAPTOP_TOKEN": TOKEN}) == {"laptop": TOKEN}


def test_hub_plain_http_on_a_private_address() -> None:
    raw = hub(listen="100.101.102.103:9465", allow_plain_http=True)
    del raw["tls_cert"], raw["tls_key"]
    settings = load_hub(raw)
    assert settings is not None and not settings.tls


@pytest.mark.parametrize(
    ("change", "drop", "message"),
    [
        ({}, ("tls_cert", "tls_key"), "needs tls_cert"),
        ({"allow_plain_http": True}, ("tls_cert", "tls_key"), "not 0.0.0.0"),
        ({"allow_plain_http": True, "listen": "8.8.4.4:9465"}, ("tls_cert", "tls_key"), "not 8"),
        ({"allow_plain_http": True}, (), "exclude"),
        ({}, ("tls_key",), "go together"),
        ({"listen": "9465"}, (), "host:port"),
        ({"listen": "0.0.0.0:0"}, (), "host:port"),
        ({"agents": []}, (), "at least one"),
        ({"agents": [{"name": "a"}]}, (), "token_env is required"),
        ({"agents": [{"name": "a", "token_env": "T", "x": 1}]}, (), "unknown"),
        (
            {"agents": [{"name": "a", "token_env": "T"}, {"name": "a", "token_env": "U"}]},
            (),
            "distinct",
        ),
        (
            {"agents": [{"name": "a", "token_env": "T"}, {"name": "b", "token_env": "T"}]},
            (),
            "own token_env",
        ),
        ({"silent_after": "5s"}, (), "silent_after"),
        ({"action_timeout": "10m"}, (), "action_timeout"),
        ({"max_connections": 3}, (), "at least 2 per agent"),
        ({"max_clock_skew": "1s"}, (), "max_clock_skew"),
        ({"surprise": 1}, (), "unknown"),
    ],
)
def test_bad_hub_config(change: dict[str, Any], drop: tuple[str, ...], message: str) -> None:
    raw = hub(**change)
    for key in drop:
        del raw[key]
    with pytest.raises(ValueError, match=message):
        load_hub(raw)


def test_hub_tokens_must_be_strong_and_distinct() -> None:
    settings = load_hub(
        hub(agents=[{"name": "a", "token_env": "TA"}, {"name": "b", "token_env": "TB"}])
    )
    assert settings is not None
    with pytest.raises(ValueError, match=r"\$TB is not set"):
        settings.tokens({"TA": TOKEN})
    with pytest.raises(ValueError, match="same token"):
        settings.tokens({"TA": TOKEN, "TB": TOKEN})
    with pytest.raises(ValueError, match="at least 32"):
        settings.tokens({"TA": TOKEN, "TB": "short"})


def test_read_token_refuses_non_printable() -> None:
    with pytest.raises(ValueError, match="printable"):
        read_token("T", "x", {"T": "a b" * 20})


@pytest.mark.parametrize(
    ("host", "private"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("10.1.2.3", True),
        ("192.168.0.1", True),
        ("100.64.0.1", True),
        ("fd12::1", True),
        ("0.0.0.0", False),
        ("::", False),
        ("8.8.4.4", False),
        ("8.8.8.8", False),
        ("localhost", False),  # a name, not an address
    ],
)
def test_private_address(host: str, private: bool) -> None:
    assert private_address(host) is private


def test_sections_load_from_the_config_file(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        '[agent]\nname = "vm-1"\nserver = "https://hub.example.internal:9465"\n'
        '[hub]\ntls_cert = "c"\ntls_key = "k"\n'
        '[[hub.agents]]\nname = "vm-1"\ntoken_env = "T1"\n'
    )
    settings = load_settings(path)
    assert settings.agent is not None and settings.agent.name == "vm-1"
    assert settings.hub is not None and settings.hub.agents[0].token_env == "T1"
    path.write_text('[agent]\nname = "vm-1"\n')
    with pytest.raises(ConfigError, match="server is required"):
        load_settings(path)
