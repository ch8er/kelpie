"""``kelpie selftest stop --adapter hermes`` against a fake Hermes (tests/selftest/fake_hermes.py).

The same self-test was run against a real Hermes (see the PR); here every step and the
failures it must report.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes.worker import PROC
from kelpie.app import HermesSettings, Settings
from kelpie.cli import main
from kelpie.selftest import hermes as selftest
from kelpie.selftest.hermes import hermes_env

pytestmark = pytest.mark.skipif(not (PROC / "self" / "stat").exists(), reason="needs /proc")

FAKE = Path(__file__).with_name("fake_hermes.py")


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The fake ``hermes`` executable; write its mode file to change its behaviour."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "hermes"
    exe.write_text(
        f"#!{sys.executable}\nimport runpy\n"
        f"runpy.run_path({str(FAKE)!r}, {{'BIN_DIR': {str(bin_dir)!r}}}, '__main__')\n"
    )
    exe.chmod(0o755)
    monkeypatch.setattr(selftest, "TICK", 0.2)
    monkeypatch.setattr(selftest, "QUIET", 1.0)
    monkeypatch.setattr(selftest, "STOP_TIMEOUT", 60.0)
    monkeypatch.setattr(selftest, "WORKER_TIMEOUT", 20.0)
    monkeypatch.setattr(selftest.tempfile, "tempdir", str(tmp_path))  # mkdtemp lands here
    yield exe
    (bin_dir / "mode").unlink(missing_ok=True)


def settings(exe: Path, **kw: object) -> Settings:
    return Settings(db_path=None, hermes=HermesSettings(cli=str(exe), **kw))  # type: ignore[arg-type]


def run(exe: Path, **kw: object) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = selftest.run(settings(exe, **kw), lines.append)
    return code, lines


def test_a_stop_passes_end_to_end(fake: Path, tmp_path: Path) -> None:
    code, lines = run(fake)
    assert code == 0, "\n".join(lines)
    steps = [line.split(":")[0] for line in lines if line.startswith(("PASS", "FAIL"))]
    assert steps == [
        "PASS  Hermes CLI",
        "PASS  throwaway Hermes home",
        "PASS  task dispatched",
        "PASS  worker running",
        "PASS  rule 10 stopped the run",
        "PASS  task blocked for a human",
        "PASS  block signed by Kelpie",
        "PASS  worker ended",
        "PASS  no model calls after the stop",
    ]
    stopped = next(line for line in lines if "rule 10 stopped" in line)
    assert "limit 8 within 10m" in stopped and "after SIGTERM" in stopped
    assert any("| [CRITICAL] loop_breaker: alert t_1" in line for line in lines)
    assert lines[-1] == "selftest stop: passed"
    assert [p for p in tmp_path.iterdir() if p.name.startswith("kelpie-selftest-")] == []


def test_a_worker_ignoring_sigterm_is_killed(fake: Path) -> None:
    (fake.parent / "mode").write_text("deaf")
    code, lines = run(fake, worker_grace=timedelta(seconds=0.5))
    assert code == 0, "\n".join(lines)
    assert any("after SIGKILL" in line for line in lines)


def test_with_end_worker_off_the_selftest_fails(fake: Path) -> None:
    code, lines = run(fake, end_worker=False)
    assert code == 1
    assert any(line.startswith("FAIL  worker ended: still running") for line in lines)
    assert "      fix: set [adapters.hermes] end_worker = true" in lines
    assert any(line.startswith("FAIL  no model calls after the stop") for line in lines)
    assert lines[-1].startswith("selftest stop: FAILED")


def test_a_block_signed_by_someone_else_fails(fake: Path) -> None:
    (fake.parent / "mode").write_text("author")
    code, lines = run(fake)
    assert code == 1
    assert any(line.startswith("FAIL  block signed by Kelpie") for line in lines)


def test_a_profile_hermes_will_not_spawn_is_explained(fake: Path) -> None:
    (fake.parent / "mode").write_text("nonspawnable")
    code, lines = run(fake)
    assert code == 1
    assert "FAIL  selftest: Hermes did not spawn t_1: skipped_nonspawnable" in lines
    assert "      fix: kanban.dispatch_profiles must list kelpie-selftest" in lines


def test_a_run_that_ends_by_itself_fails(fake: Path) -> None:
    (fake.parent / "mode").write_text("crash")
    code, lines = run(fake)
    assert code == 1
    assert any("ended before it was stopped (crashed: boom)" in line for line in lines)


def test_without_hermes_nothing_runs(tmp_path: Path) -> None:
    lines: list[str] = []
    code = selftest.run(settings(tmp_path / "no-hermes"), lines.append)
    assert code == 1 and lines[0].startswith("FAIL  Hermes CLI:")


def test_hermes_gets_no_provider_keys_nor_the_users_home(tmp_path: Path) -> None:
    base = {
        "PATH": "/bin",
        "HOME": "/home/u",
        "LANG": "C",
        "HERMES_HOME": "/home/u/.hermes",
        "HERMES_PROFILE": "coder",
        "OPENROUTER_API_KEY": "sk-or-secret",
        "ANTHROPIC_API_KEY": "secret",
        "OPENAI_BASE_URL": "https://api.example.com",
    }
    env = hermes_env(base, tmp_path / "home", tmp_path / "spool.jsonl")
    assert env == {
        "PATH": "/bin",
        "HOME": "/home/u",
        "LANG": "C",
        "HERMES_HOME": str(tmp_path / "home"),
        "KELPIE_HERMES_SPOOL": str(tmp_path / "spool.jsonl"),
    }


def test_the_cli_runs_it(fake: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(f'[adapters.hermes]\ncli = "{fake}"\n')
    assert main(["--config", str(config), "selftest", "stop", "--adapter", "hermes"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("selftest stop: passed")
    assert os.environ.get("HERMES_HOME") != str(tmp_path)  # restored
