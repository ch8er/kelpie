"""Local WIP commits of the Hermes adapter (#399, option 1), against real temporary repos."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.hermes import HermesAdapter, wip
from kelpie.adapters.hermes import git_state as gs
from kelpie.adapters.hermes.adapter import HermesError
from kelpie.core.actions import Action, ActionKind
from kelpie.core.executors import PlatformExecutor
from kelpie.core.ports import NothingToDo, UnsupportedAction, declared_actions
from tests.adapters.kanban import Board

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
MESSAGE = "WIP: Synthetic task [skip ci]\n\nSession hermes-run-1 of dev ended."


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(autouse=True)
def fresh_git_state_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gs, "_ATTR_SOURCE", None)
    monkeypatch.setattr(gs, "_WARNED", set())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repository on ``feature/t-1`` with one commit, ``a.txt``, then left dirty."""
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", "feature/t-1")
    git(path, "config", "user.email", "agent@example.invalid")
    git(path, "config", "user.name", "Agent")
    git(path, "config", "commit.gpgsign", "false")
    (path / "a.txt").write_text("one\n")
    (path / "b.txt").write_text("gone\n")
    git(path, "add", "a.txt", "b.txt")
    git(path, "commit", "-q", "-m", "first")
    (path / "a.txt").write_text("two\n")
    (path / "b.txt").unlink()
    (path / "new.txt").write_text("new\n")
    return path


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


def ended_task(board: Board, path: Path, *, task_id: str = "t_1", status: str = "ready") -> None:
    board.task(task_id, status, ago(days=1), assignee="dev")
    board.update(
        task_id, workspace_kind="worktree", workspace_path=str(path), branch_name="feature/t-1"
    )
    board.run(task_id, "dev", ago(hours=2), status="crashed", ended=ago(hours=1))


def adapter(board: Board, **kw: Any) -> HermesAdapter:
    return HermesAdapter(db_path=board.path, spool=None, **kw)


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def commit(board: Board, **kw: Any) -> str:
    return adapter(board).wip_commit("t_1", MESSAGE, branch="feature/t-1", **kw)


def hook(repo: Path, name: str, marker: Path) -> None:
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    script = hooks / name
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)


# --- the commit -------------------------------------------------------------------------


def test_declared() -> None:
    kinds = declared_actions(HermesAdapter())
    assert kinds is not None and ActionKind.WIP_COMMIT in kinds


def test_commits_every_change_locally_with_a_fixed_author(
    board: Board, repo: Path, tmp_path: Path
) -> None:
    ended_task(board, repo)
    before = head(repo)
    git(repo, "remote", "add", "origin", str(tmp_path / "nowhere.git"))
    said = commit(board)
    after = head(repo)
    assert after != before
    assert said == f"local commit {after[:7]} on feature/t-1 (3 changed entries)"
    assert git(repo, "rev-parse", "HEAD^").strip() == before
    assert git(repo, "log", "-1", "--format=%an <%ae>|%cn <%ce>").strip() == (
        "Kelpie <kelpie@kelpie.invalid>|Kelpie <kelpie@kelpie.invalid>"
    )
    assert git(repo, "log", "-1", "--format=%B").strip() == MESSAGE
    assert git(repo, "status", "--porcelain") == ""  # index and worktree match the commit
    assert sorted(git(repo, "ls-tree", "--name-only", "HEAD").split()) == ["a.txt", "new.txt"]
    assert git(repo, "symbolic-ref", "HEAD").strip() == "refs/heads/feature/t-1"
    assert not (tmp_path / "nowhere.git").exists()  # never pushed anywhere


