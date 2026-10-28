"""Rule 9, CI jobs that never start: one outage (#229, incident 20)."""

from __future__ import annotations

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


def refused(n: int, minutes_ago: float, *, repo: str = "example/shop", **fields: Any) -> Build:
    at = NOW - minutes_ago * M
    base: dict[str, Any] = {
        "repo": repo,
        "workflow": "ci.yml",
        "branch": f"task/T-{n}",
        "sha": f"sha-{n}-{minutes_ago:g}",
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


def ran(minutes_ago: float, *, repo: str = "example/shop") -> Build:
    base = {"status": PASSED, "branch": "main", "pr": None, "jobs": (RAN,)}
    return refused(0, minutes_ago, repo=repo, **base)


def found(*builds: Build, rule: CiEconomy | None = None) -> list[Action]:
    snapshot = Snapshot(now=NOW, builds=builds)
    actions = (rule or CiEconomy()).evaluate(snapshot)
    return [a for a in actions if is_outage_state(a.state or "")]


THREE = (refused(1, 50), refused(2, 40), refused(3, 30))


def test_three_prs_whose_ci_was_refused_are_one_outage() -> None:
    [alert] = found(*THREE)
    outage = Outage.from_params(alert.params)
    assert outage is not None and outage.kind == CI_NOT_STARTED
    assert alert.state == alert.key == outage.state == STATE
    assert outage.prs == ("example/shop#1", "example/shop#2", "example/shop#3")
    assert alert.since == NOW - 50 * M and alert.observe_only
    assert "billing and spending limits" in (outage.cause or "")
    assert "CI jobs not starting since 2026-03-02 11:10 UTC: 3 change(s)" in alert.reason
    assert alert.params["headline"] == "CI jobs not starting: 3 changes waiting"


def test_runs_left_queued_count_once_queued_long_enough() -> None:
    [alert] = found(refused(1, 50), queued(2, 40), queued(3, 31))
    cause = Outage.from_params(alert.params).cause  # type: ignore[union-attr]
    assert cause is not None and "2 run(s) still queued" in cause and "1 run(s)" in cause
    assert found(refused(1, 50), queued(2, 40), queued(3, 29)) == []  # not 30 min yet


def test_not_an_outage() -> None:
    assert found(*THREE[:2]) == []  # two PRs
    assert found(refused(1, 50), refused(1, 40), refused(1, 30)) == []  # one PR, retried
    assert found(refused(1, 130), *THREE[1:]) == []  # the first left the 2h window
    assert found(*THREE, rule=CiEconomy(outage_min_prs=0)) == []
    started = Job("ci-ok", FAILED)  # failed after it started: a failing job, not refused
    assert found(*(refused(n, 30 + n, jobs=(started,)) for n in range(3))) == []
    unread = (refused(n, 30 + n, jobs=None) for n in range(3))
    assert found(*unread) == []  # jobs not read: only queued builds take part


def test_a_repository_whose_ci_ran_again_since_leaves_it() -> None:
    assert found(*THREE, ran(20)) == []
    assert found(*THREE, ran(20, repo="example/docs")) != []  # another repository's
    [alert] = found(*THREE, ran(60))  # ran before they were refused
    assert len(Outage.from_params(alert.params).prs) == 3  # type: ignore[union-attr]
    rule = CiEconomy()
    assert rule.resolved(STATE, Snapshot(now=NOW, builds=(*THREE, ran(20)))) == (
        "CI started jobs again: B-0-20 in example/shop ran at 2026-03-02 11:40 UTC."
    )
    assert rule.resolved(STATE, Snapshot(now=NOW, builds=THREE)) is None


def test_refused_jobs_do_not_feed_the_failing_jobs_outage() -> None:
    builds = [refused(n, 30 + n, jobs=(Job("build", FAILED, started=False),)) for n in range(8)]
    [alert] = found(*builds)  # many PRs: still only the not-started outage
    assert alert.state == STATE
    assert Job("build", FAILED, started=False).refused
    assert not Job("build", FAILED, started=False).failed


def test_switched_on_it_follows_the_rule_s_mode() -> None:
    [alert] = found(*THREE, rule=CiEconomy(not_started_mode="rule"))
    assert not alert.observe_only


def test_config() -> None:
    rule = build({"queued_after": "10m", "not_started_mode": "rule"})
    assert rule.queued_after == 10 * M and rule.not_started_mode == "rule"
    assert CiEconomy().queued_after == 30 * M and CiEconomy().not_started_mode == "observe"
    with pytest.raises(ValueError, match="not_started_mode"):
        build({"not_started_mode": "act"})
    with pytest.raises(ValueError, match="queued_after"):
        build({"queued_after": "0s"})
