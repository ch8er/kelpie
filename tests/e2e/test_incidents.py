"""Farm incidents, replayed on a simulated Hermes farm and caught by the real ``kelpie serve``.

Each scenario builds the board the incident left behind (as Hermes writes it), starts Kelpie
with the rule under test in ``suggest`` mode (Hermes carries out ``stop`` only, so ``act``
is refused for these rules) and checks what reaches the channels: the suggestion for the
incident, nothing for its healthy look-alikes, and no change to the board.
"""

from __future__ import annotations

from datetime import timedelta

from tests.sim.farm import HERMES_STALE, REPO, Farm

SUGGEST = {"mode": "suggest"}
# What a suggestion says Kelpie did (#69): the intent, and that it was not done.
DID_SUGGEST = "(not done, rule in suggest mode)."


def settle(farm: Farm, ticks: int = 3) -> None:
    """Let a few more ticks pass, so that what did not happen has had its chance, then stop
    Kelpie: it delivers queued alerts before it exits, so the channels are final."""
    assert farm.kelpie is not None
    farm.kelpie.wait_ticks(ticks)
    farm.stop()


def board_unchanged(farm: Farm, before: dict[str, str]) -> None:
    assert farm.board.statuses() == before
    assert farm.hermes_calls() == []


def blocked_by_agent(
    farm: Farm, task_id: str, reason: str, *, kind: str | None = None, hours: float = 2
) -> None:
    """A dev worker ran the task and blocked it with ``reason`` ``hours`` ago."""
    b = farm.board
    b.task(task_id, "todo", farm.ago(hours=hours + 2), title=f"Work on {task_id}", assignee="dev")
    b.claim(task_id, "dev", farm.ago(hours=hours + 1))
    b.block(task_id, reason, farm.ago(hours=hours), kind=kind)


# --- incident 1: a guard block outlives its green CI --------------------------------------


def test_incident_01_guard_block_with_green_ci_is_suggested_for_unblock(farm: Farm) -> None:
    gh = farm.github
    gh.repo(REPO)
    green = gh.pull(REPO, 41)
    gh.checks(REPO, green.head_sha, "success", farm.ago(minutes=40))
    pending = gh.pull(REPO, 42)
    gh.checks(REPO, pending.head_sha, "pending")
    blocked_by_agent(farm, "t_guard", "guard: waiting for green ci on #41")
    blocked_by_agent(farm, "t_pending", "guard: waiting for green ci on #42", hours=1)
    before = farm.board.statuses()

    config = farm.config(
        {"stale_blocks": SUGGEST},
        github=True,
        extra={
            "block_conditions": [
                {
                    "pattern": r"waiting for (?:green )?ci (?:on|of) (?P<pr>\S+)",
                    "kind": "ci_green",
                    "repo": REPO,
                }
            ]
        },
    )
    farm.serve(config)

    text = farm.wait_message("stale_blocks", "Suggested: clear the block on t_guard")
    assert "is stale: its condition is met" in text and DID_SUGGEST in text
    assert "Suggested: clear the block on t_guard" in text
    assert "Clearing it" not in text  # the rule no longer words it as done (#69)
    settle(farm)
    assert farm.messages("t_pending") == []  # CI still running: a declared wait
    board_unchanged(farm, before)
    assert any(p.endswith(f"/commits/{green.head_sha}/status?per_page=100") for p in gh.paths())


