"""The two places Kelpie acts on a farm by itself, end to end, in ``act`` mode.

- Rule 10 (``loop_breaker``) stops a runaway Hermes run: the real adapter runs the (fake)
  ``hermes kanban block`` command pinned to that run, and the board shows the task blocked
  for a human with the run ended.
- Rule 9 (``ci_economy``) cancels a superseded CI build through the GitHub connector: the
  fake GitHub receives the cancel for that run and nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from tests.sim.farm import REPO, Farm
from tests.sim.github import Run, sha_of
from tests.sim.kelpie import wait_for

ACT = {"mode": "act"}
ISSUE_70 = (
    "https://github.com/ch8er/kelpie/issues/70: an effect carried out in act mode is not "
    "announced; the rule's alert says it was only proposed"
)


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
        assert call["argv"][6].startswith("Stopped by Kelpie: Stopping session hermes-run-")
        assert call["argv"][6].endswith(f"hermes kanban unblock {call['task']}")
        assert call["board"] == str(farm.board.path)
        assert not any(k.startswith(("KELPIE_", "GITHUB")) for k in call["env"])
    for task_id, rid in (("t_loop", runs.loop), ("t_errors", runs.errors)):
        row = farm.board.get(task_id)
        assert (row.status, row.block_kind, row.current_run_id) == ("blocked", "needs_input", None)
        assert farm.board.get_run(rid).outcome == "blocked"
        assert farm.board.comments(task_id)[0].startswith("BLOCKED: Stopped by Kelpie")
    assert farm.board.get("t_ok").status == "running"
    assert farm.messages("t_ok") == []
    # Critical: both channels hear about each runaway, once.
    slack = farm.channels.slack_texts()
    assert sum("t_loop" in t for t in slack) == 1
    assert sum("t_errors" in t for t in slack) == 1


@pytest.mark.xfail(strict=True, reason=ISSUE_70)
def test_a_stop_is_announced_as_done(farm: Farm) -> None:
    loop = live_run(farm, "t_loop", "Nightly maintenance heartbeat")
    heartbeat_loop(farm, "t_loop", loop)
    farm.serve(farm.config({"loop_breaker": ACT}))
    wait_for("the stop", lambda: farm.board.get("t_loop").status == "blocked", timeout=20)
    farm.stop()

    about_loop = farm.messages("t_loop")
    assert about_loop, "no message about the runaway"
    assert all("Stop proposed" not in t for t in about_loop)
    assert all("only sent this alert" not in t for t in about_loop)


def test_a_refused_stop_reaches_a_human(farm: Farm) -> None:
    """Hermes refuses the block (the CLI fails): the run keeps going, Kelpie says so once,
    on every channel that takes warnings."""
    loop = live_run(farm, "t_loop", "Nightly maintenance heartbeat")
    heartbeat_loop(farm, "t_loop", loop, calls=12)
    (farm.board.path.parent / "hermes-fail").touch()
    farm.allowed_errors.append(re.compile(r"action failed: Action\(kind=<ActionKind.STOP"))

    kelpie = farm.serve(farm.config({"loop_breaker": ACT}))

    text = farm.wait_message("loop_breaker", "failed", "t_loop")
    assert "Needs a human" in text and text.startswith("[WARNING]")
    kelpie.wait_ticks(3)
    farm.stop()
    assert farm.board.get("t_loop").status == "running"
    assert len(farm.messages("failed", "t_loop")) == 1  # once per cooldown, not per tick
    assert sum("failed" in t and "t_loop" in t for t in farm.channels.slack_texts()) == 1
    assert {c["code"] for c in farm.hermes_calls()} == {1}


# --- rule 9: a superseded build is cancelled ---------------------------------------------


@dataclass
class Builds:
    old: Run
    new: Run
    main_old: Run
    done: Run


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
    return Builds(old, new, main_old, done)


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
    assert "25 min" in summary
    kelpie.wait_ticks(3)
    farm.stop()

    runs = gh.repos[REPO].runs
    assert gh.cancelled == [(REPO, builds.old.id)]
    assert runs[builds.old.id].conclusion == "cancelled"
    assert runs[builds.new.id].status == "in_progress"
    assert runs[builds.main_old.id].status == "in_progress"
    assert runs[builds.done.id].conclusion == "success"
    posts = [r.path for r in gh.requests if r.method == "POST"]
    assert posts == [f"/repos/{REPO}/actions/runs/{builds.old.id}/cancel"]
    assert farm.hermes_calls() == []


@pytest.mark.xfail(strict=True, reason=ISSUE_70)
def test_a_cancel_is_announced_as_done(farm: Farm) -> None:
    builds = superseded_builds(farm)
    farm.serve(ci_config(farm))
    wait_for("the cancel request", lambda: bool(farm.github.cancelled), timeout=30)
    farm.wait_message("ci_economy", "superseded")
    farm.stop()

    assert farm.messages("cancel", str(builds.old.id)), "nothing says the build was cancelled"