def test_the_next_snapshot_reports_the_commit(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    commit(board)
    (session,) = adapter(board).snapshot(NOW).sessions
    assert session.last_commit == head(repo)[:7]
    assert session.dirty_worktree is False


def test_no_hook_and_no_signing_program_runs(board: Board, repo: Path, tmp_path: Path) -> None:
    ended_task(board, repo)
    marker = tmp_path / "ran"
    for name in ("pre-commit", "commit-msg", "post-commit", "reference-transaction"):
        hook(repo, name, marker)
    gpg = tmp_path / "gpg.sh"
    gpg.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    gpg.chmod(0o755)
    git(repo, "config", "commit.gpgSign", "true")
    git(repo, "config", "gpg.program", str(gpg))
    git(repo, "config", "core.fsmonitor", str(gpg))
    commit(board)
    assert not marker.exists()
    assert git(repo, "log", "-1", "--format=%G?").strip() == "N"


def test_global_config_is_not_read(
    board: Board, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended_task(board, repo)
    home = tmp_path / "home"
    home.mkdir()
    (home / "ignore").write_text("new.txt\n")  # read as global config, new.txt is left out
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {home / 'ignore'}\n")
    monkeypatch.setenv("HOME", str(home))
    assert wip.write_env(repo)["GIT_CONFIG_GLOBAL"] == "/dev/null"
    commit(board)
    assert "new.txt" in git(repo, "ls-tree", "--name-only", "HEAD").split()
    assert git(repo, "log", "-1", "--format=%an").strip() == "Kelpie"


def test_a_linked_worktree_is_committed(board: Board, repo: Path, tmp_path: Path) -> None:
    git(repo, "stash", "-u", "-q")
    git(repo, "branch", "feature/t-2")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked), "feature/t-2")
    (linked / "a.txt").write_text("linked\n")
    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.update("t_1", workspace_kind="worktree", workspace_path=str(linked))
    before = head(linked)
    said = adapter(board).wip_commit("t_1", MESSAGE, branch="feature/t-2")
    assert said.endswith("on feature/t-2 (1 changed entries)")
    assert git(linked, "rev-parse", "HEAD^").strip() == before
    assert git(repo, "rev-parse", "feature/t-2").strip() == head(linked)


def test_worktree_scope_config_is_checked(board: Board, repo: Path, tmp_path: Path) -> None:
    git(repo, "stash", "-u", "-q")
    git(repo, "config", "extensions.worktreeConfig", "true")
    git(repo, "branch", "feature/t-2")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", str(linked), "feature/t-2")
    git(linked, "config", "--worktree", "filter.evil.clean", "touch /nonexistent")
    (linked / "a.txt").write_text("linked\n")
    board.task("t_1", "ready", ago(days=1), assignee="dev")
    board.update("t_1", workspace_kind="worktree", workspace_path=str(linked))
    before = head(linked)
    with pytest.raises(UnsupportedAction, match="filter"):
        adapter(board).wip_commit("t_1", MESSAGE, branch="feature/t-2")
    assert head(linked) == before


def test_a_symbolic_branch_ref_is_refused(board: Board, repo: Path) -> None:
    git(repo, "symbolic-ref", "refs/heads/alias", "refs/heads/feature/t-1")
    ended_task(board, repo)
    before = head(repo)
    with pytest.raises(UnsupportedAction, match="not on alias"):
        adapter(board).wip_commit("t_1", MESSAGE, branch="alias")
    assert head(repo) == before


@pytest.mark.parametrize(
    ("message", "expected"), [("   ", "WIP"), ("x" * (wip.MAX_MESSAGE + 50), "x" * wip.MAX_MESSAGE)]
)
def test_the_message_is_capped_and_never_empty(
    board: Board, repo: Path, message: str, expected: str
) -> None:
    ended_task(board, repo)
    adapter(board).wip_commit("t_1", message, branch="feature/t-1")
    assert git(repo, "log", "-1", "--format=%B").strip() == expected


def test_environment_is_fixed(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("KELPIE_TOKEN", "secret")
    env = wip.write_env(repo)
    assert "GIT_DIR" not in env and "KELPIE_TOKEN" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_ALLOW_PROTOCOL"] == "none"
    assert env["GIT_WORK_TREE"] == str(repo)


# --- nothing to do ----------------------------------------------------------------------


def test_a_clean_worktree_is_nothing_to_do(board: Board, repo: Path) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "all")
    ended_task(board, repo)
    before = head(repo)
    with pytest.raises(NothingToDo, match="clean"):
        commit(board)
    assert head(repo) == before