def test_incident_01_with_a_fine_grained_token_ci_is_read_from_actions_runs(farm: Farm) -> None:
    """The farm's token gets 403 on check runs and statuses (no Checks permission for a
    fine-grained token): CI is derived from the Actions runs of the head commit instead."""
    gh = farm.github
    gh.repo(REPO).fine_grained = True
    green, red, pending = gh.pull(REPO, 41), gh.pull(REPO, 42), gh.pull(REPO, 43)
    nightly_red = gh.pull(REPO, 44)
    started = farm.ago(minutes=50)
    ci, nightly = ".github/workflows/ci.yml", ".github/workflows/nightly.yml"
    done = {"created": started, "status": "completed"}
    gh.run(REPO, workflow=ci, branch="b41", sha=green.head_sha, conclusion="success", **done)
    # Only the listed workflow is required: the unlisted nightly one still running on #41 is
    # no reason to wait (it could only add red).
    gh.run(REPO, workflow=nightly, branch="b41", sha=green.head_sha, created=started)
    gh.run(REPO, workflow=ci, branch="b42", sha=red.head_sha, conclusion="failure", **done)
    gh.run(REPO, workflow=ci, branch="b43", sha=pending.head_sha, created=started)
    # The required workflow is green on #44, but an unlisted one failed there: that is red too
    # (the check-run path would show it), so the block is not stale.
    gh.run(REPO, workflow=ci, branch="b44", sha=nightly_red.head_sha, conclusion="success", **done)
    gh.run(
        REPO, workflow=nightly, branch="b44", sha=nightly_red.head_sha, conclusion="failure", **done
    )
    blocked_by_agent(farm, "t_green", "guard: waiting for green ci on #41")
    blocked_by_agent(farm, "t_red", "guard: waiting for green ci on #42", hours=1)
    blocked_by_agent(farm, "t_running", "guard: waiting for green ci on #43", hours=1)
    blocked_by_agent(farm, "t_nightly", "guard: waiting for green ci on #44", hours=1)
    before = farm.board.statuses()

    config = farm.config(
        {"stale_blocks": SUGGEST},
        github=True,
        extra={
            "block_conditions": [
                {
                    "pattern": r"waiting for (?:green )?ci (?:on|of) (?P<pr>\S+)",
                    "kind": "ci_green",
                    "repo": REPO,
                }
            ],
            "connectors": {"github": {"ci_workflows": ["ci.yml"]}},
        },
    )
    farm.serve(config)

    text = farm.wait_message("stale_blocks", "Suggested: clear the block on t_green")
    assert "is stale: its condition is met" in text and DID_SUGGEST in text
    settle(farm)
    for task in ("t_red", "t_running", "t_nightly"):
        assert farm.messages(task) == [], task
    board_unchanged(farm, before)
    paths = gh.paths()
    assert any("/actions/runs?" in p and f"head_sha={green.head_sha}" in p for p in paths)
    # The refusal is learned once per repository, not repeated on every tick.
    assert len([p for p in paths if "/check-runs" in p]) == 1


# --- incident 15: a reviewer blocks its own card while the PR sits in the merge queue ----


def test_incident_15_waiting_for_merge_block_clears_once_merged(farm: Farm) -> None:
    gh = farm.github
    gh.pull(REPO, 57, merged_at=farm.ago(minutes=25), state="closed")
    gh.pull(REPO, 58)
    blocked_by_agent(farm, "t_merged", "waiting for merge of #57", hours=1)
    blocked_by_agent(farm, "t_open", "waiting for CI/merge of #58", hours=1)
    blocked_by_agent(farm, "t_closed", "waiting for merge of #59", hours=1)
    gh.pull(REPO, 59, state="closed")  # closed without merging: can never be met
    before = farm.board.statuses()

    config = farm.config(
        {"stale_blocks": SUGGEST, "exit_contract": {**SUGGEST, **HERMES_STALE}},
        github=True,
        extra={
            "block_conditions": [
                {
                    "pattern": r"waiting for (?:ci/)?merge of (?P<pr>\S+)",
                    "kind": "pr_merged",
                    "repo": REPO,
                }
            ]
        },
    )
    farm.serve(config)

    text = farm.wait_message("stale_blocks", "Suggested: clear the block on t_merged")
    assert "pr_merged" in text
    dead = farm.wait_message("stale_blocks", "t_closed")
    assert "never clear on its own" in dead
    settle(farm)
    assert farm.messages("t_open") == []  # still in the merge queue: waiting, not dead
    assert farm.messages("exit_contract") == []
    board_unchanged(farm, before)


# --- incident 2: exits without a final status ----------------------------------------------


