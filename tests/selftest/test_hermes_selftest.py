"""``kelpie selftest stop --adapter hermes`` against a fake Hermes (tests/selftest/fake_hermes.py).

The same self-test was run against a real Hermes (see the PR); here every step and the
failures it must report.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes.adapter import HermesAdapter
from kelpie.adapters.hermes.worker import UNVERIFIED, RunWorker, host_table
from kelpie.app import HermesBoardSettings, HermesSettings, Settings
from kelpie.cli import main
from kelpie.core.config import EngineConfig
from kelpie.selftest import hermes as selftest
from kelpie.selftest.hermes import hermes_env
from kelpie.sim.board import KANBAN_SCHEMA

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
        "PASS  board in WAL mode",
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


def test_a_multi_board_config_passes_through_the_boards_adapter(fake: Path, tmp_path: Path) -> None:
    """Issue 333: with [[adapters.hermes.boards]] the self-test stops the run through the
    boards adapter, routed by the board prefix of the run's id, and never opens the user's
    boards."""
    farm = tmp_path / "farm"
    boards = tuple(
        HermesBoardSettings(name, farm / name / "kanban.db", farm / f"{name}.key")
        for name in ("qr", "sign")
    )
    code, lines = run(fake, boards=boards)
    assert code == 0, "\n".join(lines)
    assert not any(line.startswith("FAIL") for line in lines)
    stopped = next(line for line in lines if "rule 10 stopped" in line)
    assert "routed to board kelpie-selftest" in stopped
    assert "kelpie-selftest:hermes-run-1" in stopped and "after SIGTERM" in stopped
    assert any(line.startswith("PASS  worker ended") for line in lines)
    assert lines[-1] == "selftest stop: passed"
    assert not farm.exists()  # the user's boards and keys: never opened nor created
    block = json.loads((fake.parent / "block-env.json").read_text())
    assert block["HERMES_HOME"].startswith(str(tmp_path / "kelpie-selftest-"))
    assert block["HERMES_KANBAN_DB"].startswith(str(tmp_path / "kelpie-selftest-"))
    assert block["HERMES_KANBAN_DB"].endswith("/kanban/boards/kelpie-selftest/kanban.db")
    assert leftovers(tmp_path) == []


@pytest.mark.parametrize("multi", [False, True])
def test_the_users_hermes_home_state_is_never_read(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi: bool
) -> None:
    """The self-test reads no state.db of the user's Hermes home (``state_home``)."""
    reads: list[object] = []
    monkeypatch.setattr(HermesAdapter, "_state", lambda self, *a, **kw: reads.append(a))
    home = tmp_path / "users-hermes"
    boards = (HermesBoardSettings("qr", tmp_path / "qr.db"),) if multi else ()
    code, lines = run(fake, state_home=home, boards=boards)
    assert code == 0, "\n".join(lines)
    assert reads == []


