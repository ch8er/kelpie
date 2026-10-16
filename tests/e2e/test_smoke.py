"""The simulator itself: a healthy farm, watched by ``kelpie serve`` for a few ticks.

Nothing is wrong on this board, so nothing may happen: no Hermes command, no alert, no
error in the log. It also checks the plumbing every other scenario relies on: the board is
read through the real adapter (``/metrics`` counts its tasks), the spool written through the
real plugin is found, and ``kelpie status`` reads the same config.
"""

from __future__ import annotations

from datetime import timedelta

from kelpie.sim.github import TOKEN
from tests.sim.farm import HERMES_STALE, REPO, Farm


def healthy_board(farm: Farm) -> None:
    b = farm.board
    b.task("t_ready", "ready", farm.ago(hours=1), title="Write the changelog", assignee="dev")
    b.task("t_run", "todo", farm.ago(hours=2), title="Add a health endpoint", assignee="dev")
    rid = b.claim(
        "t_run",
        "dev",
        farm.ago(minutes=20),
        heartbeat=farm.ago(minutes=1),
        claim_expires=farm.now + timedelta(minutes=15),
    )
    for i, tool in enumerate(["read_file", "edit_file", "terminal"]):
        farm.spool.tool_call(
            "t_run", rid, tool, {"path": f"src/app_{i}.py"}, at=farm.ago(minutes=10 - i)
        )
    b.task(
        "t_done",
        "done",
        farm.ago(hours=5),
        title="Fix the login flake",
        assignee="dev",
        completed=farm.ago(hours=1),
    )


def test_healthy_farm_stays_quiet(farm: Farm) -> None:
    healthy_board(farm)
    rules = {name: HERMES_STALE for name in ("exit_contract", "orphan_status", "wait_condition")}
    kelpie = farm.serve(farm.config({**rules, "stale_blocks": {"mode": "suggest"}}))

    kelpie.wait_ticks(3)
    status = kelpie.cli("status")

    metrics = kelpie.metrics()
    assert metrics['kelpie_tasks{status="ready"}'] == 1
    assert metrics['kelpie_tasks{status="in_progress"}'] == 1
    assert metrics['kelpie_tasks{status="done"}'] == 1
    assert metrics.get('kelpie_adapter_errors_total{adapter="hermes"}', 0) == 0
    farm.stop()  # queued alerts are delivered before it exits
    assert farm.hermes_calls() == []
    assert farm.channels.texts() == []
    assert farm.board.statuses() == {"t_ready": "ready", "t_run": "running", "t_done": "done"}
    assert status.code == 0, status.stderr
    assert "hermes" in status.stdout and "readable" in status.stdout
    assert "spool: last line" in status.stdout


def test_alerts_reach_both_channels_and_github_is_read_with_the_token(farm: Farm) -> None:
    """The other half of the plumbing: something to report, a PR to look up."""
    farm.github.pull(REPO, 7, merged_at=farm.ago(minutes=30), state="closed")
    b = farm.board
    b.task("t_wait", "todo", farm.ago(hours=3), title="Wire the client", assignee="dev")
    b.claim("t_wait", "dev", farm.ago(hours=2))
    b.block("t_wait", "waiting for merge of #7", farm.ago(hours=1))
    b.task("t_vague", "todo", farm.ago(hours=3), title="Tidy the config", assignee="dev")
    b.claim("t_vague", "dev", farm.ago(hours=2))
    b.block("t_vague", "stuck", farm.ago(hours=1))
    rules = {
        "exit_contract": {"mode": "suggest", **HERMES_STALE},
        "stale_blocks": {"mode": "suggest"},
    }
    pattern = {"pattern": r"waiting for merge of (?P<pr>\S+)", "kind": "pr_merged", "repo": REPO}
    farm.serve(farm.config(rules, github=True, extra={"block_conditions": [pattern]}))

    farm.wait_message("stale_blocks", "unblock t_wait")  # info: Telegram only
    farm.wait_message("exit_contract", "requeue t_vague")  # warning: Slack too
    farm.stop()
    slack = farm.channels.slack_texts()
    assert len(slack) == 1 and "requeue t_vague" in slack[0]
    reads = farm.github.requests
    assert any(r.path == f"/repos/{REPO}/pulls/7" for r in reads)
    assert {r.token for r in reads} == {TOKEN}
