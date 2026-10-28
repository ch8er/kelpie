"""``[instructions]`` in the app: off by default, platform defaults per adapter, snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kelpie.app import (
    ConfigError,
    Settings,
    build_adapters,
    instruction_watch,
    load_settings,
    merged_snapshot,
)
from kelpie.metrics import Metrics


def settings(tmp_path: Path, text: str) -> Settings:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return load_settings(path)


def test_off_without_the_section(tmp_path: Path, tmp_db: Path) -> None:
    s = settings(tmp_path, "[adapters.claude_code]\n")
    assert instruction_watch(s, build_adapters(s)) is None


def test_defaults_of_the_configured_platforms_after_the_configured_files(
    tmp_path: Path, tmp_db: Path
) -> None:
    s = settings(
        tmp_path,
        "[adapters.hermes]\n[adapters.claude_code]\n[adapters.openclaw]\n"
        '[instructions]\n[[instructions.files]]\npaths = ["~/farm/dev/CLAUDE.md"]\n'
        'role = "developer"\n',
    )
    w = instruction_watch(s, build_adapters(s))
    assert w is not None
    assert w.sources[0].paths == ("~/farm/dev/CLAUDE.md",)
    platforms = {src.platform for src in w.sources[1:]}
    assert platforms == {"hermes", "claude-code", "openclaw"}
    hermes_roles = [src for src in w.sources if src.platform == "hermes" and src.role_of]
    assert hermes_roles and hermes_roles[0].role_of is not None
    assert hermes_roles[0].role_of("dev-2") == "dev"


def test_defaults_off(tmp_path: Path, tmp_db: Path) -> None:
    s = settings(tmp_path, "[adapters.claude_code]\n[instructions]\ndefaults = false\n")
    w = instruction_watch(s, build_adapters(s))
    assert w is not None and w.sources == ()


def test_bad_section_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown \[instructions\] keys"):
        settings(tmp_path, "[instructions]\nbogus = 1\n")


def test_the_snapshot_carries_the_measured_files(tmp_path: Path, tmp_db: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Core\n")
    s = settings(
        tmp_path,
        f'[instructions]\ndefaults = false\n[[instructions.files]]\npaths = ["{tmp_path}/*.md"]\n',
    )
    w = instruction_watch(s, [])
    now = datetime(2026, 3, 2, tzinfo=UTC)
    snap = merged_snapshot([], Metrics(), lambda: now, instructions=w)()
    (f,) = snap.instruction_files
    assert f.path.endswith("AGENTS.md") and f.chars == 7 and f.sections == 1
    assert f.tokens_per_day == 0 and f.token_basis == "sessions"  # no sessions today