def test_the_cli_runs_it_with_a_multi_board_config(
    fake: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The farm's path: a config file with two boards (keys defaulted next to the ledger)."""
    config = tmp_path / "kelpie.toml"
    farm = tmp_path / "farm"
    config.write_text(
        f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n'
        f'[adapters.hermes]\ncli = "{fake}"\n'
        f'[[adapters.hermes.boards]]\nname = "qr"\ndb_path = "{farm / "qr.db"}"\n'
        f'[[adapters.hermes.boards]]\nname = "sign"\ndb_path = "{farm / "sign.db"}"\n'
    )
    assert main(["--config", str(config), "selftest", "stop", "--adapter", "hermes"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("selftest stop: passed"), out
    assert "routed to board kelpie-selftest" in out
    assert not farm.exists()
    assert not list(tmp_path.glob("hermes-wait*.key"))  # the user's keys are not created


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


def test_spool_lines_come_from_the_installed_plugin(fake: Path, tmp_path: Path) -> None:
    """The fake agent writes nothing itself: every spool line is the real plugin's (#353),
    with the writer the adapter checks."""
    seen: list[dict[str, object]] = []
    original = selftest._calls

    def spy(spool: Path, rid: int) -> int:
        seen.extend(selftest._lines(spool, rid))
        return original(spool, rid)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(selftest, "_calls", spy)
        code, out = run(fake)
    assert code == 0, "\n".join(out)
    assert seen, "no spool lines"
    assert all(isinstance(e["pid_start"], str) and e["pid_start"] for e in seen)
    assert all(e.get("output_chars") and e.get("exit_code") == 0 for e in seen)  # plugin-only


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        # #353: the plugin of 0.1.0a2 on macOS
        ("def _fingerprint(pid):\n    return None\n", "carry no pid_start"),
        # a writer that is not the run's recorded worker
        ("def _fingerprint(pid):\n    return '|1'\n", "none was written by the run's"),
    ],
)
def test_a_plugin_whose_lines_cannot_stop_a_run_fails_and_says_why(
    fake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: str, expected: str
) -> None:
    """The class of #353: the spool fills, but rule 10 may not stop the run on those lines.
    The self-test fails, and says why, instead of passing through a shortcut."""
    broken = tmp_path / "plugin.py"
    broken.write_text(selftest.PLUGIN.read_text() + "\n\n" + patch)
    monkeypatch.setattr(selftest, "PLUGIN", broken)
    monkeypatch.setattr(selftest, "STOP_TIMEOUT", 15.0)  # slow macOS runners
    code, lines = run(fake)
    assert code == 1
    fail = next(line for line in lines if line.startswith("FAIL  selftest:"))
    assert "rule 10 did not stop run 1" in fail and expected in fail, fail


def write_spool(path: Path, writers: list[tuple[int, str | None]]) -> Path:
    lines = [
        {"type": "tool", "run_id": "1", "pid": pid, "pid_start": start} for pid, start in writers
    ]
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


WORKER = RunWorker(10, "|100", 11, "|111", "h:1")


@pytest.mark.parametrize(
    ("writers", "worker", "expected"),
    [
        ([], WORKER, "check that Kelpie's plugin loads"),
        ([(11, "|111"), (11, None)], WORKER, "1 of them were written by the run's recorded"),
        ([(11, None), (11, None)], WORKER, "2 of them carry no pid_start"),
        ([(11, "|111")], RunWorker(10, UNVERIFIED, None, None, "h:1"), "no usable start"),
        ([(11, "|999")], WORKER, "none was written by the run's recorded worker"),
    ],
)
def test_a_run_not_stopped_says_what_the_spool_shows(
    tmp_path: Path, writers: list[tuple[int, str | None]], worker: RunWorker, expected: str
) -> None:
    spool = write_spool(tmp_path / "spool.jsonl", writers)
    text = selftest._not_stopped(spool, 1, worker, "limit 8 within 10m")
    assert text.startswith(f"rule 10 did not stop run 1 within {selftest.STOP_TIMEOUT:g}s")
    assert f"({len(writers)} tool calls" in text and expected in text, text


def test_the_psutil_hint_is_for_macos_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = write_spool(tmp_path / "spool.jsonl", [(11, "|111")])
    worker = RunWorker(10, None, 11, None, "h:1")
    for platform, psutil in (("darwin", True), ("linux", False)):
        monkeypatch.setattr(selftest.sys, "platform", platform)
        assert ("psutil" in selftest._not_stopped(spool, 1, worker, "t")) is psutil


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


class _Mock:
    """The mock's count; ``sleep`` (patched) adds ``arrive[seconds]`` calls, so the drain
    and the quiet window must differ in length (DRAIN 1s, QUIET patched to 5s)."""

    def __init__(self, calls: int, arrive: dict[float, int]) -> None:
        self.calls, self.arrive = calls, arrive

    def sleep(self, seconds: float) -> None:
        self.calls += self.arrive.get(seconds, 0)


@pytest.mark.parametrize(
    ("in_drain", "in_quiet", "ok"),
    [
        (0, 0, True),  # nothing in flight
        (1, 0, True),  # the request in flight at the stop, answered during the drain
        (2, 0, False),  # more than the one in flight: the agent kept going
        (0, 1, False),  # a call during the quiet window
        (1, 1, False),
    ],
)
def test_one_call_in_flight_at_the_stop_may_still_be_answered(
    monkeypatch: pytest.MonkeyPatch, in_drain: int, in_quiet: int, ok: bool
) -> None:
    """macOS CI: the agent's last request, sent before SIGTERM, was answered just after
    the stop; that one call is allowed, any other fails the step."""
    monkeypatch.setattr(selftest, "QUIET", 5.0)
    mock = _Mock(10, {selftest.DRAIN: in_drain, 5.0: in_quiet})
    monkeypatch.setattr(selftest.time, "sleep", mock.sleep)
    report = selftest.Report(lambda _: None)
    selftest._check_quiet(report, mock, 10)  # type: ignore[arg-type]
    assert report.failed is not ok
    if not ok:
        assert f"{in_drain + in_quiet} more in" in report.lines[0]


# --- issue 320: a worker killed mid-write -------------------------------------------------

KILLED_MID_WRITE = """\
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA cache_size=2")  # the change spills into the database file
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE tasks SET body = hex(randomblob(2000))")
print("writing", flush=True)
time.sleep(60)
"""


def killed_mid_write(db: Path, wal: bool) -> None:
    """A board whose writer was SIGKILLed inside a transaction, as a stop ends a worker that
    was heartbeating (the race behind issue 320's flake, made deterministic)."""
    conn = sqlite3.connect(db)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(KANBAN_SCHEMA)
    conn.executemany(
        "INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'x', 'running', 1)",
        [(f"t_{n}",) for n in range(300)],
    )
    conn.commit()
    conn.close()
    with subprocess.Popen(
        [sys.executable, "-c", KILLED_MID_WRITE, str(db)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as writer:
        assert writer.stdout is not None and writer.stderr is not None
        line = writer.stdout.readline()
        writer.kill()
        writer.wait()
        assert line == "writing\n", writer.stderr.read()


def test_a_hot_journal_fails_the_step_with_its_cause(tmp_path: Path) -> None:
    """Rollback-journal mode (the fake board before the fix): Kelpie's read-only read cannot
    roll the killed writer's journal back. It fails the step and says why."""
    db = tmp_path / "kanban.db"
    killed_mid_write(db, wal=False)
    assert db.with_name("kanban.db-journal").exists()
    with pytest.raises(selftest.Failed, match=r"(?s)hot journal .* WAL mode"):
        selftest._read(db, "SELECT status FROM tasks WHERE id = 't_1'")
    assert db.with_name("kanban.db-journal").exists()  # Kelpie wrote nothing


def test_a_wal_board_reads_after_a_writer_was_killed(tmp_path: Path) -> None:
    """WAL, as Hermes keeps its boards: the killed transaction is simply not there."""
    db = tmp_path / "kanban.db"
    killed_mid_write(db, wal=True)
    rows = selftest._read(db, "SELECT status, body FROM tasks WHERE id = 't_1'")
    assert [(r["status"], r["body"]) for r in rows] == [("running", None)]


def test_the_fake_hermes_keeps_its_boards_in_wal_mode(fake: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    env = {**os.environ, "HERMES_HOME": str(home)}
    subprocess.run([str(fake), "kanban", "boards", "create", "b"], env=env, check=True)
    db = home / "kanban" / "boards" / "b" / "kanban.db"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_a_board_that_cannot_be_read_fails_the_step(tmp_path: Path) -> None:
    with pytest.raises(selftest.Failed, match="cannot open the throwaway board"):
        selftest._read(tmp_path / "missing" / "kanban.db", "SELECT 1")
    junk = tmp_path / "kanban.db"
    junk.write_bytes(b"not a database" * 100)
    with pytest.raises(selftest.Failed, match=r"cannot read the throwaway board .*not a database"):
        selftest._read(junk, "SELECT id FROM tasks")


@pytest.mark.parametrize("wal", [True, False])
def test_the_journal_mode_is_read(tmp_path: Path, wal: bool) -> None:
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(db)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(KANBAN_SCHEMA)
    conn.close()
    assert selftest._journal_mode(db) == ("wal" if wal else "delete")


def test_a_board_not_in_wal_mode_fails_the_gate(fake: Path) -> None:
    """Even when the stop happens to land between two writes: on such a Hermes, Kelpie may be
    unable to read a board after any stop, so the self-test never certifies it."""
    (fake.parent / "mode").write_text("rollback")
    code, lines = run(fake)
    assert code == 1
    assert "FAIL  board in WAL mode: journal mode 'delete': after a stop" in "\n".join(lines)
    assert (
        "      fix: rule 10 stays out of act on this Hermes; tell the Kelpie maintainers" in lines
    )
    assert lines[-1].startswith("selftest stop: FAILED")
