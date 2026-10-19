"""Rule 9, part 4: a CI outage is one incident (incident 20)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action
from kelpie.core.incidents import CiOutage, is_outage_state, outage_state_id
from kelpie.core.model import (
    Block,
    Build,
    BuildStatus,
    Condition,
    ConditionKind,
    Dependency,
    Job,
    PullRequest,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules.ci_economy import CiEconomy, build

NOW = datetime(2026, 3, 2, 9, tzinfo=UTC)
M = timedelta(minutes=1)
FAILED, PASSED = BuildStatus.FAILURE, BuildStatus.SUCCESS
BROKEN = Job("build", FAILED, step="Select toolchain", runner="runner-1", labels=("self-hosted",))


def red(n: int, minutes_ago: float, *, job: Job = BROKEN, **fields: Any) -> Build:
    at = NOW - minutes_ago * M
    base: dict[str, Any] = {
        "repo": "example/app",
        "workflow": "ci.yml",
        "branch": f"task/T-{n}",
        "sha": f"sha-{n}-{minutes_ago:g}",
        "pr": n,
        "status": FAILED,
        "started_at": at - 2 * M,
        "finished_at": at,
        "jobs": (job,),
    }
    return Build(f"B-{n}-{minutes_ago:g}", **{**base, **fields})


def green(minutes_ago: float, **fields: Any) -> Build:
    base: dict[str, Any] = {"status": PASSED, "branch": "main", "pr": None, "sha": "base"}
    return red(0, minutes_ago, **{**base, "jobs": (Job("build", PASSED),), **fields})


# The evidence that the environment broke: main's last green commit fails when re-run.
BEFORE, RERUN = green(400), red(0, 280, branch="main", pr=None, sha="base")


def outages(*builds: Build, rule: CiEconomy | None = None, **snap: Any) -> list[Action]:
    snapshot = Snapshot(now=NOW, builds=builds, **snap)
    return [a for a in (rule or CiEconomy()).evaluate(snapshot) if is_outage_state(a.state or "")]


THREE = (red(1, 300), red(2, 250), red(3, 240))


def test_prs_failing_the_same_way_after_a_green_commit_broke_are_one_outage() -> None:
    [alert] = outages(BEFORE, RERUN, *THREE, red(1, 30))
    outage = CiOutage.from_params(alert.params)
    assert outage is not None and alert.state == alert.key == outage.state
    assert alert.since == NOW - 300 * M
    assert outage.state == outage_state_id("ci.yml", "build", "Select toolchain")
    assert outage.prs == ("example/app#1", "example/app#2", "example/app#3")
    assert outage.runners == ("runner-1", "self-hosted")
    assert outage.builds[0] == "B-1-30"  # newest first
    assert "CI outage since 2026-03-02 04:00 UTC: 4 change(s) in example/app" in alert.reason
    assert "the CI (toolchain, runner, service) or the base they build on" in alert.reason


def test_the_re_run_that_is_the_evidence_does_not_count_toward_the_prs() -> None:
    """A flaky nightly re-run and two red PRs are not three changes failing the same way."""
    assert outages(BEFORE, RERUN, red(1, 300), red(2, 250)) == []


def test_two_repositories_failing_on_the_same_runner_are_evidence_enough() -> None:
    other = red(9, 200, repo="example/other")
    assert outages(red(1, 300), red(2, 250), other) != []
    elsewhere = Job("build", FAILED, step="Select toolchain", runner="hosted-7")
    assert outages(red(1, 300), red(2, 250), red(9, 200, repo="example/other", job=elsewhere)) == []
    no_repo = red(9, 200, repo=None)
    assert outages(red(1, 300), red(2, 250), no_repo) == []  # unknown repository: not a second


def test_without_evidence_it_takes_twice_the_prs() -> None:
    """Agents' PRs red at their own tests within the hour are not an outage."""
    tests = Job("test", FAILED, step="Run tests")
    three = [red(n, 300 - 30 * n, job=tests) for n in (1, 2, 3)]
    assert outages(*three) == []
    six = [red(n, 300 - 10 * n, job=tests) for n in range(1, 7)]
    [alert] = outages(*six)
    assert "6 change(s)" in alert.reason


