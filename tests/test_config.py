from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.core.config import EngineConfig, Mode, load_config


def test_defaults_are_safe() -> None:
    config = EngineConfig()
    assert config.default_mode is Mode.OBSERVE
    assert config.mode_for("anything") is Mode.OBSERVE
    assert not config.dry_run


def test_load_toml(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        """
[engine]
interval = "1m"
dry_run = true
default_mode = "suggest"

[engine.rules.stale_blocks]
mode = "act"
unblock_grace = "5m"

[engine.rules.ping_pong]
max_review_rounds = 3
"""
    )
    config = load_config(path)
    assert config.interval == timedelta(minutes=1)
    assert config.dry_run
    assert config.mode_for("stale_blocks") is Mode.ACT
    assert config.mode_for("ping_pong") is Mode.SUGGEST
    assert config.rules["stale_blocks"] == {"unblock_grace": "5m"}
    assert config.rules["ping_pong"] == {"max_review_rounds": 3}


def test_missing_engine_table_means_defaults(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text("")
    assert load_config(path) == EngineConfig()


@pytest.mark.parametrize(
    "data",
    [
        {"dry_rn": True},
        {"default_mode": "yolo"},
        {"rules": {"x": {"mode": "auto"}}},
        {"dry_run": "false"},
        {"rules": {"x": "act"}},
        {"interval": 0},
    ],
)
def test_invalid_config_rejected(data: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        EngineConfig.from_mapping(data)
