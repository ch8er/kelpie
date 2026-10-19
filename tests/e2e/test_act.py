"""The two places Kelpie acts on a farm by itself, end to end, in ``act`` mode.

- Rule 10 (``loop_breaker``) stops a runaway Hermes run: the real adapter runs the (fake)
  ``hermes kanban block`` command pinned to that run, and the board shows the task blocked
  for a human with the run ended.
- Rule 9 (``ci_economy``) cancels a superseded CI build through the GitHub connector: the
  fake GitHub receives the cancel for that run and nothing else.
"""

from __future__ import annotations

import json
import re
import signal
import socket
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.hermes.worker import fingerprint, instantiation_epoch, read_proc
from kelpie.sim.github import Run, sha_of
from tests.sim.farm import REPO, Farm
from tests.sim.kelpie import wait_for

ACT = {"mode": "act"}


def live_run(farm: Farm, task_id: str, title: str, minutes: float = 40) -> int:
    b = farm.board
    b.task(task_id, "todo", farm.ago(hours=2), title=title, assignee="ops")
    return b.claim(
        task_id,
        "ops",
        farm.ago(minutes=minutes),
        heartbeat=farm.ago(seconds=20),
        claim_expires=farm.now + timedelta(minutes=10),
    )


def heartbeat_loop(farm: Farm, task_id: str, run_id: int, calls: int = 20) -> None:
    """The same call every 30 seconds, ending now."""
    for i in range(calls):
        farm.spool.tool_call(
            task_id,
            run_id,
            "heartbeat_check",
            {"target": "all"},
            at=farm.ago(seconds=30 * (calls - i)),
        )


# --- rule 10: a runaway loop is stopped --------------------------------------------------


@dataclass
class Runaways:
    loop: int
    errors: int
    healthy: int


def runaway_farm(farm: Farm) -> Runaways:
    loop = live_run(farm, "t_loop", "Nightly maintenance heartbeat")
    heartbeat_loop(farm, "t_loop", loop)
    errors = live_run(farm, "t_errors", "Migrate the settings loader", minutes=20)
    for i, tool in enumerate(["terminal", "terminal", "edit_file", "terminal", "terminal"]):
        farm.spool.tool_call(
            "t_errors", errors, tool, {"attempt": i}, at=farm.ago(minutes=6 - i), status="error"
        )
    healthy = live_run(farm, "t_ok", "Paginate the audit log", minutes=30)
    for i, tool in enumerate(["read_file", "edit_file", "terminal", "read_file", "edit_file"]):
        farm.spool.tool_call("t_ok", healthy, tool, {"step": i}, at=farm.ago(minutes=9 - 2 * i))
    return Runaways(loop, errors, healthy)


def test_runaway_loop_is_stopped_through_the_hermes_cli(farm: Farm) -> None:
    runs = runaway_farm(farm)
    kelpie = farm.serve(farm.config({"loop_breaker": ACT}))

    farm.wait_message("loop_breaker", "t_loop", "identical")
    farm.wait_message("loop_breaker", "t_errors", "consecutive tool errors")
    kelpie.wait_ticks(3)  # a stopped run must not be stopped again
    farm.stop()

    calls = farm.hermes_calls()
    assert sorted((c["task"], c["run"]) for c in calls) == [
        ("t_errors", str(runs.errors)),
        ("t_loop", str(runs.loop)),
    ]
    for call in calls:
        assert call["code"] == 0
        assert call["argv"][:6] == ["kanban", "block", "--kind", "needs_input", "--", call["task"]]
        assert call["argv"][6].startswith("Stopped by Kelpie: Runaway run: session hermes-run-")
        assert f"hermes kanban unblock {call['task']} [kelpie-stop:v1:" in call["argv"][6]
        assert call["board"] == str(farm.board.path)
        assert not any(k.startswith(("KELPIE_", "GITHUB")) for k in call["env"])
    for task_id, rid in (("t_loop", runs.loop), ("t_errors", runs.errors)):
        row = farm.board.get(task_id)
        assert (row.status, row.block_kind, row.current_run_id) == ("blocked", "needs_input", None)
        assert farm.board.get_run(rid).outcome == "blocked"
        assert farm.board.comments(task_id)[0].startswith("BLOCKED: Stopped by Kelpie")
    assert farm.board.get("t_ok").status == "running"
    assert farm.messages("t_ok") == []
    # The task id reaches Slack byte for byte, as a code element a person can copy.
    raw = [json.dumps(b, ensure_ascii=False) for b in farm.channels.slack_bodies()]
    code = '{"type": "text", "text": "t_loop", "style": {"code": true}}'
    assert any(code in r for r in raw) and all("\u200b" not in r for r in raw)
    # Critical: both channels hear about each runaway, once.
    slack = farm.channels.slack_texts()
    runaway = [t for t in slack if t.startswith("\U0001f534") and "Rule: loop_breaker" in t]
    assert sum("t_loop" in t for t in runaway) == 1
    assert sum("t_errors" in t for t in runaway) == 1