def test_a_slow_farm_whose_prs_stay_red_is_an_outage() -> None:
    """PRs that started failing hours apart and are retried hourly: red together all night."""
    starts = ((1, 600), (2, 420), (3, 240))
    retried = [red(n, t) for n, first in starts for t in range(first, 0, -60)]
    old_rerun = red(0, 700, branch="main", pr=None, sha="base")  # long before the PRs
    [alert] = outages(green(800), old_rerun, *retried)
    assert alert.since == NOW - 600 * M  # the earliest active failure of the group
    assert outages(*retried) == []  # without evidence: three PRs are not enough


def test_not_an_outage() -> None:
    evidence = (BEFORE, RERUN)
    assert outages(*evidence, red(1, 300), red(2, 250)) == []  # two PRs
    assert outages(*evidence, red(1, 270), red(1, 200), red(1, 100)) == []  # one PR, often
    assert outages(*evidence, red(1, 300), red(2, 150), red(3, 20)) == []  # spread out
    other = Job("build", FAILED, step="Run tests")
    assert outages(*evidence, *THREE[:2], red(3, 240, job=other)) == []  # another step
    assert outages(*evidence, *THREE[:2], red(3, 240, workflow="lint.yml")) == []
    assert outages(*evidence, *THREE[:2], red(3, 240, jobs=None)) == []  # jobs not read
    cancelled = Job("build", BuildStatus.CANCELLED, step="Select toolchain")
    assert outages(*evidence, *THREE[:2], red(3, 240, job=cancelled)) == []
    off = CiEconomy(outage_min_prs=0)
    assert outages(*evidence, *THREE, rule=off) == []


def test_a_pass_ends_it_in_its_repository_only() -> None:
    reds = (BEFORE, RERUN, *THREE)
    assert outages(*reds, green(100)) == []
    assert outages(*reds, green(100, repo="example/other")) != []  # another repo's runner
    # An old failure elsewhere does not let that repository's passes end this outage.
    elsewhere = red(9, 960, repo="example/other")
    assert outages(*reds, elsewhere, green(100, repo="example/other")) != []
    after = (red(4, 60), red(5, 50), red(6, 40, repo="example/x"))
    [again] = outages(*reds, green(100), *after)
    assert again.since == NOW - 60 * M  # failures after the pass start over


def test_a_wider_window_and_fewer_prs_are_configurable() -> None:
    rule = build({"outage_min_prs": 2, "outage_window": "6h"})
    assert outages(red(1, 300), red(2, 10, repo="example/other"), rule=rule) != []
    for bad in (1, -1, True, 2.5, 1001):
        with pytest.raises(ValueError, match="outage_min_prs"):
            build({"outage_min_prs": bad})
    with pytest.raises(ValueError, match="outage_window"):
        build({"outage_window": "0s"})


def task(tid: str, status: Status, since_minutes: float = 600, **kw: Any) -> Task:
    return Task(tid, tid, status, NOW - since_minutes * M, **kw)