def test_an_active_run_is_nothing_to_do(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    board.run("t_1", "dev", ago(minutes=5))  # a new session started in the worktree
    before = head(repo)
    with pytest.raises(NothingToDo, match="run is active"):
        commit(board)
    assert head(repo) == before


def test_a_claimed_task_is_nothing_to_do(board: Board, repo: Path) -> None:
    ended_task(board, repo, status="running")
    with pytest.raises(NothingToDo, match="run is active"):
        commit(board)


# --- refused before git -----------------------------------------------------------------


def test_draft_pr_is_unsupported(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    before = head(repo)
    with pytest.raises(UnsupportedAction, match="no push, no draft PR"):
        commit(board, draft_pr=True)
    assert head(repo) == before


def test_no_branch_is_unsupported(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    with pytest.raises(UnsupportedAction, match="no branch"):
        adapter(board).wip_commit("t_1", MESSAGE)


def test_git_state_off_is_unsupported(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    with pytest.raises(UnsupportedAction, match="git_state is off"):
        adapter(board, git_state=False).wip_commit("t_1", MESSAGE, branch="feature/t-1")


def test_unknown_task(board: Board) -> None:
    with pytest.raises(HermesError, match="unknown"):
        commit(board)


@pytest.mark.parametrize("kind", ["dir", "scratch"])
def test_only_a_worktree_workspace(board: Board, repo: Path, kind: str) -> None:
    ended_task(board, repo)
    board.update("t_1", workspace_kind=kind)
    with pytest.raises(UnsupportedAction, match="no worktree"):
        commit(board)


def test_a_shared_worktree_is_refused(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    ended_task(board, repo, task_id="t_2", status="review")
    before = head(repo)
    with pytest.raises(UnsupportedAction, match="used by another task"):
        commit(board)
    assert head(repo) == before


# --- refused by the worktree ------------------------------------------------------------


def refused(board: Board, repo: Path, match: str) -> None:
    ended_task(board, repo)
    before = head(repo)
    with pytest.raises(UnsupportedAction, match=match):
        commit(board)
    assert head(repo) == before


@pytest.mark.parametrize(
    "key", ["include.path", "includeIf.gitdir:/x/.path", "filter.lfs.clean", "filter.x.process"]
)
def test_includes_and_filter_drivers_are_refused(board: Board, repo: Path, key: str) -> None:
    git(repo, "config", key, "/nonexistent")
    refused(board, repo, key.split(".")[0].casefold())


def test_a_filter_attribute_on_a_changed_file_is_refused(board: Board, repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (repo / "big.bin").write_bytes(b"\0" * 16)
    refused(board, repo, "filter attribute")


def test_a_filter_attribute_on_other_files_is_fine(board: Board, repo: Path) -> None:
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-q", "-m", "attrs")
    ended_task(board, repo)
    commit(board)


def test_another_branch_is_refused(board: Board, repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "other")
    refused(board, repo, "on other, not on feature/t-1")


def test_a_detached_head_is_refused(board: Board, repo: Path) -> None:
    git(repo, "checkout", "-q", "--detach")
    refused(board, repo, "detached")


@pytest.mark.parametrize("name", wip.IN_PROGRESS)
def test_an_operation_in_progress_is_refused(board: Board, repo: Path, name: str) -> None:
    target = repo / ".git" / name
    if name in ("rebase-merge", "rebase-apply", "sequencer"):
        target.mkdir()
    else:
        target.write_text(head(repo) + "\n")
    refused(board, repo, name)


def test_an_untracked_nested_repository_is_refused(board: Board, repo: Path) -> None:
    nested = repo / "vendor"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "x").write_text("x\n")
    refused(board, repo, "nested repository")


def submodule_repo(tmp_path: Path, repo: Path, marker: Path) -> Path:
    """``repo`` with a populated submodule ``sub`` whose own config names a filter driver
    that leaves ``marker`` and applies to a file changed in it."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-q", "-b", "main")
    (upstream / ".gitattributes").write_text("f.txt filter=evil\n")
    (upstream / "f.txt").write_text("f\n")
    git(upstream, "add", "-A")
    git(upstream, "-c", "user.name=U", "-c", "user.email=u@example.invalid", "commit", "-qm", "u")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
    git(repo, "commit", "-qm", "add sub")
    git(repo / "sub", "config", "filter.evil.clean", f"sh -c 'touch {marker}; cat'")
    (repo / "sub" / "f.txt").write_text("changed\n")
    return repo


def test_a_submodule_is_refused_before_anything_runs_in_it(
    board: Board, repo: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "PWNED"
    submodule_repo(tmp_path, repo, marker)
    refused(board, repo, "submodules")
    assert not marker.exists()


def test_a_gitlink_without_gitmodules_is_refused(board: Board, repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "PWNED"
    submodule_repo(tmp_path, repo, marker)
    git(repo, "rm", "-q", "--cached", ".gitmodules")
    (repo / ".gitmodules").unlink()
    refused(board, repo, "submodules")
    assert not marker.exists()


def test_a_listing_over_the_cap_is_refused(
    board: Board, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wip, "MAX_LISTING", 16)
    refused(board, repo, "too large")


def test_a_large_file_is_refused(board: Board, repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wip, "MAX_FILE_BYTES", 8)
    (repo / "build.log").write_text("x" * 64)
    refused(board, repo, "larger than")


def test_too_many_entries_are_refused(
    board: Board, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(wip, "MAX_ENTRIES", 2)
    refused(board, repo, "more than 2 changed entries")


def test_an_untrusted_layout_is_refused(board: Board, repo: Path, tmp_path: Path) -> None:
    alternates = repo / ".git" / "objects" / "info" / "alternates"
    alternates.write_text(str(tmp_path) + "\n")
    refused(board, repo, "untrusted .git layout")


def test_a_branch_without_a_commit_is_refused(board: Board, tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.mkdir()
    git(path, "init", "-q", "-b", "feature/t-1")
    (path / "x").write_text("x\n")
    ended_task(board, path)
    with pytest.raises(UnsupportedAction, match="no commit yet"):
        commit(board)


def test_a_worktree_of_another_user_is_refused(
    board: Board, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = os.geteuid()
    monkeypatch.setattr(wip.os, "geteuid", lambda: uid + 1)
    refused(board, repo, "another user")


def test_parse_refuses_unmerged_and_submodule_entries() -> None:
    with pytest.raises(wip.WipRefused, match="unmerged"):
        wip._parse("u UU N... 100644 100644 100644 100644 a b c x.txt\0")
    with pytest.raises(wip.WipRefused, match="submodule"):
        wip._parse("1 .M SC.. 160000 160000 160000 a b sub\0")
    assert wip._parse("2 R. N... 100644 100644 100644 a b R100 new\0old\0")[2] == {"new", "old"}


def test_a_branch_name_that_is_not_plain_is_refused(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    for bad in ("-x", "refs/heads/x", "a..b"):
        with pytest.raises(UnsupportedAction, match="not a plain branch name"):
            adapter(board).wip_commit("t_1", MESSAGE, branch=bad)


# --- git failures -----------------------------------------------------------------------


def test_a_branch_moved_meanwhile_is_not_overwritten(
    board: Board, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended_task(board, repo)
    real = gs._run
    moved: list[str] = []

    def racing(cmd: list[str], *args: Any, **kw: Any) -> tuple[int, str, str]:
        if "update-ref" in cmd and not moved:  # an agent committed between status and now
            subprocess.run(
                ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "agent"],
                check=True,
                capture_output=True,
            )
            moved.append(head(repo))
        return real(cmd, *args, **kw)

    monkeypatch.setattr(wip, "_run", racing)
    with pytest.raises(HermesError) as caught:
        commit(board)
    assert caught.value.public_reason == "WIP commit failed (update-ref: git exited 128)"
    assert head(repo) == moved[0]


def test_git_missing_is_a_failure(
    board: Board, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended_task(board, repo)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(HermesError, match="git cannot be run"):
        commit(board)


# --- executor and ledger ----------------------------------------------------------------


def test_the_executor_hands_the_commit_on_as_the_detail(board: Board, repo: Path) -> None:
    ended_task(board, repo)
    action = Action(
        ActionKind.WIP_COMMIT,
        "checkpoints",
        MESSAGE,
        task_id="t_1",
        params={"branch": "feature/t-1"},
    )
    done = PlatformExecutor(adapter(board)).execute(action)
    assert isinstance(done, str) and done.startswith("local commit ")