@pytest.fixture
def worker() -> Iterator[subprocess.Popen[bytes]]:
    """A process standing in for a run's worker."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    yield child
    child.kill()
    child.wait(timeout=10)


def bind(farm: Farm, run_id: int, child: subprocess.Popen[bytes]) -> None:
    """Record ``child`` as the run's worker, with Hermes' proof of identity."""
    p = read_proc(child.pid)
    assert p is not None
    farm.board.bind_worker(
        run_id,
        farm.ago(minutes=1),
        claim_lock=f"{socket.gethostname()}:{child.pid}",
        submitter=(child.pid, fingerprint(p, instantiation_epoch())),
    )


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs /proc")
def test_a_stop_is_announced_as_done(farm: Farm, worker: subprocess.Popen[bytes]) -> None:
    """One critical message, and it says what Kelpie did, with Hermes' specifics (#70): the
    task is blocked and the run's worker process ended (#175)."""
    loop = live_run(farm, "t_loop", "Nightly maintenance heartbeat")
    bind(farm, loop, worker)
    heartbeat_loop(farm, "t_loop", loop)
    farm.serve(farm.config({"loop_breaker": ACT}))
    wait_for("the stop", lambda: farm.board.get("t_loop").status == "blocked", timeout=20)
    farm.stop()

    about_loop = farm.messages("t_loop")
    assert about_loop, "no message about the runaway"
    assert all("Stop proposed" not in t for t in about_loop)
    assert all("only sent this alert" not in t for t in about_loop)
    assert any(re.search(r"\b(stopped|blocked)\b", t, re.IGNORECASE) for t in about_loop)
    (message,) = about_loop  # the runaway alert carries the stop: no second message
    assert message.startswith("\U0001f534 Runaway run on t_loop: ") and " · t_loop" in message
    assert re.fullmatch(
        rf"Kelpie did: Stopped run hermes-run-{loop}; task t_loop is blocked for a human "
        r"\(hermes kanban unblock t_loop\); its worker ended \d+\.\ds after SIGTERM\.",
        message.splitlines()[-2],
    )
    assert worker.wait(timeout=5) == -signal.SIGTERM  # the worker itself, not just the card


def test_a_refused_stop_reaches_a_human(farm: Farm) -> None:
    """Hermes refuses the block (the CLI fails): the run keeps going, Kelpie says so once,
    on every channel that takes warnings."""
    loop = live_run(farm, "t_loop", "Nightly maintenance heartbeat")
    heartbeat_loop(farm, "t_loop", loop, calls=12)
    (farm.board.path.parent / "hermes-fail").touch()
    farm.allowed_errors.append(re.compile(r"action failed: Action\(kind=<ActionKind.STOP"))

    kelpie = farm.serve(farm.config({"loop_breaker": ACT}))

    # The runaway alert carries what happened to its stop: one critical message.
    text = farm.wait_message("loop_breaker", "t_loop", "Failed: could not stop run")
    assert text.startswith("\U0001f534 Runaway run on t_loop: ") and " · t_loop" in text
    assert text.splitlines()[-2] == (
        f"Kelpie did: Failed: could not stop run hermes-run-{loop}: hermes kanban block "
        "failed (exit 1), needs a human."
    )
    kelpie.wait_ticks(3)
    farm.stop()
    assert farm.board.get("t_loop").status == "running"
    assert len(farm.messages("Failed:", "t_loop")) == 1  # once per cooldown, not per tick
    assert sum("Failed:" in t and "t_loop" in t for t in farm.channels.slack_texts()) == 1
    assert {c["code"] for c in farm.hermes_calls()} == {1}


