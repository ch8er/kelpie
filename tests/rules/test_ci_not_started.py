"""Rule 9, CI jobs that never start: one outage (#229, incident 20)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action
from kelpie.core.incidents import CI_NOT_STARTED, Outage, is_outage_state
from kelpie.core.model import Build, BuildStatus, Job, Snapshot
from kelpie.rules._ci_not_started import STATE
from kelpie.rules.ci_economy import CiEconomy, build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
FAILED, PASSED = BuildStatus.FAILURE, BuildStatus.SUCCESS
REFUSED = Job("ci-ok", FAILED, started=False)
RAN = Job("test", PASSED)
OK = Job("ci-ok", PASSED)


def refused(n: int, minutes_ago: float, *, repo: str = "example/shop", **fields: Any) -> Build:
    at = NOW - minutes_ago * M
    base: dict[str, Any] = {
        "repo": repo,
        "workflow": "ci.yml",
        "branch": f"task/T-{n}",
        "sha": f"sha-{n}-{minutes_ago:g}",
        "event": "pull_request",
        "pr": n,
        "status": FAILED,
        "started_at": at,
        "finished_at": at,
        "jobs": (RAN, REFUSED),
    }
    return Build(f"B-{n}-{minutes_ago:g}", **{**base, **fields})


def queued(n: int, minutes_ago: float, **fields: Any) -> Build:
    base: dict[str, Any] = {"status": BuildStatus.QUEUED, "finished_at": None, "jobs": None}
    return refused(n, minutes_ago, **{**base, **fields})


def ran(minutes_ago: float, *, repo: str = "example/shop", **fields: Any) -> Build:
    """A build whose jobs all started (ci-ok among them)."""
    base: dict[str, Any] = {"status": PASSED, "branch": "main", "pr": None, "jobs": (RAN, OK)}
    return refused(0, minutes_ago, repo=repo, **{**base, **fields})


BEFORE = ran(300, event="pull_request", branch="task/T-0", pr=10)  # ci-ok ran on PRs before


def found(*builds: Build, rule: CiEconomy | None = None, at: datetime = NOW) -> list[Action]:
    snapshot = Snapshot(now=at, builds=builds)
    actions = (rule or CiEconomy()).evaluate(snapshot)
    return [a for a in actions if is_outage_state(a.state or "")]


THREE = (refused(1, 50), refused(2, 40), refused(3, 30))


def test_three_prs_whose_ci_was_refused_are_one_outage() -> None:
    [alert] = found(BEFORE, *THREE)
    outage = Outage.from_params(alert.params)
    assert outage is not None and outage.kind == CI_NOT_STARTED
    assert alert.state == alert.key == outage.state == STATE
    assert outage.prs == ("example/shop#1", "example/shop#2", "example/shop#3")
    assert alert.since == NOW - 50 * M and alert.observe_only
    assert "billing and spending limits, or a protection rule" in (outage.cause or "")
    assert "CI jobs not starting since 2026-03-02 11:10 UTC: 3 change(s)" in alert.reason
    assert alert.params["headline"] == "CI jobs not starting: 3 changes waiting"


def test_a_job_that_never_ran_for_that_trigger_is_no_sign() -> None:
    assert found(*THREE) == []  # no evidence ci-ok ever ran here
    deploys = ran(300, event="push")  # it runs on pushes; PRs are refused by protection rules
    assert found(deploys, *THREE) == []
    assert found(ran(300, event=None), *THREE) != []  # a trigger unknown: as evidence


def test_a_refusal_counts_until_the_job_runs_again_however_long() -> None:
    assert found(BEFORE, *THREE, at=NOW + 5 * 60 * M) != []  # hours later, still refused
    spread = (refused(1, 300), refused(2, 40), refused(3, 30))  # not within the 2h window
    assert found(replace(BEFORE, started_at=NOW - 400 * M), *spread) == []
    assert found(BEFORE, *THREE[1:]) == []  # two PRs


def test_only_the_refused_job_running_again_clears_it() -> None:
    assert found(BEFORE, *THREE, ran(20)) == []  # ci-ok ran again in example/shop
    other_job = ran(20, jobs=(RAN, Job("mac", PASSED)))  # a self-hosted workflow's run
    assert found(BEFORE, *THREE, other_job) != []
    other_workflow = ran(20, workflow="nightly.yml")
    assert found(BEFORE, *THREE, other_workflow) != []
    assert found(BEFORE, *THREE, ran(20, repo="example/docs")) != []  # another repository
    long_build = ran(65, finished_at=NOW - 5 * M)  # started before them, finished after
    assert found(BEFORE, *THREE, long_build) != []


def test_the_resolved_notice_names_the_first_build_that_ran_it_again() -> None:
    rule = CiEconomy()
    snapshot = Snapshot(now=NOW, builds=(BEFORE, *THREE, ran(10), ran(20)))
    assert rule.resolved(STATE, snapshot) == (
        "CI started jobs again: B-0-20 in example/shop ran ci-ok (started 2026-03-02 11:40 UTC)."
    )
    old_elsewhere = ran(400, repo="example/other")
    assert rule.resolved(STATE, Snapshot(now=NOW, builds=(old_elsewhere, BEFORE, *THREE))) is None


def test_runs_left_queued_count_once_queued_long_enough() -> None:
    [alert] = found(BEFORE, refused(1, 50), queued(2, 40), queued(3, 31))
    cause = Outage.from_params(alert.params).cause  # type: ignore[union-attr]
    assert cause is not None and "2 run(s) queued over 30 min" in cause and "1 run(s)" in cause
    assert found(BEFORE, refused(1, 50), queued(2, 40), queued(3, 29)) == []  # not 30 min yet


def test_a_queue_with_runners_at_work_or_held_on_purpose_is_no_outage() -> None:
    waiting = (queued(1, 50), queued(2, 45), queued(3, 40))
    assert found(*waiting) != []
    busy = replace(ran(60), status=BuildStatus.RUNNING, finished_at=None)
    assert found(*waiting, busy) == []  # one busy runner: a queue
    assert found(*waiting, ran(35)) == []  # a later build ran
    held = tuple(queued(n, 50 - n, held=True) for n in range(1, 4))
    assert found(*held) == []  # an approval or a concurrency group holds them
    ghost = queued(9, 20 * 60)  # queued since yesterday: not within the window of the rest
    assert found(ghost, *waiting[1:]) == []


def test_not_an_outage() -> None:
    assert found(BEFORE, refused(1, 50), refused(1, 40), refused(1, 30)) == []  # one PR
    assert found(BEFORE, *THREE, rule=CiEconomy(outage_min_prs=0)) == []
    started = Job("ci-ok", FAILED)  # failed after it started: a failing job, not refused
    assert found(BEFORE, *(refused(n, 30 + n, jobs=(started,)) for n in range(3))) == []
    unread = (refused(n, 30 + n, jobs=None) for n in range(3))
    assert found(BEFORE, *unread) == []  # jobs not read: only queued builds take part


def test_refused_jobs_do_not_feed_the_failing_jobs_outage() -> None:
    builds = [refused(n, 30 + n) for n in range(8)]
    [alert] = found(BEFORE, *builds)  # many PRs: still only the not-started outage
    assert alert.state == STATE
    assert REFUSED.refused and not REFUSED.failed


def test_switched_on_it_follows_the_rule_s_mode() -> None:
    [alert] = found(BEFORE, *THREE, rule=CiEconomy(not_started_mode="rule"))
    assert not alert.observe_only


def test_config() -> None:
    rule = build({"queued_after": "10m", "not_started_mode": "rule"})
    assert rule.queued_after == 10 * M and rule.not_started_mode == "rule"
    assert CiEconomy().queued_after == 30 * M and CiEconomy().not_started_mode == "observe"
    with pytest.raises(ValueError, match="not_started_mode"):
        build({"not_started_mode": "act"})
    with pytest.raises(ValueError, match="queued_after"):
        build({"queued_after": "0s"})
