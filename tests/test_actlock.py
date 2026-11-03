"""One acting Kelpie per board (audit #275, M7)."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kelpie.adapters.claude_code import ClaudeCodeAdapter
from kelpie.adapters.openclaw.adapter import OpenClawAdapter
from kelpie.app import board_locks, build_adapters, load_settings, serve_act_locks
from kelpie.cli import main
from kelpie.core.actlock import ActLock, ActLockError, acquire, beside, keyed, state_dir


def _lock(tmp_path: Path, name: str = "kanban.db") -> ActLock:
    return ActLock("qr", beside(tmp_path / name))


def test_a_second_holder_is_refused_with_the_board_and_the_pid(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    held = acquire([lock], pid=4242)
    try:
        with pytest.raises(ActLockError) as exc:  # flock is per open file: same process too
            acquire([lock])
        text = str(exc.value)
        assert "another Kelpie acts on board qr (pid 4242)" in text
        assert str(lock.path) in text
    finally:
        held.release()
    acquire([lock]).release()  # released: the next instance gets it


def test_another_process_is_refused_while_the_lock_is_held(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    held = acquire([lock])
    try:
        code = textwrap.dedent(
            f"""
            from pathlib import Path
            from kelpie.core.actlock import ActLock, ActLockError, acquire
            try:
                acquire([ActLock("qr", Path({str(lock.path)!r}))])
            except ActLockError as exc:
                print(exc)
                raise SystemExit(3)
            """
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert done.returncode == 3, done.stderr
        assert f"pid {os.getpid()}" in done.stdout
    finally:
        held.release()


def test_all_or_none(tmp_path: Path) -> None:
    first, second = _lock(tmp_path, "a.db"), _lock(tmp_path, "b.db")
    other = acquire([second])
    try:
        with pytest.raises(ActLockError):
            acquire([first, second])
        acquire([first]).release()  # the first lock was let go when the second failed
    finally:
        other.release()


def test_the_same_file_twice_is_taken_once(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    held = acquire([_lock(tmp_path / "real"), _lock(tmp_path / "link")])
    try:
        assert held.boards == ("qr",)
    finally:
        held.release()


def test_lock_file_names_its_holder_and_only_its_owner_writes_it(tmp_path: Path) -> None:
    held = acquire([_lock(tmp_path)], pid=77)
    try:
        path = beside(tmp_path / "kanban.db")
        assert path.name == "kanban.db.kelpie-act.lock"
        assert stat.S_IMODE(path.stat().st_mode) & 0o022 == 0
        assert path.read_text() == "77\n"
    finally:
        held.release()


def test_a_hard_link_to_another_file_is_locked_but_never_written(tmp_path: Path) -> None:
    victim = tmp_path / "precious"
    victim.write_text("keep me")
    lock = _lock(tmp_path)
    os.link(victim, lock.path)
    held = acquire([lock], pid=77)
    try:
        assert victim.read_text() == "keep me"
        with pytest.raises(ActLockError):  # still a lock
            acquire([lock])
    finally:
        held.release()


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any file for writing")
def test_a_lock_file_of_another_user_is_taken_read_only(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    lock.path.write_text("1\n")
    lock.path.chmod(0o444)  # as a file another user left behind looks to us
    held = acquire([lock], pid=77)
    try:
        assert lock.path.read_text() == "1\n"
        with pytest.raises(ActLockError):
            acquire([lock])
    finally:
        held.release()


def test_a_board_reached_through_a_symlink_has_one_lock(tmp_path: Path) -> None:
    (tmp_path / "kanban.db").write_text("")
    (tmp_path / "alias.db").symlink_to(tmp_path / "kanban.db")
    assert beside(tmp_path / "alias.db") == beside(tmp_path / "kanban.db")
    held = acquire([ActLock("real", beside(tmp_path / "kanban.db"))])
    try:
        with pytest.raises(ActLockError):
            acquire([ActLock("alias", beside(tmp_path / "alias.db"))])
    finally:
        held.release()


def test_a_fifo_or_a_symlink_is_not_taken(tmp_path: Path) -> None:
    lock = _lock(tmp_path)
    os.mkfifo(lock.path)
    with pytest.raises(ActLockError, match="not a regular file"):
        acquire([lock])  # and does not block on the FIFO
    lock.path.unlink()
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    lock.path.symlink_to(target)
    with pytest.raises(ActLockError, match="cannot take the act lock"):
        acquire([lock])
    assert target.read_text() == "keep"


def test_a_missing_board_folder_is_an_error_and_a_state_folder_is_made(tmp_path: Path) -> None:
    with pytest.raises(ActLockError, match="does not exist"):
        acquire([ActLock("qr", beside(tmp_path / "nope" / "kanban.db"))])
    assert not (tmp_path / "nope").exists()
    environ = {"XDG_STATE_HOME": str(tmp_path / "state")}
    path = keyed("github-issues", "https://api.github.com", "example/repo", environ=environ)
    assert path.parent == state_dir(environ) == tmp_path / "state" / "kelpie" / "locks"
    assert "example" not in path.name  # names from config never reach the path
    held = acquire([ActLock("example/repo", path, create_folder=True)])
    try:
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    finally:
        held.release()


def test_state_dir_ignores_a_relative_xdg_state_home() -> None:
    assert state_dir({"XDG_STATE_HOME": "rel"}) == Path("~/.local/state/kelpie/locks").expanduser()


def test_adapters_lock_where_their_board_is(tmp_path: Path) -> None:
    claude = ClaudeCodeAdapter(spool=tmp_path / "spool.jsonl")
    assert [lk.path for lk in claude.act_locks()] == [tmp_path / "spool.jsonl.kelpie-act.lock"]
    assert ClaudeCodeAdapter(spool=tmp_path / "s", stop_dir=None).act_locks() == ()  # no stops
    assert ClaudeCodeAdapter(spool=None).act_locks() == ()
    openclaw = OpenClawAdapter(state_dir=tmp_path, spool=tmp_path / "oc.jsonl")
    assert [lk.path for lk in openclaw.act_locks()] == [tmp_path / "oc.jsonl.kelpie-act.lock"]
    assert OpenClawAdapter(state_dir=tmp_path, stop_dir=None).act_locks() == ()


def test_a_fresh_host_without_the_spool_folder_is_not_refused(tmp_path: Path) -> None:
    """The hook makes the spool's folder on its first line; serve may start first."""
    (lock,) = ClaudeCodeAdapter(spool=tmp_path / "kelpie" / "spool.jsonl").act_locks()
    held = acquire([lock])
    try:
        assert stat.S_IMODE((tmp_path / "kelpie").stat().st_mode) == 0o700
    finally:
        held.release()