def test_the_outage_explains_the_tasks_waiting_on_its_prs_ci_or_merge() -> None:
    on_merge = Block("waiting for the merge", NOW - 280 * M, condition_id="C-1")
    asks = Block("needs_input: which API?", NOW - 900 * M, condition_id="C-4")
    tasks = (
        task("T-1", Status.BLOCKED, 280),  # PR #1's own task, no condition, since the outage
        task("T-2", Status.IN_PROGRESS),  # its task works on: not explained
        task("T-3", Status.BLOCKED, 900),  # blocked without a condition long before
        task("T-4", Status.BLOCKED, 900, block=asks),  # its PR is red, but it asks a person
        task("T-5", Status.WAITING),  # its own PR's CI, the condition naming the PR by URL
        task("R-1", Status.BLOCKED, block=on_merge),  # a reviewer waits for #1 to merge
        task("R-2", Status.WAITING),  # waits for CI of a PR outside the outage
        task("R-3", Status.WAITING),  # its condition holds already
        task("T-6", Status.BLOCKED, 280),  # its PR is red, but it waits on another repo's PR
        task("T-7", Status.BLOCKED, 280),  # its PR is red, but it waits for task D-1
        task("D-1", Status.IN_PROGRESS),
    )
    deps = (Dependency("T-7", "D-1"),)
    conditions = (
        Condition("C-1", ConditionKind.PR_MERGED, "R-1", {"repo": "Example/App", "pr": "1"}),
        Condition("C-2", ConditionKind.CI_STATUS, "R-2", {"repo": "example/app", "pr": 9}),
        Condition(
            "C-3", ConditionKind.CI_STATUS, "R-3", {"repo": "example/app", "pr": 2}, satisfied=True
        ),
        Condition("C-4", ConditionKind.HUMAN_TASK, "T-4", {"task": "H-1"}),
        Condition("C-5", ConditionKind.CI_STATUS, "T-5", {"ref": "https://example/pull/5"}),
        Condition("C-6", ConditionKind.PR_MERGED, "T-6", {"repo": "example/lib", "pr": 9}),
    )
    pulls = (
        PullRequest("example/app", 1, branch="task/T-1", task_ids=("T-1",)),
        PullRequest("example/app", 2, branch="task/T-2", task_ids=("T-2",)),
        PullRequest("example/app", 3, branch="task/T-3", task_ids=("T-3", "T-4", "T-5")),
        PullRequest("example/app", 4, branch="task/T-3", fork=True),  # a fork's branch
        PullRequest("example/app", 5, branch="task/T-5", task_ids=("T-6", "T-7")),
    )
    reds = (BEFORE, RERUN, red(1, 300), red(2, 250), red(3, 200, pr=None), red(5, 230))
    [alert] = outages(
        *reds, tasks=tasks, conditions=conditions, pull_requests=pulls, dependencies=deps
    )
    outage = CiOutage.from_params(alert.params)
    assert outage is not None and outage.tasks == ("R-1", "T-1", "T-5")
    assert outage.prs == ("example/app#1", "example/app#2", "example/app#3", "example/app#5")
    assert "3 task(s) wait on it: R-1, T-1, T-5." in alert.reason


def test_closed_prs_are_left_out_when_the_code_host_listed_them_all() -> None:
    reds = (BEFORE, RERUN, *THREE)
    open_only = (PullRequest("example/app", 1, branch="task/T-1"),)
    [alert] = outages(*reds, pull_requests=open_only, pull_requests_complete=True)
    assert CiOutage.from_params(alert.params).prs == ("example/app#1",)  # type: ignore[union-attr]
    [unsure] = outages(*reds, pull_requests=open_only)
    assert CiOutage.from_params(unsure.params).prs == (  # type: ignore[union-attr]
        "example/app#1",
        "example/app#2",
        "example/app#3",
    )


def test_the_resolved_notice_says_where_the_job_passed_again() -> None:
    rule = CiEconomy()
    [alert] = outages(BEFORE, RERUN, *THREE)
    assert alert.state is not None
    later = Snapshot(now=NOW, builds=(BEFORE, RERUN, *THREE, green(20)))
    assert rule.resolved(alert.state, later) == (
        "CI outage over: ci.yml > build passed again in example/app at 2026-03-02 08:40 UTC "
        "(B-0-20)."
    )
    # Only a pass in the outage's repositories says it is over.
    stale = Snapshot(now=NOW, builds=(BEFORE, RERUN, *THREE, green(20, repo="example/other")))
    assert rule.resolved(alert.state, stale) is None
    assert rule.resolved(alert.state, Snapshot(now=NOW, builds=(green(20),))) is None  # gone
    lone = Snapshot(now=NOW, builds=(BEFORE, red(1, 300), green(20)))
    assert rule.resolved(alert.state, lone) is None  # not an outage in what is left in sight
    assert rule.resolved("ci_economy:waste", later) is None
    assert CiEconomy(outage_min_prs=0).resolved(alert.state, later) is None


