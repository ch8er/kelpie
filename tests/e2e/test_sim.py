"""The simulator's own parts, without Kelpie: the fake Hermes CLI, the spool and the TOML
writer. Scenarios trust them, so they are checked against what Kelpie expects of Hermes."""

from __future__ import annotations

import json
import os
import subprocess
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.stop import block_task, stop_comment
from kelpie.core.model import Status
from tests.sim import hermes_cli, toml
from tests.sim.farm import Farm


def test_fake_cli_blocks_the_run_kelpie_names(farm: Farm) -> None:
    b = farm.board
    b.task("t_1", "todo", farm.ago(hours=1), assignee="dev")
    rid = b.claim("t_1", "dev", farm.ago(minutes=30), heartbeat=farm.ago(minutes=1))

    block_task(
        str(farm.hermes_path),
        b.path,
        "t_1",
        rid,
        stop_comment("t_1", "loop"),
        environ={"PATH": os.environ["PATH"], "GITHUB_TOKEN": "secret"},
    )

    assert b.get("t_1").status == "blocked"
    assert b.get("t_1").block_kind == "needs_input"
    run = b.get_run(rid)
    assert (run.outcome, run.ended_at is not None) == ("blocked", True)
    assert b.events("t_1")[-1] == (
        "blocked",
        {"kind": "needs_input", "reason": stop_comment("t_1", "loop")},
    )
    assert b.comments("t_1") == [f"BLOCKED: {stop_comment('t_1', 'loop')}"]
    (call,) = farm.hermes_calls()
    assert call["argv"][:4] == ["kanban", "block", "--kind", "needs_input"]
    assert (call["task"], call["run"], call["code"]) == ("t_1", str(rid), 0)
    assert "GITHUB_TOKEN" not in call["env"]  # Kelpie's allow-list held

    # The adapter reads the result as Hermes' own block: a typed, current block.
    snap = HermesAdapter(db_path=b.path, spool=None, git_state=False).snapshot(farm.now)
    task = snap.task("t_1")
    assert task is not None and task.status is Status.BLOCKED
    assert task.block is not None and task.block.reason.startswith("waiting_human: needs_input")


def test_fake_cli_refuses_a_run_that_is_not_current(farm: Farm) -> None:
    b = farm.board
    b.task("t_1", "todo", farm.ago(hours=1), assignee="dev")
    rid = b.claim("t_1", "dev", farm.ago(minutes=30))
    env = {"PATH": os.environ["PATH"], "HERMES_KANBAN_DB": str(b.path)}
    proc = subprocess.run(
        [str(farm.hermes_path), "kanban", "block", "--", "t_1", "why"],
        env={**env, "HERMES_KANBAN_RUN_ID": str(rid + 1)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1 and "not on run" in proc.stderr
    assert b.get("t_1").status == "running"
    assert b.comments("t_1") == ["BLOCKED: why"]  # written before the block, as Hermes does

    ok = subprocess.run(
        [str(farm.hermes_path), "kanban", "block", "--", "t_1", "why"],
        env={**env, "HERMES_KANBAN_RUN_ID": str(rid)},
        check=False,
    )
    assert ok.returncode == 0
    unblock = subprocess.run(
        [str(farm.hermes_path), "kanban", "unblock", "t_1"], env=env, check=False
    )
    assert unblock.returncode == 0
    assert b.get("t_1").status == "ready"
    assert [c["argv"][1] for c in farm.hermes_calls()] == ["block", "block", "unblock"]

    (b.path.parent / hermes_cli.FAIL).touch()
    failed = subprocess.run(
        [str(farm.hermes_path), "kanban", "unblock", "t_1"], env=env, check=False
    )
    assert failed.returncode == 1


def test_spool_lines_are_the_plugins(farm: Farm) -> None:
    at = farm.ago(minutes=3)
    farm.spool.tool_call("t_1", 7, "terminal", {"command": "gh pr checks 5"}, at=at)
    farm.spool.session_end("t_1", 7, at=at + timedelta(seconds=5))
    tool, end = (json.loads(line) for line in farm.spool.lines())
    assert tool["type"] == "tool" and tool["at"] == at.timestamp()
    assert (tool["task_id"], tool["run_id"], tool["command"]) == ("t_1", "7", "gh pr checks 5")
    assert end["type"] == "session_end" and end["completed"] is True
    assert "HERMES_KANBAN_TASK" not in os.environ  # the worker environment was restored


def test_toml_writer_round_trips() -> None:
    data = {
        "engine": {"interval": "1s", "dry_run": False, "rules": {"a": {"mode": "act", "n": 3}}},
        "block_conditions": [{"pattern": r"merge of (?P<pr>\S+)", "kind": "pr_merged"}],
        "connectors": {"github": {"repos": ["example/repo"], "ci": {"allow_cancel": True}}},
        "x": {"y": {"z": {"k": [1, 2]}, "w": {"m": "n"}}},
    }
    assert tomllib.loads(toml.dumps(data)) == data
    with pytest.raises(TypeError):
        toml.dumps({"a": object()})


def test_install_writes_an_executable(tmp_path: Path) -> None:
    path = hermes_cli.install(tmp_path, Path("/nowhere"))
    assert os.access(path, os.X_OK)
    assert path.read_text().startswith("#!")
