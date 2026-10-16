"""Git state and run summaries of the Hermes adapter, against real temporary git repos."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes import git_state as gs
from kelpie.adapters.hermes.git_state import GitState, Problem, read_git_state
from kelpie.core.model import Session
from tests.adapters.kanban import Board

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
COMMITTED_AT = "2026-03-02T10:00:00+02:00"  # 08:00 UTC


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def git(repo: Path, *args: str, date: str | None = None) -> None:
    env = {"GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date} if date else {}

    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **env},
    )


@pytest.fixture(autouse=True)
def fresh_git_state_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test probes git itself and sees every warning once."""
    monkeypatch.setattr(gs, "_ATTR_SOURCE", None)
    monkeypatch.setattr(gs, "_WARNED", set())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on ``main`` with one commit, ``a.txt``, clean."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "agent@example.invalid")
    git(path, "config", "user.name", "Agent")
    git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("one\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "first", date=COMMITTED_AT)
    return path


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


def live_task(
    board: Board,
    path: Path | str | None,
    *,
    kind: str = "worktree",
    branch: str | None = None,
    status: str = "running",
    task_id: str = "t_1",
) -> None:
    board.task(task_id, status, ago(days=1), assignee="dev")
    board.update(
        task_id,
        workspace_kind=kind,
        workspace_path=str(path) if path is not None else None,
        branch_name=branch,
    )


def snapshot_sessions(board: Board, **kw: object) -> dict[str, Session]:
    adapter = HermesAdapter(db_path=board.path, spool=None, **kw)  # type: ignore[arg-type]
    return {s.id: s for s in adapter.snapshot(NOW).sessions}


def only(board: Board, **kw: object) -> Session:
    (s,) = snapshot_sessions(board, **kw).values()
    return s


# --- summary ----------------------------------------------------------------------------


def test_summary_is_passed_through(board: Board) -> None:
    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.run("t_1", "dev", ago(hours=3), status="done", ended=ago(hours=2), summary="  did X\n")
    board.run("t_1", "dev", ago(hours=1), status="done", ended=ago(minutes=30), summary="")
    board.run("t_1", "dev", ago(minutes=20), status="done", ended=ago(minutes=10))
    first, second, third = snapshot_sessions(board).values()
    assert first.summary == "did X"
    assert second.summary is None and third.summary is None


def test_summary_survives_without_git(board: Board, repo: Path) -> None:
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1), summary="halfway")
    assert only(board, git_state=False).summary == "halfway"


def test_older_schema_without_summary_or_workspace_columns(board: Board) -> None:
    board.task("t_1", "running", ago(days=1), assignee="dev")
    board.run("t_1", "dev", ago(hours=1), summary="ignored")
    for table, column in (
        ("task_runs", "summary"),
        ("tasks", "workspace_kind"),
        ("tasks", "workspace_path"),
        ("tasks", "branch_name"),
    ):
        board.conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    board.conn.commit()
    s = only(board)
    assert s.summary is None and s.last_commit is None and s.changed_files is None


# --- git state --------------------------------------------------------------------------


def test_clean_worktree(board: Board, repo: Path) -> None:
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.last_commit is not None and len(s.last_commit) >= 7
    assert s.last_commit_at == datetime(2026, 3, 2, 8, tzinfo=UTC)
    assert s.last_commit_at is not None and s.last_commit_at.utcoffset() is not None
    assert s.branch == "main"
    assert s.changed_files == 0 and s.dirty_worktree is False


def test_last_commit_is_the_short_sha_of_head(board: Board, repo: Path) -> None:
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    full = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    sha = only(board).last_commit
    assert sha is not None and full.startswith(sha) and sha != full


