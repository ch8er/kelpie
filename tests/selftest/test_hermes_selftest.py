"""``kelpie selftest stop --adapter hermes`` against a fake Hermes (tests/selftest/fake_hermes.py).

The same self-test was run against a real Hermes (see the PR); here every step and the
failures it must report.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes.worker import host_table
from kelpie.app import HermesSettings, Settings
from kelpie.cli import main
from kelpie.core.config import EngineConfig
from kelpie.selftest import hermes as selftest
from kelpie.selftest.hermes import hermes_env

pytestmark = pytest.mark.skipif(
    host_table().unavailable() is not None, reason="needs /proc or libproc"
)

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
    alert = [line for line in lines if line.startswith("      | ")]  # as Telegram shows it
    assert any("Stopped run hermes-run-1" in line and "after SIGTERM" in line for line in alert)
    assert lines[-1] == "selftest stop: passed"
    assert [p for p in tmp_path.iterdir() if p.name.startswith("kelpie-selftest-")] == []
    # Kelpie's block ran against the throwaway home, never the user's.
    block = json.loads((fake.parent / "block-env.json").read_text())
    assert block["HERMES_HOME"].startswith(str(tmp_path / "kelpie-selftest-"))
    assert block["HOME"].startswith(str(tmp_path / "kelpie-selftest-"))
    assert block["HERMES_PROFILE_NAME"] == "kelpie"


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
        "XDG_CONFIG_HOME": "/home/u/.config",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }
    env = hermes_env(base, tmp_path / "home", tmp_path / "spool.jsonl", tmp_path / "user")
    assert env == {
        "PATH": "/bin",
        "HOME": str(tmp_path / "user"),
        "LANG": "C",
        "HERMES_HOME": str(tmp_path / "home"),
        "KELPIE_HERMES_SPOOL": str(tmp_path / "spool.jsonl"),
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "SPT_NOENV": "1",
    }


def test_the_cli_runs_it(fake: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(f'[adapters.hermes]\ncli = "{fake}"\n')
    before = dict(os.environ)
    assert main(["--config", str(config), "selftest", "stop", "--adapter", "hermes"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("selftest stop: passed")
    assert dict(os.environ) == before  # Kelpie's own environment is never changed


def leftovers(tmp_path: Path) -> list[int]:
    """Processes still running with a throwaway home under ``tmp_path``, or working there."""
    table = host_table()
    root = tmp_path.resolve()
    needle = f"HERMES_HOME={root}".encode()
    found = []
    for pid in table.pids():
        if pid == os.getpid():
            continue
        p = table.read(pid)
        if p is None or p.state in "ZXx":
            continue
        env = table.environ(pid) or []
        cwd = table.cwd(pid)
        if any(v.startswith(needle) for v in env) or (cwd is not None and root in cwd.parents):
            found.append(pid)
    return found


@pytest.mark.parametrize("mode", ["silent", "silent deaf", "silent noenv"])
def test_a_runaway_rule_10_never_sees_is_cleaned_up(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Rule 10 never fires (no tool call reaches Kelpie): the self-test fails, and the runaway
    it started is ended anyway, with SIGKILL when it ignores SIGTERM."""
    (fake.parent / "mode").write_text(mode)
    monkeypatch.setattr(selftest, "STOP_TIMEOUT", 3.0)
    code, lines = run(fake)
    assert code == 1
    assert any(line.startswith("FAIL  selftest: rule 10 did not stop run 1") for line in lines)
    assert ("      cleanup: some processes of the throwaway home needed SIGKILL" in lines) == (
        "deaf" in mode
    )
    assert leftovers(tmp_path) == []
    assert [p for p in tmp_path.iterdir() if p.name.startswith("kelpie-selftest-")] == []


def test_keep_keeps_the_directory(fake: Path, tmp_path: Path) -> None:
    lines: list[str] = []
    assert selftest.run(settings(fake), lines.append, keep=True) == 0
    (kept,) = [p for p in tmp_path.iterdir() if p.name.startswith("kelpie-selftest-")]
    assert any(str(kept) in line for line in lines) and (kept / "home").is_dir()
    assert leftovers(tmp_path) == []


def test_not_in_hermes_s6_container(fake: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(selftest, "s6_container", lambda: True)
    code, lines = run(fake)
    assert code == 1 and lines[0].startswith("FAIL  host: this is Hermes' s6 container")


def test_s6_detection(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "comm").write_text("s6-svscan\n")
    assert not selftest.s6_container(tmp_path, tmp_path / "no-basedir")
    (tmp_path / "basedir").mkdir()
    assert selftest.s6_container(tmp_path, tmp_path / "basedir")
    (tmp_path / "1" / "comm").write_text("systemd\n")
    assert not selftest.s6_container(tmp_path, tmp_path / "basedir")


def test_a_rule_config_that_does_not_load_fails_first(fake: Path, tmp_path: Path) -> None:
    lines: list[str] = []
    broken = Settings(
        db_path=None,
        hermes=HermesSettings(cli=str(fake)),
        engine=EngineConfig(rules={"loop_breaker": {"identical_calls": "many"}}),
    )
    assert selftest.run(broken, lines.append) == 1
    assert lines[0].startswith("FAIL  rule 10: its configuration does not load")
    assert [p for p in tmp_path.iterdir() if p.name.startswith("kelpie-selftest-")] == []


def test_json_after_other_output() -> None:
    cli = selftest.Hermes("/bin/sh", {}, Path("/"))
    cli.run = lambda *a, **kw: 'warning {not json}\n[1]\n{"id": "t_1"}\n'  # type: ignore[method-assign]
    assert cli.json("x") == {"id": "t_1"}
    cli.run = lambda *a, **kw: "nothing"  # type: ignore[method-assign]
    with pytest.raises(selftest.Failed, match="no JSON object"):
        cli.json("x")


def test_sigterm_mid_run_cleans_up_and_restores_the_handler(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake.parent / "mode").write_text("silent")
    monkeypatch.setattr(selftest, "STOP_TIMEOUT", 60.0)
    before = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(4.0, os.kill, (os.getpid(), signal.SIGTERM))
    timer.start()
    try:
        code, lines = run(fake)
    finally:
        timer.cancel()
    assert code == 1 and "FAIL  selftest: interrupted; cleaning up" in lines
    assert signal.getsignal(signal.SIGTERM) == before
    assert leftovers(tmp_path) == []
    assert [p for p in tmp_path.iterdir() if p.name.startswith("kelpie-selftest-")] == []


def test_a_cli_that_does_not_use_the_throwaway_home_is_refused(fake: Path, tmp_path: Path) -> None:
    """A ``cli`` such as ``docker exec ... hermes`` would act on another home."""
    elsewhere = tmp_path / "bin2" / "hermes"
    elsewhere.parent.mkdir()
    elsewhere.write_text(f'#!/bin/sh\nif [ "$1" = profile ]; then exit 0; fi\nexec {fake} "$@"\n')
    elsewhere.chmod(0o755)
    code, lines = run(elsewhere)
    assert code == 1
    assert any("did not create" in line and "throwaway HERMES_HOME" in line for line in lines)
    assert not any(line.startswith("PASS  task dispatched") for line in lines)
