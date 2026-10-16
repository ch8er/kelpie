"""Incident 5 end to end: the CI minutes quota is used up and tasks wait for CI that will not
run. The minutes used come from GitHub's billing usage report (issue #43), read with a token
of its own, and rule 9 says the cause is billing, naming the tasks that wait.
"""

from __future__ import annotations

from datetime import timedelta

from tests.sim.farm import BILLING_TOKEN_ENV, REPO, Farm
from tests.sim.github import BILLING_TOKEN, TOKEN

ACCOUNT = "farm-owner"
CI_GREEN = {
    "pattern": r"waiting for (?:green )?ci (?:on|of) (?P<pr>\S+)",
    "kind": "ci_green",
    "repo": REPO,
}


def blocked_on_ci(farm: Farm, task_id: str, pr: int) -> None:
    b = farm.board
    b.task(task_id, "todo", farm.ago(hours=13), title=f"Change {task_id}", assignee="dev")
    b.claim(task_id, "dev", farm.ago(hours=12))
    b.block(task_id, f"waiting for green ci on #{pr}", farm.ago(hours=11))
    pull = farm.github.pull(REPO, pr)
    farm.github.checks(REPO, pull.head_sha, "pending")


def ci_config(farm: Farm) -> dict:
    return farm.config(
        {"ci_economy": {"mode": "suggest"}},
        github=True,
        extra={
            "block_conditions": [CI_GREEN],
            "connectors": {
                "github": {
                    "ci": {
                        "included_minutes": 3000,
                        "quota_reset_day": 1,
                        "billing_user": ACCOUNT,
                        "billing_token_env": BILLING_TOKEN_ENV,
                        "billing_interval": "5m",
                    }
                }
            },
        },
    )


def test_incident_05_exhausted_ci_minutes_name_billing_and_the_waiting_tasks(farm: Farm) -> None:
    farm.github.repo(REPO)
    for i, pr in enumerate((51, 52, 53)):
        blocked_on_ci(farm, f"t_ci{i}", pr)
    # The whole quota went this period, a little every day since the reset.
    start = farm.now.replace(day=1, hour=0, minute=0, second=0)
    days = max((farm.now - start).days + 1, 1)
    for d in range(days):  # whole minutes, the remainder on the last day: exactly 3000
        share = 3000 // days + (3000 % days if d == days - 1 else 0)
        farm.github.minutes(ACCOUNT, start + timedelta(days=d), share)
    farm.github.minutes(ACCOUNT, start - timedelta(days=2), 900)  # last period: not counted
    farm.serve(ci_config(farm))

    text = farm.wait_message("ci_economy", "exhausted")
    farm.stop()
    assert text.startswith("[CRITICAL] ci_economy: alert")
    assert "(3000 of 3000 minutes)" in text and "the cause is billing, not the code" in text
    assert "3 tasks wait for CI that will not run: t_ci0, t_ci1, t_ci2" in text
    assert any("exhausted" in t for t in farm.channels.slack_texts())  # critical: Slack too
    billing = [r for r in farm.github.requests if "/settings/billing/usage" in r.path]
    assert billing and {r.token for r in billing} == {BILLING_TOKEN}
    others = [r for r in farm.github.requests if r not in billing]
    assert others and {r.token for r in others} == {TOKEN}


def test_a_quota_with_room_left_stays_quiet(farm: Farm) -> None:
    farm.github.repo(REPO)
    blocked_on_ci(farm, "t_ci0", 51)
    farm.github.minutes(ACCOUNT, farm.now, 120)
    kelpie = farm.serve(ci_config(farm))
    kelpie.wait_ticks(3)
    farm.stop()
    assert farm.messages("ci_economy") == []
    assert any("/settings/billing/usage" in r.path for r in farm.github.requests)
