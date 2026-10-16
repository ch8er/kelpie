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
    monkeypatch.setenv("KELPIE_AGENT_TOKEN", "k" * 40)
    code, err = run(tmp_path, AGENT, capsys)
    assert code == 2 and "at least one enabled [adapters.*]" in err