def test_issue_boards_lock_per_repository_in_the_state_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = tmp_path / "kelpie.toml"
    path.write_text(
        '[connectors.github]\nrepos = ["example/one", "example/two"]\n[adapters.github_issues]\n'
    )
    monkeypatch.setenv("GITHUB_TOKEN", "x" * 40)
    locks = board_locks(build_adapters(load_settings(path)))
    assert [lk.board for lk in locks] == ["github-issues example/one", "github-issues example/two"]
    assert all(
        lk.create_folder and lk.path.parent == tmp_path / "state/kelpie/locks" for lk in locks
    )
    assert len({lk.path for lk in locks}) == 2


def _config(tmp_path: Path, extra: str) -> Path:
    path = tmp_path / "kelpie.toml"
    db = tmp_path / "kanban.db"
    path.write_text(f'[adapters.hermes]\ndb_path = "{db}"\n{extra}')
    return path


ACTING = '[engine]\ndry_run = false\n[engine.rules.loop_breaker]\nmode = "act"\n'


def test_hermes_boards_each_get_a_lock(tmp_path: Path) -> None:
    for name in ("qr", "ops"):
        (tmp_path / name).mkdir()
    path = tmp_path / "kelpie.toml"
    path.write_text(
        "[adapters.hermes]\n"
        + "".join(
            f'[[adapters.hermes.boards]]\nname = "{n}"\ndb_path = "{tmp_path / n / "kanban.db"}"\n'
            for n in ("qr", "ops")
        )
    )
    locks = board_locks(build_adapters(load_settings(path)))
    assert [lk.path for lk in locks] == [
        tmp_path / n / "kanban.db.kelpie-act.lock" for n in ("qr", "ops")
    ]


@pytest.mark.parametrize(
    ("extra", "locked"),
    [
        (ACTING, True),
        # a rule in act that only alerts acts on no board
        ('[engine]\ndry_run = false\n[engine.rules.ping_pong]\nmode = "act"\n', False),
        ('[engine]\ndry_run = false\n[engine.rules.loop_breaker]\nmode = "suggest"\n', False),
        ('[engine]\ndry_run = true\n[engine.rules.loop_breaker]\nmode = "act"\n', False),
    ],
)
def test_serve_locks_only_when_a_rule_acts(tmp_path: Path, extra: str, locked: bool) -> None:
    held = serve_act_locks(load_settings(_config(tmp_path, extra)))
    try:
        assert bool(held.boards) is locked
        assert (tmp_path / "kanban.db.kelpie-act.lock").exists() is locked
    finally:
        held.release()


def test_a_second_acting_serve_refuses_to_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path, ACTING)
    built: list[object] = []
    monkeypatch.setattr("kelpie.cli.build_app", lambda s: built.append(s))
    held = serve_act_locks(load_settings(path))  # the first instance
    try:
        assert main(["--config", str(path), "serve"]) == 2
    finally:
        held.release()
    err = capsys.readouterr().err
    assert "kelpie: cannot start: another Kelpie acts on board" in err
    assert "observe or suggest" in err
    assert built == []  # refused before anything was built or bound


def test_an_observing_serve_starts_next_to_an_acting_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    acting = serve_act_locks(load_settings(_config(tmp_path, ACTING)))
    try:
        observing = load_settings(_config(tmp_path, ""))
        assert serve_act_locks(observing).boards == ()
    finally:
        acting.release()