def test_dirty_worktree_counts_modified_added_and_untracked(board: Board, repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n")  # modified
    (repo / "b.txt").write_text("new\n")  # untracked
    (repo / "c.txt").write_text("staged\n")
    git(repo, "add", "c.txt")  # staged
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.changed_files == 3 and s.dirty_worktree is True


def test_status_does_not_touch_the_index(board: Board, repo: Path) -> None:
    (repo / "a.txt").write_text("changed\n")
    index = repo / ".git" / "index"
    before = (index.stat().st_mtime_ns, index.read_bytes())
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    only(board)
    assert (index.stat().st_mtime_ns, index.read_bytes()) == before


def test_branch_from_task_wins_over_git(board: Board, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "other")
    live_task(board, repo, branch="hermes/t_1")
    board.run("t_1", "dev", ago(hours=1))
    assert only(board).branch == "hermes/t_1"


def test_branch_from_git_when_the_task_has_none(board: Board, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "feature/x")
    live_task(board, repo, branch="  ")
    board.run("t_1", "dev", ago(hours=1))
    assert only(board).branch == "feature/x"


def test_detached_head_has_no_branch(board: Board, repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.branch is None and s.last_commit is not None


def test_dir_workspace_is_ignored(board: Board, repo: Path) -> None:
    live_task(board, repo, kind="dir", branch="hermes/t_1")  # may be a shared or human checkout
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.last_commit is None and s.branch is None and s.changed_files is None


def test_scratch_workspace_is_ignored(board: Board, repo: Path) -> None:
    live_task(board, repo, kind="scratch", branch="hermes/t_1")
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.last_commit is None and s.branch is None and s.changed_files is None


@pytest.mark.parametrize("path", [None, "", "relative/dir", "MISSING"])
def test_missing_or_unusable_path(board: Board, tmp_path: Path, path: str | None) -> None:
    live_task(board, tmp_path / "gone" if path == "MISSING" else path)
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.last_commit is None and s.changed_files is None and s.dirty_worktree is False


def test_path_that_is_a_file_is_ignored(board: Board, repo: Path) -> None:
    live_task(board, repo / "a.txt")
    board.run("t_1", "dev", ago(hours=1))
    assert only(board).last_commit is None


@pytest.mark.parametrize("status", ["done", "archived"])
def test_finished_tasks_are_ignored(board: Board, repo: Path, status: str) -> None:
    live_task(board, repo, status=status)
    board.update("t_1", completed_at=ago(hours=1))
    board.run("t_1", "dev", ago(hours=3), status="done", ended=ago(hours=1), outcome="completed")
    sessions = snapshot_sessions(board)
    assert all(s.last_commit is None and s.changed_files is None for s in sessions.values())


@pytest.mark.parametrize("status", ["ready", "blocked", "review", "todo"])
def test_other_live_statuses_are_read(board: Board, repo: Path, status: str) -> None:
    live_task(board, repo, status=status)
    board.run("t_1", "dev", ago(hours=3), status="done", ended=ago(hours=1))
    assert only(board).last_commit is not None


def test_only_the_latest_run_of_a_task_gets_git_state(board: Board, repo: Path) -> None:
    (repo / "b.txt").write_text("new\n")
    live_task(board, repo)
    first = board.run("t_1", "dev", ago(hours=5), status="done", ended=ago(hours=4))
    second = board.run("t_1", "dev", ago(hours=3), status="done", ended=ago(hours=2))
    third = board.run("t_1", "dev", ago(hours=1), summary="note")
    sessions = snapshot_sessions(board)
    for run_id in (first, second):
        old = sessions[f"hermes-run-{run_id}"]
        assert old.last_commit is None and old.changed_files is None
        assert old.branch is None and old.dirty_worktree is False
    latest = sessions[f"hermes-run-{third}"]
    assert latest.last_commit is not None and latest.changed_files == 1
    assert latest.dirty_worktree is True and latest.summary == "note"


def test_each_task_gets_its_own_latest_run(board: Board, repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q", "-b", "side")
    live_task(board, repo, task_id="t_1")
    live_task(board, other, task_id="t_2")
    r1 = board.run("t_1", "dev", ago(hours=2))
    r2 = board.run("t_2", "dev", ago(hours=1))
    sessions = snapshot_sessions(board)
    assert sessions[f"hermes-run-{r1}"].branch == "main"
    # a repository without commits: branch and status are known, there is no commit
    s2 = sessions[f"hermes-run-{r2}"]
    assert s2.last_commit is None and s2.last_commit_at is None and s2.changed_files == 0


def test_not_a_git_directory_leaves_fields_empty(board: Board, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    live_task(board, plain, branch="hermes/t_1")
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.last_commit is None and s.last_commit_at is None
    assert s.changed_files is None and s.dirty_worktree is False
    assert s.branch == "hermes/t_1"  # the task's own knowledge stays


def test_git_state_can_be_disabled(board: Board, repo: Path) -> None:
    (repo / "b.txt").write_text("new\n")
    live_task(board, repo, branch="hermes/t_1")
    board.run("t_1", "dev", ago(hours=1))
    s = only(board, git_state=False)
    assert s.last_commit is None and s.branch is None and s.changed_files is None
    assert s.dirty_worktree is False


def test_git_timeout_leaves_fields_empty(
    board: Board, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slow = tmp_path / "bin"
    slow.mkdir()
    fake = slow / "git"
    fake.write_text("#!/bin/sh\nsleep 5\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{slow}:/usr/bin:/bin")
    live_task(board, repo, branch="hermes/t_1")
    board.run("t_1", "dev", ago(hours=1))
    started = datetime.now(UTC)
    s = only(board, git_timeout=timedelta(milliseconds=200))
    assert datetime.now(UTC) - started < timedelta(seconds=3)  # one timeout, not four
    assert s.last_commit is None and s.changed_files is None and s.dirty_worktree is False
    assert s.branch == "hermes/t_1"


def test_git_not_installed_leaves_fields_empty(
    board: Board, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.last_commit is None and s.branch is None and s.changed_files is None


# --- read_git_state ---------------------------------------------------------------------


def test_read_git_state_directly(repo: Path) -> None:
    (repo / "b.txt").write_text("new\n")
    state = read_git_state(repo, 5.0)
    assert state.branch == "main" and state.changed_files == 1 and state.dirty is True
    assert state.last_commit_at == datetime(2026, 3, 2, 8, tzinfo=UTC)


def test_read_git_state_outside_a_repository(tmp_path: Path) -> None:
    assert read_git_state(tmp_path, 5.0) == GitState()
    assert read_git_state(tmp_path / "gone", 5.0) == GitState()


def test_clean_state_is_not_dirty() -> None:
    assert GitState(changed_files=0).dirty is False
    assert GitState(changed_files=None).dirty is False


def test_unborn_branch_comes_from_status(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "start")
    (fresh / "x.txt").write_text("x\n")
    state = read_git_state(fresh, 5.0)
    assert state == GitState(
        branch="start", changed_files=1, paths=("x.txt",), repo=str((fresh / ".git").resolve())
    )


def test_parent_repository_is_not_searched(repo: Path) -> None:
    sub = repo / "sub"
    sub.mkdir()
    assert read_git_state(sub, 5.0, branch="b") == GitState(branch="b")


def test_old_git_without_attr_source(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert gs.probe_git(5.0) is None
    monkeypatch.setattr(gs, "_ATTR_SOURCE", False)
    (repo / "b.txt").write_text("new\n")
    state = read_git_state(repo, 5.0)
    assert state.branch == "main" and state.changed_files == 1 and state.problem is None


# --- workspaces are agent-controlled: nothing they configure runs ------------------------


def touch_script(tmp_path: Path, marker: Path, name: str = "evil.sh") -> Path:
    """A program that leaves ``marker`` behind if anyone runs it."""
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    script.chmod(0o755)
    return script


def make_stat_dirty(repo: Path, seconds: int) -> None:
    """Same content, new mtime: status must hash ``a.txt`` again, through its filters."""
    os.utime(repo / "a.txt", (seconds, seconds))


def vulnerable(repo: Path, *args: str) -> None:
    """Run git the naive way (what Kelpie must not do), without touching the index."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        check=False,
    )


@pytest.mark.parametrize("attr_source", [True, False])
@pytest.mark.parametrize("where", [".gitattributes", ".git/info/attributes"])
@pytest.mark.parametrize("key", ["clean", "process"])
def test_filter_driver_of_the_workspace_is_not_run(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attr_source: bool,
    where: str,
    key: str,
) -> None:
    marker = tmp_path / "marker"
    git(repo, "config", f"filter.evil.{key}", str(touch_script(tmp_path, marker)))
    git(repo, "config", "filter.evil.required", "true")
    (repo / where).parent.mkdir(exist_ok=True)
    (repo / where).write_text("*.txt filter=evil\n")
    make_stat_dirty(repo, 1_600_000_000)
    vulnerable(repo, "status", "--porcelain")
    assert marker.exists(), "the attack must work against plain git"
    marker.unlink()
    make_stat_dirty(repo, 1_600_000_100)

    assert gs.probe_git(5.0) is None
    monkeypatch.setattr(gs, "_ATTR_SOURCE", attr_source and gs._ATTR_SOURCE)
    state = read_git_state(repo, 5.0)
    assert not marker.exists()
    assert state.problem is None and state.last_commit is not None
    assert state.changed_files == (1 if where == ".gitattributes" else 0)


def test_fsmonitor_of_the_workspace_is_not_run(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(repo, "config", "core.fsmonitor", str(touch_script(tmp_path, marker)))
    vulnerable(repo, "status", "--porcelain")
    assert marker.exists()
    marker.unlink()
    assert read_git_state(repo, 5.0).changed_files == 0
    assert not marker.exists()


def test_filter_driver_that_cannot_be_overridden_stops_the_read(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(repo, "config", "filter.a=b.clean", str(touch_script(tmp_path, marker)))
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=a=b\n")
    make_stat_dirty(repo, 1_600_000_000)
    state = read_git_state(repo, 5.0, branch="hermes/t_1")
    assert replace(state, repo=None) == GitState(branch="hermes/t_1", problem=Problem.UNSAFE)
    assert not marker.exists()


def test_partial_clone_does_not_fetch_through_the_workspace_transport(
    repo: Path, tmp_path: Path
) -> None:
    """A promisor remote with a missing HEAD would make status fetch via upload-pack."""
    marker = tmp_path / "marker"
    git(repo, "config", "core.repositoryformatversion", "1")
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.url", str(tmp_path))
    upload_pack = tmp_path / "up.sh"  # leaves a marker and fails, so plain git does not hang
    upload_pack.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    upload_pack.chmod(0o755)
    git(repo, "config", "remote.origin.uploadpack", str(upload_pack))
    (repo / ".git" / "refs" / "heads" / "main").write_text("1" * 40 + "\n")
    subprocess.run(
        ["git", "-C", str(repo), "status"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert marker.exists(), "the attack must work against plain git"
    marker.unlink()
    read_git_state(repo, 5.0)
    assert not marker.exists()


def test_empty_dot_git_does_not_fall_back_to_a_parent_repository(repo: Path) -> None:
    inner = repo / "wt"
    (inner / ".git").mkdir(parents=True)
    state = read_git_state(inner, 5.0)
    assert state.last_commit is None and state.changed_files is None


def test_sha256_repository(tmp_path: Path) -> None:
    path = tmp_path / "sha256"
    path.mkdir()
    if subprocess.run(
        ["git", "-C", str(path), "init", "-q", "--object-format=sha256"], capture_output=True
    ).returncode:
        pytest.skip("git without SHA-256 support")
    git(path, "config", "user.email", "agent@example.invalid")
    git(path, "config", "user.name", "Agent")
    (path / "a.txt").write_text("one\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "first", date=COMMITTED_AT)
    (path / "b.txt").write_text("two\n")
    state = read_git_state(path, 5.0)
    assert state.problem is None and state.changed_files == 1
    assert state.last_commit is not None and state.last_commit_at is not None


def test_signed_head_does_not_run_gpg(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    git(repo, "config", "gpg.program", str(touch_script(tmp_path, marker, "gpg.sh")))
    git(repo, "config", "log.showSignature", "true")
    tree = subprocess.run(
        ["git", "-C", str(repo), "write-tree"], capture_output=True, text=True, check=True
    ).stdout.strip()
    raw = (
        f"tree {tree}\n"
        "author Agent <agent@example.invalid> 1772445600 +0200\n"
        "committer Agent <agent@example.invalid> 1772449200 +0200\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n -----END PGP SIGNATURE-----\n"
        "\nsigned\n"
    )
    oid = subprocess.run(
        ["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=raw,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    git(repo, "update-ref", "refs/heads/main", oid)
    vulnerable(repo, "log", "-1", "--format=%cI")
    assert marker.exists(), "the attack must work against plain git log"
    marker.unlink()

    state = read_git_state(repo, 5.0)
    assert not marker.exists()
    assert state.last_commit == oid[:7]
    assert state.last_commit_at == datetime(2026, 3, 2, 11, tzinfo=UTC)  # 1772449200


def fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Put a ``git`` script first on PATH; it logs its arguments to the returned file."""
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    script = bin_dir / "git"
    script.write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> '{calls}'\n"
        'if [ "$1" = version ]; then echo "git version 2.43.0"; exit 0; fi\n' + body
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    return calls


def test_git_env_is_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KELPIE_TEST_SECRET", "s3cret")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("HOME", "/home/kelpie")
    assert gs.git_env() == {
        "PATH": os.environ["PATH"],
        "HOME": "/home/kelpie",
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "none",
    }
    assert gs.git_env(Path("/w/wt"))["GIT_CEILING_DIRECTORIES"] == "/w"
    assert gs.git_env(Path("/w/wt"))["GIT_WORK_TREE"] == "/w/wt"


def test_secrets_do_not_reach_git(
    board: Board, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dump = tmp_path / "env.dump"
    fake_git(tmp_path, monkeypatch, f"env >> '{dump}'\nexit 0\n")
    for name, value in (
        ("KELPIE_TEST_SECRET", "s3cret"),
        ("GITHUB_TOKEN", "s3cret"),
        ("GIT_DIR", "/elsewhere"),
        ("GIT_WORK_TREE", "/elsewhere"),
        ("GIT_INDEX_FILE", "/elsewhere/index"),
    ):
        monkeypatch.setenv(name, value)
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    only(board)
    seen = dump.read_text()
    assert "LC_ALL=C" in seen and "GIT_CONFIG_NOSYSTEM=1" in seen
    for leaked in ("s3cret", "/elsewhere", "GIT_DIR=", "GIT_INDEX_FILE="):
        assert leaked not in seen
    # GIT_WORK_TREE is Kelpie's own: the workspace itself, never the inherited value.
    assert f"GIT_WORK_TREE={repo}" in seen


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def test_timeout_kills_the_whole_process_group(repo: Path, tmp_path: Path) -> None:
    children = tmp_path / "children"
    with pytest.MonkeyPatch.context() as mp:
        assert gs.probe_git(5.0) is None
        fake_git(tmp_path, mp, f"sleep 30 &\necho $! >> '{children}'\nwait\n")
        # Generous: under load the shell may need a while to start and record its child.
        state = read_git_state(repo, 1.5)
    assert state.problem is Problem.TIMEOUT
    pids = [int(x) for x in children.read_text().split()]
    assert pids
    deadline = time.monotonic() + 3.0  # the kernel needs a moment to reap the killed group
    while any(alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not any(alive(p) for p in pids)


def many_workspaces(board: Board, tmp_path: Path, count: int) -> list[Path]:
    paths = []
    for i in range(count):
        ws = tmp_path / f"ws{i:02d}"
        (ws / ".git").mkdir(parents=True)
        live_task(board, ws, task_id=f"t_{i:02d}", branch=f"hermes/t_{i:02d}")
        board.run(f"t_{i:02d}", "dev", ago(hours=1))
        paths.append(ws)
    return paths


def called_paths(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [line.split()[1] for line in calls.read_text().splitlines() if line.startswith("-C ")]


def test_slow_git_is_bounded_per_snapshot_and_backs_off(
    board: Board, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = fake_git(tmp_path, monkeypatch, "sleep 30\n")
    paths = many_workspaces(board, tmp_path, 12)
    adapter = HermesAdapter(db_path=board.path, spool=None, git_timeout=timedelta(seconds=0.5))

    started = time.monotonic()
    sessions = adapter.snapshot(NOW).sessions
    elapsed = time.monotonic() - started
    assert elapsed < 3.0  # budget 2 x 0.5s, not 12 workspaces x 3 calls x 0.5s
    assert len(called_paths(calls)) <= 8  # at most two rounds of 4 workers
    assert all(s.last_commit is None and s.changed_files is None for s in sessions)
    assert {s.branch for s in sessions} == {f"hermes/t_{i:02d}" for i in range(12)}
    backed_off = set(adapter._git_backoff)
    assert 1 <= len(backed_off) <= 4 and backed_off <= set(paths)
    assert all(until == NOW + timedelta(minutes=5) for until in adapter._git_backoff.values())

    calls.unlink()
    adapter.snapshot(NOW + timedelta(minutes=1))
    assert not backed_off & {Path(p) for p in called_paths(calls)}

    calls.unlink()
    adapter._git_backoff = {p: NOW + timedelta(minutes=5) for p in backed_off}
    adapter.snapshot(NOW + timedelta(minutes=6))
    assert backed_off <= {Path(p) for p in called_paths(calls)}  # back after the backoff

    timeouts = [r for r in caplog.records if "timed out" in r.getMessage()]
    warned = [r for r in timeouts if r.levelname == "WARNING"]
    assert len({r.getMessage() for r in warned}) == len(warned)  # once per workspace


def test_shared_workspace_gets_no_git_state(
    board: Board, repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    link = tmp_path / "link"
    link.symlink_to(repo)
    live_task(board, repo, task_id="t_1", branch="hermes/t_1")
    live_task(board, link, task_id="t_2")
    live_task(board, tmp_path / "repo" / ".." / "repo", task_id="t_3")
    for task_id in ("t_1", "t_2", "t_3"):
        board.run(task_id, "dev", ago(hours=1))
    adapter = HermesAdapter(db_path=board.path, spool=None)
    for _ in range(2):
        sessions = {s.task_id: s for s in adapter.snapshot(NOW).sessions}
        assert all(s.last_commit is None and s.changed_files is None for s in sessions.values())
    assert sessions["t_1"].branch == "hermes/t_1"
    warned = [r for r in caplog.records if r.levelname == "WARNING" and "used by" in r.message]
    assert len(warned) == 1 and "t_1, t_2, t_3" in warned[0].getMessage()


def test_shared_workspace_of_a_finished_task_does_not_count(board: Board, repo: Path) -> None:
    live_task(board, repo, task_id="t_1")
    live_task(board, repo, task_id="t_2", status="done")
    board.update("t_2", completed_at=ago(hours=2))
    board.run("t_1", "dev", ago(hours=1))
    board.run("t_2", "dev", ago(hours=3), status="done", ended=ago(hours=2), outcome="completed")
    sessions = {s.task_id: s for s in snapshot_sessions(board).values()}
    assert sessions["t_1"].last_commit is not None and sessions["t_2"].last_commit is None


def test_git_not_found_warns_once(
    board: Board,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    adapter = HermesAdapter(db_path=board.path, spool=None)
    adapter.snapshot(NOW)
    adapter.snapshot(NOW)
    warned = [r for r in caplog.records if r.levelname == "WARNING"]
    assert [r.getMessage() for r in warned] == [
        "hermes: git not found on PATH; no git state is read"
    ]


def test_failed_git_is_debug_only(
    board: Board,
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dubious ownership and the like: no git state, only debug lines that say why."""
    caplog.set_level("DEBUG", logger="kelpie.adapters.hermes")
    fake_git(tmp_path, monkeypatch, "echo 'fatal: detected dubious ownership' >&2\nexit 128\n")
    live_task(board, repo)
    board.run("t_1", "dev", ago(hours=1))
    s = only(board)
    assert s.last_commit is None and s.changed_files is None
    assert not [r for r in caplog.records if r.levelno >= 30]
    assert any("dubious ownership" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("output", "supported"),
    [
        ("git version 2.43.0", True),
        ("git version 2.40.0.windows.1", True),
        ("git version 2.39.3 (Apple Git-146)", False),
        ("something else", False),
    ],
)
def test_probe_reads_the_git_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, supported: bool
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(f"#!/bin/sh\necho '{output}'\n")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert gs.probe_git(5.0) is None
    assert gs._ATTR_SOURCE is supported
    (bin_dir / "git").unlink()
    assert gs.probe_git(5.0) is None  # cached: git is not asked again


def test_probe_timeout_is_not_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text("#!/bin/sh\nsleep 30\n")
    (bin_dir / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    assert gs.probe_git(0.2) is Problem.TIMEOUT
    assert gs._ATTR_SOURCE is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "tree t\ncommitter A <a@b> 1772449200 +0200\n\nmsg\n",
            datetime(2026, 3, 2, 11, tzinfo=UTC),
        ),
        ("tree t\ncommitter A <a@b> soon +0200\n", None),
        ("tree t\ncommitter nobody\n", None),
        ("tree t\n\ncommitter A <a@b> 1772449200 +0200\n", None),  # in the message, not a header
        ("", None),
    ],
)
def test_committer_date_parsing(raw: str, expected: datetime | None) -> None:
    assert gs._committed_at(raw) == expected


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        ("", set()),
        ("filter.lfs.clean\0filter.lfs.process\0filter.My.Driver.smudge\0", {"lfs", "My.Driver"}),
        ("filter.a=b.clean\0", None),
        ("filter.clean\0", None),
    ],
)
def test_filter_driver_names(listing: str, expected: set[str] | None) -> None:
    assert gs._filter_drivers(listing) == expected


# --- changed paths (Task.files) -----------------------------------------------------------


def branch_off(repo: Path, name: str = "task") -> None:
    git(repo, "checkout", "-q", "-b", name)


def commit(repo: Path, *files: str, message: str = "change") -> None:
    for f in files:
        target = repo / f
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{f} {message}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)


def test_paths_are_uncommitted_entries_with_renames_spaces_and_untracked(repo: Path) -> None:
    commit(repo, "old name.txt", "keep.txt")
    git(repo, "mv", "old name.txt", "new name.txt")
    (repo / "keep.txt").write_text("edited\n")
    (repo / "dir").mkdir()
    (repo / "dir" / "untracked file.py").write_text("x\n")
    state = read_git_state(repo, 5.0)
    # A new untracked directory is one entry, as status lists it (untracked-files=normal).
    assert state.paths == ("dir/", "keep.txt", "new name.txt", "old name.txt")
    assert state.changed_files == 3 and not state.paths_truncated


def test_clean_worktree_without_base_refs_has_no_paths(repo: Path) -> None:
    assert read_git_state(repo, 5.0).paths == ()


def test_committed_branch_changes_count_against_the_base(repo: Path) -> None:
    branch_off(repo)
    commit(repo, "src/routes.py", message="one")
    commit(repo, "src/types.py", message="two")
    (repo / "notes.md").write_text("wip\n")
    state = read_git_state(repo, 5.0, base_refs=("main",))
    assert state.paths == ("notes.md", "src/routes.py", "src/types.py")
    assert read_git_state(repo, 5.0).paths == ("notes.md",)  # without base refs


def test_newest_merge_base_wins_over_a_stale_base(repo: Path) -> None:
    git(repo, "branch", "stale")  # an old copy of main, never updated
    commit(repo, "landed.py", message="merged elsewhere")  # main moves on
    branch_off(repo)  # the task starts from the new main
    commit(repo, "mine.py")
    state = read_git_state(repo, 5.0, base_refs=("stale", "main"))
    assert state.paths == ("mine.py",)  # landed.py is main's, not the task's


def test_unknown_and_invalid_base_refs_are_skipped(repo: Path) -> None:
    branch_off(repo)
    commit(repo, "mine.py")
    for refs in [("nope", "main"), ("-x", "main"), ("a..b", "main")]:
        assert read_git_state(repo, 5.0, base_refs=refs).paths == ("mine.py",)
    assert read_git_state(repo, 5.0, base_refs=("nope",)).paths == ()


@pytest.mark.parametrize(
    ("ref", "ok"),
    [
        ("main", True),
        ("origin/main", True),
        ("release/2.x", True),
        ("-main", False),
        ("a..b", False),
        ("main.lock", False),
        ("main/", False),
        ("ma in", False),
        ("main^", False),
        ("", False),
    ],
)
def test_valid_base_ref(ref: str, ok: bool) -> None:
    assert gs.valid_base_ref(ref) is ok


def test_paths_are_capped(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gs, "MAX_PATHS", 2)
    for name in ("c.txt", "b.txt", "d.txt"):
        (repo / name).write_text("x\n")
    state = read_git_state(repo, 5.0)
    assert state.paths == ("b.txt", "c.txt") and state.paths_truncated
    assert state.changed_files == 3


def test_adapter_puts_paths_on_the_task(board: Board, repo: Path) -> None:
    branch_off(repo)
    commit(repo, "src/routes.py")
    (repo / "wip.py").write_text("x\n")
    live_task(board, repo, branch="task")
    board.run("t_1", "dev", ago(hours=1))
    board.task("t_2", "ready", ago(days=1), assignee="dev")  # no workspace: no files
    adapter = HermesAdapter(db_path=board.path, spool=None, base_refs=("main",))
    tasks = {t.id: t for t in adapter.snapshot(NOW).tasks}
    assert tasks["t_1"].files == ("src/routes.py", "wip.py")
    assert tasks["t_2"].files == ()
    no_git = HermesAdapter(db_path=board.path, spool=None, git_state=False)
    assert {t.id: t.files for t in no_git.snapshot(NOW).tasks}["t_1"] == ()


def test_adapter_rejects_invalid_base_refs(board: Board) -> None:
    with pytest.raises(ValueError, match="base refs"):
        HermesAdapter(db_path=board.path, spool=None, base_refs=("--upload-pack=x",))


def test_core_worktree_cannot_point_git_elsewhere(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "private"
    other.mkdir()
    (other / "private-name.txt").write_text("secret\n")
    git(repo, "config", "core.worktree", str(other))
    (repo / "mine.txt").write_text("x\n")
    state = read_git_state(repo, 5.0)
    assert state.paths is not None and "private-name.txt" not in state.paths
    assert "mine.txt" in state.paths


def test_gitfile_must_point_back_at_the_workspace(repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "task", str(linked))
    (linked / "wip.py").write_text("x\n")
    state = read_git_state(linked, 5.0)
    assert state.problem is None and state.paths == ("wip.py",)
    assert state.repo == str((repo / ".git").resolve())  # same repo as the main checkout
    assert read_git_state(repo, 5.0).repo == state.repo
    # A .git file naming another repository's git directory is not trusted.
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    (stranger / ".git").write_text(f"gitdir: {repo / '.git'}\n")
    assert read_git_state(stranger, 5.0).problem is Problem.UNSAFE
    linked_gitdir = (linked / ".git").read_text().split(":", 1)[1].strip()
    hijack = tmp_path / "hijack"
    hijack.mkdir()
    (hijack / ".git").write_text(f"gitdir: {linked_gitdir}\n")  # points at linked's, not back
    assert read_git_state(hijack, 5.0).problem is Problem.UNSAFE


def test_symlinked_dot_git_is_not_followed(repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / ".git").symlink_to(repo / ".git")
    assert read_git_state(other, 5.0).problem is Problem.UNSAFE


def test_unmerged_detached_and_newline_paths(repo: Path) -> None:
    branch_off(repo, "side")
    commit(repo, "a.txt", message="side")
    git(repo, "checkout", "-q", "main")
    commit(repo, "a.txt", message="main")
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "side"], capture_output=True)
    (repo / "odd\nname.txt").write_text("x\n")
    state = read_git_state(repo, 5.0)
    assert state.paths == ("a.txt", "odd?name.txt")  # unmerged entry; newline shown as ?
    git(repo, "merge", "--abort")
    git(repo, "checkout", "-q", "--detach")
    detached = read_git_state(repo, 5.0, base_refs=("main",))
    assert detached.branch is None and detached.paths == ("odd?name.txt",)


def test_files_step_failure_keeps_the_rest(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    branch_off(repo)
    commit(repo, "mine.py")
    (repo / "wip.py").write_text("x\n")
    real = gs._run_capped

    def slow_diff(cmd: list[str], *args: object, **kw: object) -> object:
        if "diff-tree" in cmd:
            raise gs._Stop(Problem.TIMEOUT)
        return real(cmd, *args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(gs, "_run_capped", slow_diff)
    state = read_git_state(repo, 5.0, base_refs=("main",))
    assert state.problem is None and state.last_commit is not None
    assert state.changed_files == 1 and state.paths == ("wip.py",) and state.paths_partial


def test_no_merge_base_or_unknown_refs_is_partial(repo: Path, tmp_path: Path) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    git(unrelated, "init", "-q", "-b", "other")
    git(unrelated, "config", "user.email", "a@example.invalid")
    git(unrelated, "config", "user.name", "A")
    git(unrelated, "config", "commit.gpgsign", "false")
    commit(unrelated, "x.py")
    git(repo, "fetch", "-q", str(unrelated), "other:other")  # no common history with main
    git(repo, "checkout", "-q", "other")
    state = read_git_state(repo, 5.0, base_refs=("main",))
    assert state.paths == () and state.paths_partial
    assert read_git_state(repo, 5.0, base_refs=("nope",)).paths_partial


def test_huge_output_is_cut_not_read_whole(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gs, "MAX_STATUS_BYTES", 200)
    for i in range(50):
        (repo / f"untracked-file-number-{i:03d}.txt").write_text("x\n")
    state = read_git_state(repo, 5.0)
    assert state.paths_truncated and state.paths is not None
    assert 0 < len(state.paths) < 50
    assert all(p.startswith("untracked-file-number-") and p.endswith(".txt") for p in state.paths)


def test_capped_diff_marks_truncation(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    branch_off(repo)
    commit(repo, *(f"src/module_{i:03d}.py" for i in range(40)))
    monkeypatch.setattr(gs, "MAX_DIFF_BYTES", 120)
    state = read_git_state(repo, 5.0, base_refs=("main",))
    assert state.paths_truncated and state.paths is not None
    assert 0 < len(state.paths) < 40 and all(p.endswith(".py") for p in state.paths)


def test_open_task_without_a_recent_run_still_gets_files(board: Board, repo: Path) -> None:
    branch_off(repo)
    commit(repo, "src/routes.py")
    live_task(board, repo, branch="task", status="review")
    board.run("t_1", "dev", ago(days=3), status="done", ended=ago(days=2))  # outside lookback
    adapter = HermesAdapter(db_path=board.path, spool=None, base_refs=("main",))
    snap = adapter.snapshot(NOW)
    (task,) = snap.tasks
    assert task.files == ("src/routes.py",) and task.repo == str((repo / ".git").resolve())
    assert snap.sessions == ()