def test_the_scope_is_the_outage_s_repositories_not_every_red_one() -> None:
    """A web PR red at the same generic step for its own reasons stays its own business."""
    own = Job("build", FAILED, step="Select toolchain", runner="hosted-x")
    web = red(5, 200, repo="example/web", job=own)
    mate = red(6, 150, repo="example/lib")  # fails the same way on the outage's runner
    [alert] = outages(BEFORE, RERUN, *THREE, web, mate)
    outage = CiOutage.from_params(alert.params)
    assert outage is not None and outage.repos == ("example/app", "example/lib")
    assert "hosted-x" not in outage.runners and "example/web#5" not in outage.prs


def test_a_stale_re_run_is_no_evidence_for_today() -> None:
    """A flaky nightly hours before three PRs red at their own tests proves nothing."""
    tests = Job("test", FAILED, step="Run tests")
    nightly = red(0, 400, branch="main", pr=None, sha="base", job=tests)
    green_base = green(500, jobs=(Job("test", PASSED),))
    prs = [red(n, 100 - 10 * n, job=tests) for n in (1, 2, 3)]
    assert outages(green_base, nightly, *prs) == []


def test_one_change_red_since_yesterday_does_not_move_the_start() -> None:
    # Red since yesterday in a quiet repository on the same runner, retried in the window.
    old, retry = red(9, 1300, repo="example/lib"), red(9, 260, repo="example/lib")
    [alert] = outages(BEFORE, RERUN, *THREE, old, retry)
    assert alert.since == NOW - 300 * M


def test_the_resolved_notice_never_cites_another_repository_s_pass() -> None:
    rule = CiEconomy()
    [alert] = outages(BEFORE, RERUN, *THREE)
    assert alert.state is not None
    # Another repository went red and green during the outage, on its own runner; the
    # app stayed red until it passed at 08:40.
    hosted = Job("build", FAILED, step="Select toolchain")
    x_red, x_green = red(8, 110, repo="example/x", job=hosted), green(100, repo="example/x")
    app = (red(1, 90), red(2, 80), green(20))
    for earlier in ((), (red(8, 1400, repo="example/web"), green(1340, repo="example/web"))):
        builds = (*earlier, BEFORE, RERUN, *THREE, x_red, x_green, *app)
        notice = rule.resolved(alert.state, Snapshot(now=NOW, builds=builds))
        assert notice is not None and "in example/app at 2026-03-02 08:40 UTC" in notice


def test_the_resolved_notice_ignores_red_prs_fixed_before_the_outage() -> None:
    """Six unrelated PRs red then fixed one after another just before the outage: their
    passes are not its end."""
    rule = CiEconomy()
    hosted = Job("build", FAILED, step="Select toolchain")
    fixed = [
        b
        for n in range(1, 7)
        for b in (
            red(n, 400 - 5 * n, repo=f"example/r{n}", job=hosted),
            green(392 - 5 * n, repo=f"example/r{n}"),
        )
    ]
    outage = (BEFORE, RERUN, *THREE, red(1, 120), red(2, 60))
    [alert] = outages(*fixed, *outage)
    assert alert.state is not None
    later = Snapshot(now=NOW, builds=(*fixed, *outage, green(20)))
    notice = rule.resolved(alert.state, later)
    assert notice is not None and "in example/app at 2026-03-02 08:40 UTC" in notice


def test_a_re_run_pr_still_counts_toward_the_unsure_threshold() -> None:
    """Six repositories red, two of them re-running a PR commit that passed: an outage."""
    hosted = Job("build", FAILED, step="Select toolchain")
    six = [red(n, 300 - 5 * n, repo=f"example/r{n}", job=hosted, sha=f"s{n}") for n in range(1, 7)]
    passed = [green(400, repo=f"example/r{n}", branch=f"task/T-{n}", sha=f"s{n}") for n in (1, 2)]
    assert outages(*passed, *six) != []


