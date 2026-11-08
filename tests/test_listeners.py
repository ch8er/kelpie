"""kelpie serve refuses two listeners on one address: the main port, [hub] and [view]."""

from __future__ import annotations

from pathlib import Path

import pytest

from kelpie.app import ConfigError, check_listeners, load_settings

HUB = """
[hub]
listen = "{listen}"
allow_plain_http = true

[[hub.agents]]
name = "host-a"
token_env = "KELPIE_TEST_AGENT_TOKEN"
"""


def settings(tmp_path: Path, text: str, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KELPIE_TEST_AGENT_TOKEN", "t" * 40)
    config = tmp_path / "kelpie.toml"
    config.write_text(text)
    return load_settings(config)


def test_defaults_do_not_clash(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = settings(
        tmp_path, HUB.format(listen="127.0.0.1:9465") + "[view]\nenabled = true\n", monkeypatch
    )
    check_listeners(s, "127.0.0.1", 9464)  # 9464 main, 9465 hub, 9466 view


@pytest.mark.parametrize(
    ("text", "port", "names"),
    [
        (
            HUB.format(listen="127.0.0.1:9465") + "[view]\nenabled = true\nport = 9465\n",
            9464,
            "[hub] listen and [view]",
        ),
        (HUB.format(listen="127.0.0.1:9464"), 9464, "the main port"),
        ('[view]\nenabled = true\nhost = "127.0.0.1"\nport = 9464\n', 9464, "[view]"),
    ],
)
def test_a_shared_port_is_refused(
    tmp_path: Path,
    tmp_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    port: int,
    names: str,
) -> None:
    s = settings(tmp_path, text, monkeypatch)
    with pytest.raises(ConfigError, match="both listen on port"):
        check_listeners(s, "127.0.0.1", port)
    with pytest.raises(ConfigError) as err:
        check_listeners(s, "127.0.0.1", port)
    assert names in str(err.value)


def test_a_disabled_view_and_other_hosts_are_fine(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = settings(tmp_path, "[view]\nport = 9464\n", monkeypatch)  # not enabled
    check_listeners(s, "127.0.0.1", 9464)
    s = settings(tmp_path, '[view]\nenabled = true\nhost = "127.0.0.2"\nport = 9464\n', monkeypatch)
    check_listeners(s, "127.0.0.1", 9464)
