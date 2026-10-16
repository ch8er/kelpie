"""``kelpie serve`` with ``[hub]``: refused next to in-process adapters; otherwise the hub
listens, an agent's board is merged into the tick and actions route back to it."""

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import ConfigError, build_app, load_settings
from kelpie.remote.agent import Agent
from kelpie.remote.config import AgentSettings
from kelpie.remote.hub import HubService
from tests.remote.test_hub import BoardAdapter

TOKEN = "0123456789abcdef" * 3


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def config(tmp_path: Path, extra: str = "", port: int = 0) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        '[storage]\npath = ":memory:"\n'
        f'[hub]\nlisten = "127.0.0.1:{port or free_port()}"\nallow_plain_http = true\n'
        '[[hub.agents]]\nname = "laptop"\ntoken_env = "KELPIE_TEST_AGENT_TOKEN"\n' + extra
    )
    return path


def test_hub_and_in_process_adapters_exclude_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_TEST_AGENT_TOKEN", TOKEN)
    extra = f'[adapters.hermes]\ndb_path = "{tmp_path / "kanban.db"}"\nspool = false\n'
    with pytest.raises(ConfigError, match="run `kelpie agent`"):
        build_app(load_settings(config(tmp_path, extra)))


def test_hub_needs_the_agent_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_TEST_AGENT_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="KELPIE_TEST_AGENT_TOKEN is not set"):
        build_app(load_settings(config(tmp_path)))


def test_a_port_in_use_is_a_config_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KELPIE_TEST_AGENT_TOKEN", TOKEN)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        with pytest.raises(ConfigError, match="cannot listen"):
            build_app(load_settings(config(tmp_path, port=busy.getsockname()[1])))


def test_an_agents_board_is_merged_and_actions_route_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_TEST_AGENT_TOKEN", TOKEN)
    port = free_port()
    app = build_app(load_settings(config(tmp_path, port=port)))
    try:
        assert app.engine.fallback is False
        assert app.ingest is None  # spans come through agents only
        (service,) = [s for s in app.services if isinstance(s, HubService)]
        service.start()
        settings = AgentSettings(
            name="laptop",
            server=f"http://127.0.0.1:{port}",
            allow_plain_http=True,
            interval=timedelta(seconds=5),
        )
        adapter = BoardAdapter()
        agent = Agent(settings, [adapter], TOKEN, clock=lambda: datetime.now(UTC))
        agent.report()
        snapshot = app.loop.snapshot_source()
        assert [t.id for t in snapshot.tasks] == ["laptop:t_1a"]
        assert snapshot.unobserved == ()
        assert "laptop/fake" in app.engine.executors
        assert "kelpie_agent_up" in app.metrics.render()
    finally:
        app.close()
        for s in app.services:
            if isinstance(s, HubService):
                s.stop()
