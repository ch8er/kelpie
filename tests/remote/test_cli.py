from pathlib import Path

import pytest

from kelpie.cli import main

AGENT = '[agent]\nname = "laptop"\nserver = "https://hub.example.internal"\n'


def run(tmp_path: Path, text: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    code = main(["--config", str(path), "agent"])
    return code, capsys.readouterr().err


def test_agent_needs_its_section(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, err = run(tmp_path, "", capsys)
    assert code == 2 and "needs an [agent] section" in err


def test_agent_needs_its_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_AGENT_TOKEN", raising=False)
    code, err = run(tmp_path, AGENT, capsys)
    assert code == 2 and "$KELPIE_AGENT_TOKEN is not set" in err


def test_agent_needs_an_adapter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_AGENT_TOKEN", "0123456789abcdef" * 3)
    code, err = run(tmp_path, AGENT, capsys)
    assert code == 2 and "at least one enabled [adapters.*]" in err


def _hermes(tmp_path: Path, actions: str = "") -> str:
    head = AGENT + "relay = false\n" + actions
    return head + f'[adapters.hermes]\ndb_path = "{tmp_path / "kanban.db"}"\n'


def test_an_acting_agent_refuses_a_board_another_kelpie_acts_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit M7: an agent next to a local acting kelpie serve on the same board."""
    from kelpie.core.actlock import ActLock, acquire, beside

    monkeypatch.setenv("KELPIE_AGENT_TOKEN", "0123456789abcdef" * 3)
    held = acquire([ActLock("other", beside(tmp_path / "kanban.db"))])
    try:
        code, err = run(tmp_path, _hermes(tmp_path), capsys)
    finally:
        held.release()
    assert code == 2 and "cannot start: another Kelpie acts on board" in err


def test_an_agent_that_carries_out_no_actions_takes_no_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from kelpie.core.actlock import ActLock, acquire, beside
    from kelpie.remote.agent import Agent

    monkeypatch.setenv("KELPIE_AGENT_TOKEN", "0123456789abcdef" * 3)
    monkeypatch.setattr(Agent, "run", lambda self: None)  # starts, then stops at once
    held = acquire([ActLock("other", beside(tmp_path / "kanban.db"))])
    try:
        code, err = run(tmp_path, _hermes(tmp_path, "actions = []\n"), capsys)
    finally:
        held.release()
    assert code == 0, err
