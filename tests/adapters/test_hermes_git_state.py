"""Git state and run summaries of the Hermes adapter, against real temporary git repos."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.git_state import GitState, read_git_state
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


def test_dir_workspace_is_read_too(board: Board, repo: Path) -> None:
    live_task(board, repo, kind="dir")
    board.run("t_1", "dev", ago(hours=1))
    assert only(board).last_commit is not None


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