def test_incident_02_exit_without_status_is_suggested_for_requeue(farm: Farm) -> None:
    b = farm.board
    # The worker vanished: no heartbeat for two hours and its claim expired, before Hermes'
    # sweep closed the run (a crash the sweep sees leaves the task ready, not running).
    b.task("t_gone", "todo", farm.ago(hours=4), title="Paginate the list endpoint", assignee="dev")
    rid = b.claim(
        "t_gone",
        "dev",
        farm.ago(hours=3),
        heartbeat=farm.ago(hours=2),
        claim_expires=farm.ago(hours=1, minutes=45),
    )
    # An agent blocked its task with a reason nobody can act on.
    blocked_by_agent(farm, "t_vague", "stuck, not sure why", hours=1)
    # Look-alikes: a typed block for an operator, and a worker in a long LLM call whose
    # heartbeat is 45 minutes old: Hermes tolerates up to an hour (session_stale_after 60m).
    blocked_by_agent(farm, "t_question", "which API version?", kind="needs_input", hours=1)
    b.task("t_slow", "todo", farm.ago(hours=2), title="Add metrics", assignee="dev")
    b.claim("t_slow", "dev", farm.ago(hours=1), heartbeat=farm.ago(minutes=45))
    before = farm.board.statuses()

    farm.serve(farm.config({"exit_contract": {**SUGGEST, **HERMES_STALE}}))

    gone = farm.wait_message("exit_contract", "return t_gone to the queue")
    assert f"hermes-run-{rid}" in gone and "stopped sending heartbeats" in gone
    vague = farm.wait_message("exit_contract", "return t_vague to the queue")
    assert "not an allowed block reason" in vague
    settle(farm)
    assert farm.messages("t_question") == [] and farm.messages("t_slow") == []
    board_unchanged(farm, before)


# --- incident 10: waiting for CI inside the session ----------------------------------------


def test_incident_10_polling_ci_in_a_session_is_coached_not_stopped(farm: Farm) -> None:
    b = farm.board
    b.task("t_review", "review", farm.ago(hours=1), title="Review: retry policy", assignee="rev")
    rid = b.claim(
        "t_review",
        "rev",
        farm.ago(minutes=12),
        heartbeat=farm.ago(seconds=30),
        claim_expires=farm.now + timedelta(minutes=10),
    )
    for i in range(12):  # the same check every 40 seconds
        farm.spool.tool_call(
            "t_review",
            rid,
            "terminal",
            {"command": "gh pr checks 77"},
            at=farm.ago(seconds=40 * (12 - i)),
            result="build pending",
        )
    before = farm.board.statuses()

    polls = {"ci_status": ["gh pr checks"]}
    rules = {
        "wait_condition": {**SUGGEST, **HERMES_STALE, "poll_patterns": polls},
        # Rule 10 may act (stop) on Hermes: it must leave coached polling alone.
        "loop_breaker": {"mode": "act"},
    }
    farm.serve(farm.config(rules))

    text = farm.wait_message("wait_condition", "comment on t_review")
    assert "same check" in text and "register" in text.lower()
    settle(farm)
    assert farm.messages("loop_breaker") == []
    board_unchanged(farm, before)


# --- incident 11: a task in a status nobody owns -------------------------------------------


def test_incident_11_ownerless_review_with_dependents_is_critical(farm: Farm) -> None:
    b = farm.board
    b.task("t_auth", "todo", farm.ago(hours=6), title="Shared auth middleware", assignee="dev")
    b.claim("t_auth", "dev", farm.ago(hours=5))
    b.hand_off("t_auth", farm.ago(hours=3))
    # Assumption: the review is up for any reviewer, so the task has no assignee (if Hermes
    # kept the developer as assignee, the task would be routed to it, not ownerless).
    b.update("t_auth", assignee=None)
    for i in range(4):
        b.task(f"t_dep{i}", "todo", farm.ago(hours=6), title=f"Protect endpoint {i}")
        b.link("t_auth", f"t_dep{i}")
    # Look-alike: handed over to review ten minutes ago.
    b.task("t_fresh", "todo", farm.ago(hours=1), title="Bump lint config", assignee="dev")
    b.claim("t_fresh", "dev", farm.ago(minutes=40))
    b.hand_off("t_fresh", farm.ago(minutes=10))
    b.update("t_fresh", assignee=None)
    before = farm.board.statuses()

    farm.serve(farm.config({"orphan_status": {**SUGGEST, **HERMES_STALE}}))

    text = farm.wait_message("orphan_status", "t_auth")
    assert text.startswith("\U0001f534") and "4 open task(s) depend on it" in text
    settle(farm)
    assert farm.messages("t_fresh") == []
    board_unchanged(farm, before)
    # Critical alerts reach Slack too (min_severity warning).
    assert any("t_auth" in t for t in farm.channels.slack_texts())


# --- incident 14: the farm sits idle while ready work waits --------------------------------


def idle_dev(farm: Farm, task_id: str, ended_hours: float) -> None:
    """The dev profile finished ``task_id`` ``ended_hours`` ago and has run nothing since."""
    b = farm.board
    b.task(
        task_id, "todo", farm.ago(hours=ended_hours + 3), title=f"Done {task_id}", assignee="dev"
    )
    rid = b.claim(task_id, "dev", farm.ago(hours=ended_hours + 1))
    b.end_run(rid, farm.ago(hours=ended_hours), "completed")
    b.update(
        task_id,
        status="done",
        current_run_id=None,
        completed_at=farm.ago(hours=ended_hours),
    )
    b.event(task_id, "completed", farm.ago(hours=ended_hours), run_id=rid)


