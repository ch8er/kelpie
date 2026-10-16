"""The simulator's own parts: the fake Hermes CLI, the spool, the log check and the TOML
writer. Scenarios trust them, so they are checked against what Kelpie expects of Hermes."""

from __future__ import annotations

import json
import os
import re
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
from tests.sim.kelpie import problems_in


def hermes(farm: Farm, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(farm.hermes_path), *args],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


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
    reason = stop_comment("t_1", "loop")
    assert b.events("t_1")[-1] == ("blocked", {"kind": "needs_input", "reason": reason})
    assert b.comments("t_1") == [f"BLOCKED: {reason}"]
    (call,) = farm.hermes_calls()
    assert call["argv"][:4] == ["kanban", "block", "--kind", "needs_input"]
    assert (call["task"], call["run"], call["code"]) == ("t_1", str(rid), 0)
    assert call["board"] == str(b.path)
    assert "GITHUB_TOKEN" not in call["env"]  # Kelpie's allow-list held

    # The adapter reads the result as Hermes' own block: a typed, current block.
    snap = HermesAdapter(db_path=b.path, spool=None, git_state=False).snapshot(farm.now)
    task = snap.task("t_1")
    assert task is not None and task.status is Status.BLOCKED
    assert task.block is not None and task.block.reason.startswith("waiting_human: needs_input")


def test_fake_cli_pins_the_run_only_for_the_named_task(farm: Farm) -> None:
    b = farm.board
    b.task("t_1", "todo", farm.ago(hours=1), assignee="dev")
    rid = b.claim("t_1", "dev", farm.ago(minutes=30))
    board = {"HERMES_KANBAN_DB": str(b.path)}
    stale = {"HERMES_KANBAN_TASK": "t_1", "HERMES_KANBAN_RUN_ID": str(rid + 1)}

    refused = hermes(farm, "kanban", "block", "--", "t_1", "why", **board, **stale)
    assert refused.returncode == 1 and "not on run" in refused.stderr
    assert b.get("t_1").status == "running"
    assert b.comments("t_1") == ["BLOCKED: why"]  # written before the block, as Hermes does

    # Another task's worker: the run variable does not pin this task (Hermes ignores it).
    other = {"HERMES_KANBAN_TASK": "t_other", "HERMES_KANBAN_RUN_ID": str(rid + 1)}
    ok = hermes(farm, "kanban", "block", "t_1", "why", "--kind", "transient", **board, **other)
    assert ok.returncode == 0, ok.stderr
    assert b.get("t_1").block_kind == "transient"

    assert hermes(farm, "kanban", "unblock", "t_1", **board).returncode == 0
    assert b.get("t_1").status == "ready"
    # A ready task can be blocked too (Hermes' block_task takes running or ready).
    assert hermes(farm, "kanban", "block", "t_1", "hold", **board).returncode == 0
    assert [c["argv"][1] for c in farm.hermes_calls()] == ["block", "block", "unblock", "block"]

    (b.path.parent / hermes_cli.FAIL).touch()
    assert hermes(farm, "kanban", "unblock", "t_1", **board).returncode == 1


def test_fake_cli_records_every_call(farm: Farm) -> None:
    no_board = hermes(farm, "kanban", "block", "t_1", "why")
    bad_kind = hermes(
        farm, "kanban", "block", "t_1", "why", "--kind", HERMES_KANBAN_DB=str(farm.board.path)
    )
    bad_run = hermes(
        farm,
        "kanban",
        "block",
        "t_1",
        "why",
        HERMES_KANBAN_DB=str(farm.board.path),
        HERMES_KANBAN_TASK="t_1",
        HERMES_KANBAN_RUN_ID="x",
    )
    unknown = hermes(farm, "kanban", "archive", "t_1")
    assert [p.returncode for p in (no_board, bad_kind, bad_run, unknown)] == [1, 1, 1, 2]
    calls = farm.hermes_calls()
    assert [c["code"] for c in calls] == [1, 1, 1, 2]
    assert calls[0]["board"] is None


def test_spool_lines_are_the_plugins(farm: Farm, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_SESSION_ID", "the-test-machine")  # e.g. run inside a worker
    before = dict(os.environ)
    at = farm.ago(minutes=3)
    farm.spool.tool_call("t_1", 7, "terminal", {"command": "gh pr checks 5"}, at=at)
    farm.spool.session_end("t_1", 7, at=at + timedelta(seconds=5))
    tool, end = (json.loads(line) for line in farm.spool.lines())
    assert tool["type"] == "tool" and tool["at"] == at.timestamp()
    assert (tool["task_id"], tool["run_id"], tool["command"]) == ("t_1", "7", "gh pr checks 5")
    assert tool["session_id"] is None  # nothing of the test machine's own Hermes
    assert end["type"] == "session_end" and end["completed"] is True
    assert dict(os.environ) == before  # the worker environment was restored


LINE = "2026-10-16 01:02:03,456 {level} kelpie.{name} {message}"


def test_problems_are_errors_failure_warnings_and_tracebacks() -> None:
    lines = [
        LINE.format(level="INFO", name="server", message="kelpie listening"),
        LINE.format(level="WARNING", name="app", message="OTLP ingest accepts spans"),
        LINE.format(level="ERROR", name="app", message="adapter hermes failed to snapshot: x"),
        LINE.format(level="WARNING", name="core.engine", message="action failed: stop: boom"),
        LINE.format(level="ERROR", name="core.engine", message="engine tick failed"),
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "RuntimeError: boom",
        LINE.format(level="INFO", name="server", message="received SIGTERM"),
        "Traceback (most recent call last):",
    ]
    assert problems_in(lines) == [lines[2], lines[3], lines[4], lines[5], lines[9]]
    allowed = [re.compile("engine tick failed"), re.compile("action failed")]
    # The traceback right after an allowed line is its own; a later one is not.
    assert problems_in(lines, allowed) == [lines[2], lines[9]]


def test_a_kelpie_error_is_caught(farm: Farm) -> None:
    """The teardown check works on a real log: a board Kelpie cannot read is an ERROR."""
    config = farm.config()
    config["adapters"]["hermes"]["db_path"] = str(farm.root / "missing" / "kanban.db")
    kelpie = farm.serve(config)
    kelpie.wait_ticks(2)
    farm.stop()
    assert any("adapter hermes failed to snapshot" in p for p in kelpie.problems())
    farm.allowed_errors.append(re.compile("adapter hermes failed to snapshot"))


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
    path = hermes_cli.install(tmp_path, Path("/nowhere"), tmp_path / "calls.jsonl")
    assert os.access(path, os.X_OK)
    assert path.read_text().startswith("#!")