def test_evidence_never_holds_back_what_opens_without_it() -> None:
    hosted = Job("build", FAILED, step="Select toolchain")  # no machine name
    six = [red(n, 300 - 5 * n, repo=f"example/r{n}", job=hosted) for n in range(1, 7)]
    assert outages(*six) != []  # six repositories, no evidence
    rerun_r1 = red(0, 290, repo="example/r1", branch="main", pr=None, sha="base", job=hosted)
    passed_r1 = green(400, repo="example/r1")
    assert outages(passed_r1, rerun_r1, *six) != []  # with a re-run in one of them
    mac = Job("build", FAILED, step="Select toolchain", runner="mac-1")
    two = [red(n, 280, repo=f"example/m{n}", job=mac) for n in (7, 8)]
    assert outages(*six, *two) != []  # with two of them on a shared runner


def test_the_start_is_the_outage_s_not_an_excluded_repository_s() -> None:
    web = Job("build", FAILED, step="Select toolchain", runner="hosted-x")
    early = red(5, 360, repo="example/web", job=web)  # red for its own reasons, earlier
    [alert] = outages(BEFORE, RERUN, *THREE, early)
    assert alert.since == NOW - 300 * M


def test_a_condition_naming_another_repository_is_not_the_own_pr_s() -> None:
    lib = Condition(
        "C-1", ConditionKind.CI_STATUS, "T-1", {"repo": "example/lib", "branch": "main"}
    )
    same = Condition("C-2", ConditionKind.CI_STATUS, "T-2", {"repo": "example/app"})
    tasks = (task("T-1", Status.WAITING), task("T-2", Status.WAITING))
    pulls = (
        PullRequest("example/app", 1, branch="task/T-1", task_ids=("T-1",)),
        PullRequest("example/app", 2, branch="task/T-2", task_ids=("T-2",)),
    )
    [alert] = outages(
        BEFORE, RERUN, *THREE, tasks=tasks, conditions=(lib, same), pull_requests=pulls
    )
    assert CiOutage.from_params(alert.params).tasks == ("T-2",)  # type: ignore[union-attr]


def test_with_the_engine_the_outage_is_one_alert_for_its_tasks() -> None:
    """Incident 20 end to end: rule 9 and the per-task rules on the fixture's farm, through
    the engine. One outage alert; the blocks and waits it explains are folded under it; the
    unrelated web task still gets its own alert."""
    from pathlib import Path

    from kelpie.core.config import EngineConfig, Mode
    from kelpie.core.engine import Engine
    from kelpie.core.incidents import FOLDED_UNDER
    from kelpie.core.ledger import Outcome, Suppression
    from kelpie.rules import REGISTRY
    from tests.fakes import FakeAdapter, FakeAlerter
    from tests.incidents import load

    fixture = Path(__file__).parents[1] / "fixtures/incidents/20-ci-outage.yaml"
    snapshot = load(fixture).snapshot
    rules = [
        REGISTRY["ci_economy"]({"outage_min_prs": 3, "outage_window": "2h"}),
        REGISTRY["stale_blocks"]({"unlinked_alert_after": "1h", "max_block": "2h"}),
        REGISTRY["wait_condition"]({"max_wait": "2h"}),
    ]
    alerter = FakeAlerter()
    config = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
    engine = Engine(rules, adapters=[FakeAdapter()], alerter=alerter, config=config)  # type: ignore[arg-type]
    done = engine.tick(lambda: snapshot)
    [outage] = [d for d in done if is_outage_state(d.action.state or "")]
    assert outage.outcome is Outcome.ALERTED
    folded = {d.action.task_id for d in done if d.suppression is Suppression.INCIDENT}
    assert folded == {"T-201", "T-202", "T-210"}
    assert all(
        d.action.params[FOLDED_UNDER] == outage.action.state
        for d in done
        if d.suppression is Suppression.INCIDENT
    )
    alerted = {d.action.task_id for d in done if d.outcome is Outcome.ALERTED}
    assert {"T-301", "T-204"} <= alerted and not alerted & folded  # T-204 asks a person