def test_incident_14_idle_farm_with_ready_work_is_alerted(farm: Farm) -> None:
    """The idle half of incident 14: the assignee of a ready task routes it, nobody has
    claimed it, so the adapter reports it ownerless (issue 68). The hold itself cannot be
    modelled on Hermes: holds are task labels, and the Hermes adapter reports none."""
    b = farm.board
    idle_dev(farm, "t_prev", ended_hours=3)
    for i in range(3):  # ready for five hours, nobody dispatches them
        b.task(f"t_ready{i}", "ready", farm.ago(hours=5), title=f"Follow-up {i}", assignee="dev")
        b.event(f"t_ready{i}", "promoted", farm.ago(hours=5))
    before = farm.board.statuses()

    rules = {"bottlenecks": {**SUGGEST, "min_idle_time": "30m", "idle_alert_after": "30m"}}
    farm.serve(farm.config(rules))

    text = farm.wait_message("bottlenecks", "t_ready0", timeout=5)
    assert "idle" in text and "dev" in text
    settle(farm)
    board_unchanged(farm, before)


# --- incident 16: a human task gates a chain of agent work ---------------------------------


def human_gate(farm: Farm) -> dict[str, str]:
    """``alice`` (a person) has not done her task for 6 hours; five agent tasks wait behind
    it in a chain; the dev profile has had nothing to run for 2 hours."""
    b = farm.board
    idle_dev(farm, "t_prev", ended_hours=2)
    b.task(
        "t_store",
        "ready",
        farm.ago(hours=6),
        title="Store the products in the app store console",
        assignee="alice",
    )
    b.event("t_store", "promoted", farm.ago(hours=6))
    parent = "t_store"
    for i in range(1, 6):
        b.task(f"t_step{i}", "todo", farm.ago(hours=6), title=f"Store step {i}", assignee="dev")
        b.link(parent, f"t_step{i}")
        parent = f"t_step{i}"
    # Look-alike: a human task with nothing behind it.
    b.task("t_calendar", "ready", farm.ago(hours=8), title="Renew the calendar", assignee="alice")
    return farm.board.statuses()


HUMAN_GATE_RULES = {
    "bottlenecks": {
        **SUGGEST,
        "min_idle_time": "30m",
        "human_gate_min_tasks": 3,
        "human_gate_after": "1h",
    },
    "needs_you": {**SUGGEST, "human_wait_alert_after": "1h"},
}


def test_incident_16_human_task_gating_a_chain_is_escalated(farm: Farm) -> None:
    """Rule 13 escalates human work, one message per person; rule 6 escalates single
    gates only with ``escalate_human_gates = true``."""
    before = human_gate(farm)
    farm.serve(farm.config(HUMAN_GATE_RULES, human_owners=["alice"]))

    text = farm.wait_message("needs_you", "Needs you (alice)", "t_store")
    assert "holds 5 agent task(s)" in text
    settle(farm)
    assert farm.messages("t_calendar") == []
    assert farm.messages("Rule: bottlenecks", " · t_store") == []  # no per-gate alert
    board_unchanged(farm, before)


def test_incident_16_per_gate_escalation_is_opt_in(farm: Farm) -> None:
    before = human_gate(farm)
    rules = {
        **HUMAN_GATE_RULES,
        "bottlenecks": {**HUMAN_GATE_RULES["bottlenecks"], "escalate_human_gates": True},
    }
    farm.serve(farm.config(rules, human_owners=["alice"]))

    text = farm.wait_message("Rule: bottlenecks", " · t_store", "gates 5 agent task(s)")
    assert "owner alice" in text and "Kelpie does not change dependencies" in text
    settle(farm)
    assert farm.messages("t_calendar") == []
    board_unchanged(farm, before)


def test_incident_16_idle_capacity_behind_a_human_gate_is_alerted(farm: Farm) -> None:
    human_gate(farm)
    farm.serve(farm.config(HUMAN_GATE_RULES, human_owners=["alice"]))

    text = farm.wait_message("Rule: bottlenecks", "idle with no ready work", "t_store", timeout=5)
    assert "dev" in text