# --- rule 9: a superseded build is cancelled ---------------------------------------------


@dataclass
class Builds:
    old: Run
    new: Run
    main_old: Run
    done: Run
    lint: Run


def superseded_builds(farm: Farm) -> Builds:
    gh, ci = farm.github, ".github/workflows/ci.yml"
    gh.repo(REPO)

    def run(branch: str, label: str, minutes: float, **fields: Any) -> Run:
        return gh.run(
            REPO,
            workflow=ci,
            branch=branch,
            sha=sha_of(label),
            created=farm.ago(minutes=minutes),
            **fields,
        )

    old = run("task/t_1", "t_1-a", 25, message="Add the retry policy")
    # Same branch, another workflow, still running: a different group, never superseded.
    lint = gh.run(
        REPO,
        workflow=".github/workflows/lint.yml",
        branch="task/t_1",
        sha=sha_of("t_1-a"),
        created=farm.ago(minutes=24),
    )
    new = run("task/t_1", "t_1-b", 4, message="Address review comments")
    # Look-alikes: the default branch is never cancelled; a finished build cannot be.
    main_old = run("main", "m-a", 20)
    run("main", "m-b", 3)
    done = run(
        "task/t_2",
        "t_2-a",
        50,
        status="completed",
        conclusion="success",
        updated=farm.ago(minutes=30),
    )
    run("task/t_2", "t_2-b", 10)
    return Builds(old, new, main_old, done, lint)


def ci_config(farm: Farm) -> dict[str, Any]:
    return farm.config(
        {"ci_economy": {**ACT, "waste_min_minutes": 10}},
        github=True,
        extra={
            "connectors": {
                "github": {
                    "ci": {
                        "allow_cancel": True,
                        "cancel_repos": [REPO],
                        "poll_interval": "30s",
                        "window": "24h",
                    }
                }
            }
        },
    )


def test_superseded_build_is_cancelled_on_github(farm: Farm) -> None:
    gh = farm.github
    builds = superseded_builds(farm)
    kelpie = farm.serve(ci_config(farm))

    wait_for("the cancel request", lambda: bool(gh.cancelled), timeout=30)
    summary = farm.wait_message("ci_economy", "superseded")
    assert re.search(r"\b2[5-9] min\b", summary)  # 25 min, plus however long startup took
    kelpie.wait_ticks(3)
    farm.stop()

    runs = gh.repos[REPO].runs
    assert gh.cancelled == [(REPO, builds.old.id)]
    assert runs[builds.old.id].conclusion == "cancelled"
    assert runs[builds.new.id].status == "in_progress"
    assert runs[builds.main_old.id].status == "in_progress"
    assert runs[builds.done.id].conclusion == "success"
    assert runs[builds.lint.id].status == "in_progress"
    posts = [r.path for r in gh.requests if r.method == "POST"]
    assert posts == [f"/repos/{REPO}/actions/runs/{builds.old.id}/cancel"]
    assert farm.hermes_calls() == []


def test_a_cancel_is_announced_as_done(farm: Farm) -> None:
    """The cancel says it was done and which newer build superseded it (#70)."""
    builds = superseded_builds(farm)
    farm.serve(ci_config(farm))
    wait_for("the cancel request", lambda: bool(farm.github.cancelled), timeout=30)
    farm.wait_message("ci_economy", "superseded")
    farm.stop()

    announced = [
        t
        for t in farm.channels.telegram_texts()
        if re.search(r"\bcancell?ed\b", t, re.IGNORECASE) and f"{REPO}:{builds.old.id}" in t
    ]
    assert announced, "nothing says the build was cancelled"
    (done,) = announced  # the waste summary of that tick carries it: one message
    assert done.startswith("\U0001f535 CI waste: ") and "CI waste in the last 24h" in done
    assert "still runs" not in done
    assert done.splitlines()[-2] == (
        f"Kelpie did: Cancelled build {REPO}:{builds.old.id} "
        f"(superseded by {REPO}:{builds.new.id})."
    )
