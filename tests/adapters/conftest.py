"""Shared fixtures for the adapter tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from kelpie.adapters.claude_code import hook


@pytest.fixture(autouse=True)
def _no_real_claude_process(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Claude Code hook records the process it runs under (CLAUDE_PID, /proc). Tests must
    not depend on whether they run inside Claude Code: point the hook at an empty /proc;
    tests about liveness set what they need."""
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.setattr(hook, "PROC", Path(tmp_path_factory.mktemp("no-proc")))
