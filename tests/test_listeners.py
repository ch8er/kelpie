"""kelpie serve refuses two listeners on one address: the main port, [hub] and [view]."""

from __future__ import annotations

from pathlib import Path

import pytest

from kelpie.app import ConfigError, Settings, check_listeners, load_settings

HUB = """
[hub]
listen = "{listen}"
{tls}

[[hub.agents]]
name = "host-a"
token_env = "KELPIE_TEST_AGENT_TOKEN"
"""


def hub(listen: str, tmp_path: Path) -> str:
    """A [hub] section; off a private address it needs TLS files (never read here)."""
    cert, key = tmp_path / "tls.crt", tmp_path / "tls.key"
    cert.write_text("x")
    key.write_text("x")
    private = listen.startswith(("127.", "10.", "[::1]"))
    tls = "allow_plain_http = true" if private else f'tls_cert = "{cert}"\ntls_key = "{key}"'
    return HUB.format(listen=listen, tls=tls)


def settings(tmp_path: Path, text: str, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("KELPIE_TEST_AGENT_TOKEN", "t" * 40)
    config = tmp_path / "kelpie.toml"
    config.write_text(text)
    return load_settings(config)


VIEW = '[view]\nenabled = true\nhost = "{host}"\nport = {port}\n'


def test_defaults_do_not_clash(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = hub("0.0.0.0:9465", tmp_path) + "[view]\nenabled = true\n"
    check_listeners(settings(tmp_path, text, monkeypatch), "127.0.0.1", 9464)


@pytest.mark.parametrize(
    ("hub_at", "view_at", "main", "names"),
    [
        ("127.0.0.1:9465", ("127.0.0.1", 9465), ("127.0.0.1", 9464), "[hub] listen and [view]"),
        ("127.0.0.1:9464", None, ("127.0.0.1", 9464), "the main port"),
        (None, ("127.0.0.1", 9464), ("127.0.0.1", 9464), "[view]"),
        ("0.0.0.0:9464", None, ("127.0.0.1", 9464), "[hub] listen"),  # wildcard hub
        ("0.0.0.0:9466", ("::", 9466), ("127.0.0.1", 9464), "[view]"),  # both wildcards
        (None, ("localhost", 9464), ("127.0.0.1", 9464), "[view]"),
        (None, ("[::1]", 9464), ("0:0:0:0:0:0:0:1", 9464), "[view]"),  # canonical IPv6
        (None, ("127.0.0.1", 9464), ("", 9464), "the main port"),  # main on every address
    ],
)
def test_a_shared_port_is_refused(
    tmp_path: Path,
    tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    hub_at: str | None,
    view_at: tuple[str, int] | None,
    main: tuple[str, int],
    names: str,
) -> None:
    text = (hub(hub_at, tmp_path) if hub_at else "") + (
        VIEW.format(host=view_at[0], port=view_at[1]) if view_at else ""
    )
    with pytest.raises(ConfigError, match="both listen on port") as err:
        check_listeners(settings(tmp_path, text, monkeypatch), *main)
    assert names in str(err.value)


@pytest.mark.parametrize(
    ("text", "main"),
    [
        ("[view]\nport = 9464\n", ("127.0.0.1", 9464)),  # not enabled
        (VIEW.format(host="127.0.0.2", port=9464), ("127.0.0.1", 9464)),  # another host
        (VIEW.format(host="0.0.0.0", port=9466), ("127.0.0.1", 9464)),  # wildcard, other port
    ],
)
def test_other_hosts_ports_and_a_disabled_view_are_fine(
    tmp_path: Path,
    tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    main: tuple[str, int],
) -> None:
    check_listeners(settings(tmp_path, text, monkeypatch), *main)
