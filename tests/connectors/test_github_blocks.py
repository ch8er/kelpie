"""Block reasons turned into conditions during snapshot assembly, and rule 4 on the result."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.connectors.github.blocks import (
    MAX_READY_REFS,
    MAX_TASK_TEXT,
    ConditionSync,
    load_block_conditions,
)
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.core.actions import ActionKind
from kelpie.core.executors import wait_conditions
from kelpie.core.model import Block, Condition, ConditionKind, Snapshot, Status, Task
from kelpie.rules.dispatch_gate import DispatchGate
from kelpie.rules.stale_blocks import StaleBlocks
from kelpie.rules.wait_condition import WaitCondition
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, Recorder, serve
from tests.incidents import load

REPO = "example/repo"
SHA = "c" * 40
NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
PATTERNS = [
    {"pattern": r"waiting for ci/merge of (?P<pr>\S+)", "kind": "pr_merged", "repo": REPO},
    {"pattern": r"waiting for green ci on (?P<pr>\S+)", "kind": "ci_green", "repo": REPO},
]


def blocked(task_id: str, reason: str, since: datetime, **block: str) -> Task:
    return Task(
        task_id,
        "Review change",
        Status.BLOCKED,
        since,
        role="reviewer",
        block=Block(reason, since, **block),
    )


def sync(
    gh: FakeGitHub, rec: Recorder | None = None, clock: Clock | None = None, **kw: object
) -> ConditionSync:
    connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=clock or Clock(NOW)), [REPO])
    return ConditionSync(connector, load_block_conditions(PATTERNS, [REPO]), rec, **kw)  # type: ignore[arg-type]


# --- config ---------------------------------------------------------------------------


def test_load_patterns() -> None:
    (p, q) = load_block_conditions(PATTERNS, [REPO])
    assert p.kind == "pr_merged" and p.condition_kind is ConditionKind.PR_MERGED
    assert q.condition_kind is ConditionKind.CI_STATUS
    assert p.pattern.search("WAITING FOR CI/MERGE OF #1")  # case-insensitive
    assert load_block_conditions(None, None) == ()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"pattern": "x"}, "array of tables"),
        ([{"pattern": "(?P<pr>", "kind": "pr_merged"}], "does not compile"),
        ([{"pattern": "waiting", "kind": "pr_merged"}], "named group"),
        ([{"pattern": "(?P<pr>.+)", "kind": "deployed"}], "kind must be"),
        ([{"pattern": "(?P<pr>.+)", "kind": "pr_merged", "repo": "nope"}], "owner/name"),
        ([{"pattern": "(?P<pr>.+)", "kind": "pr_merged", "repo": "other/repo"}], "not in"),
        ([{"pattern": "(?P<pr>.+)", "kind": "pr_merged", "extra": 1}], "unknown keys"),
        ([{"pattern": " ", "kind": "pr_merged"}], "non-empty"),
    ],
)
def test_pattern_validation(raw: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_block_conditions(raw, [REPO])


def test_patterns_need_the_connector() -> None:
    with pytest.raises(ValueError, match=r"connectors\.github"):
        load_block_conditions([{"pattern": "(?P<pr>.+)", "kind": "pr_merged"}], None)


# --- attaching --------------------------------------------------------------------------


def test_matched_block_gets_a_linked_condition() -> None:
    rec = Recorder()
    since = NOW - timedelta(hours=3)
    task = blocked("T-1", "Waiting for CI/merge of #123", since)
    with serve() as gh:
        gh.pull(REPO, 123)
        out = sync(gh, rec)(Snapshot(now=NOW, tasks=(task,)))
    (t,) = out.tasks
    (c,) = out.conditions
    assert (
        t.block is not None
        and t.block.condition_id == c.id == "block:T-1:pr_merged:example/repo#123"
    )
    assert c.kind is ConditionKind.PR_MERGED and c.task_id == "T-1"
    assert c.registered_at == since and c.checked_at == NOW and c.satisfied is False
    assert c.params["source"] == "block_reason"
    assert rec.blocks == {"attached": 1}
    # Rule 4: waiting on a pending condition, no "block without condition" alert.
    assert StaleBlocks().evaluate(out) == []


def test_merged_pr_lets_rule_4_unblock() -> None:
    task = blocked("T-1", "waiting for CI/merge of #123", NOW - timedelta(hours=3))
    with serve() as gh:
        gh.pull(REPO, 123, state="closed", merged=True, merged_at="2026-03-02T11:00:00Z")
        out = sync(gh)(Snapshot(now=NOW, tasks=(task,)))
    (action,) = StaleBlocks().evaluate(out)
    assert action.kind is ActionKind.UNBLOCK and action.task_id == "T-1"


def test_ci_green_variant() -> None:
    with serve() as gh:
        # PR links are accepted on the web host of the API (here the fake's own host).
        task = blocked("T-2", f"waiting for green CI on {gh.url}/example/repo/pull/9", NOW)
        gh.pull(REPO, 9, sha=SHA)
        gh.ci(REPO, SHA, statuses=[{"state": "success", "updated_at": "2026-03-02T12:00:00Z"}])
        out = sync(gh)(Snapshot(now=NOW + timedelta(minutes=10), tasks=(task,)))
    (c,) = out.conditions
    assert c.kind is ConditionKind.CI_STATUS and c.satisfied is True
    assert c.params["expect"] == "success"


def test_unresolved_refs_leave_the_block_alone() -> None:
    rec = Recorder()
    tasks = (
        blocked("T-1", "waiting for CI/merge of #404", NOW),  # GitHub: no such PR
        blocked("T-2", "waiting for CI/merge of other/repo#5", NOW),  # repo not allowed
        blocked("T-3", "waiting for CI/merge of soon", NOW),  # not a PR ref
    )
    with serve() as gh:
        out = sync(gh, rec)(Snapshot(now=NOW, tasks=tasks))
        assert gh.paths() == ["/repos/example/repo/pulls/404"]
    assert out.tasks == tasks and out.conditions == ()
    assert rec.blocks == {"unresolved": 3}


def test_unconfirmed_pr_is_not_attached() -> None:
    rec = Recorder()
    task = blocked("T-1", "waiting for CI/merge of #1", NOW)
    with serve() as gh:
        s = sync(gh, rec)
    out = s(Snapshot(now=NOW, tasks=(task,)))  # the server is gone: never confirmed
    assert out.tasks == (task,) and out.conditions == ()
    assert rec.blocks == {"unconfirmed": 1}


def test_confirmed_pr_keeps_its_condition_when_github_fails() -> None:
    clock = Clock(NOW)
    task = blocked("T-1", "waiting for CI/merge of #1", NOW - timedelta(hours=1))
    with serve() as gh:
        gh.pull(REPO, 1)
        s = sync(gh, clock=clock)
        first = s(Snapshot(now=clock.now, tasks=(task,)))
        clock.advance(seconds=61)
        gh.reply(f"/repos/{REPO}/pulls/1", {"m": "down"}, status=502)
        second = s(Snapshot(now=clock.now, tasks=(task,)))
    (c1,), (c2,) = first.conditions, second.conditions
    assert c1.id == c2.id and second.tasks[0].block.condition_id == c2.id  # type: ignore[union-attr]
    assert c2.satisfied is False and c2.checked_at == NOW  # the last answer, with its age


def test_bad_block_reasons_do_not_sink_the_step(monkeypatch: pytest.MonkeyPatch) -> None:
    import kelpie.connectors.github.blocks as blocks

    real = blocks.parse_pr_ref

    def parse(text: str, repo: str | None, host: str) -> object:
        if text == "#666":
            raise RuntimeError("unexpected")
        return real(text, repo, host)

    monkeypatch.setattr(blocks, "parse_pr_ref", parse)
    rec = Recorder()
    reasons = [
        "waiting for CI/merge of https://[oops/pull/1",
        "waiting for CI/merge of https://exa．mple/example/repo/pull/2",  # noqa: RUF001 (deliberate)
        "waiting for CI/merge of #١٢",  # Arabic-Indic digits
        "waiting for CI/merge of ²",
        "waiting for CI/merge of #666",
        "waiting for CI/merge of #10",
    ]
    tasks = tuple(blocked(f"T-{i}", r, NOW) for i, r in enumerate(reasons))
    with serve() as gh:
        gh.pull(REPO, 10)
        out = sync(gh, rec)(Snapshot(now=NOW, tasks=tasks))
        assert gh.paths() == [f"/repos/{REPO}/pulls/10"]
    (c,) = out.conditions
    assert c.task_id == "T-5"
    assert rec.blocks == {"attached": 1, "unresolved": 4, "error": 1}


def test_revoked_token_costs_one_request_per_backoff() -> None:
    clock = Clock(NOW)
    tasks = tuple(blocked(f"T-{i}", f"waiting for CI/merge of #{i + 1}", NOW) for i in range(50))
    with serve() as gh:
        for i in range(50):
            gh.reply(f"/repos/{REPO}/pulls/{i + 1}", {"message": "Bad credentials"}, status=401)
        s = sync(gh, clock=clock)
        s(Snapshot(now=clock.now, tasks=tasks))
        assert len(gh.requests) == 1  # 401: back off, every other lookup waits
        clock.advance(seconds=30)
        s(Snapshot(now=clock.now, tasks=tasks))
        assert len(gh.requests) == 1
        clock.advance(seconds=31)
        s(Snapshot(now=clock.now, tasks=tasks))
        assert len(gh.requests) == 2


def test_tick_request_budget() -> None:
    rec = Recorder()
    tasks = tuple(blocked(f"T-{i}", f"waiting for CI/merge of #{i + 1}", NOW) for i in range(10))
    with serve() as gh:
        for i in range(10):
            gh.pull(REPO, i + 1)
        s = sync(gh, rec, max_requests=3)
        out = s(Snapshot(now=NOW, tasks=tasks))
        assert len(gh.requests) == 3
    assert len(out.conditions) == 3
    assert rec.blocks == {"attached": 3, "unconfirmed": 7}


def test_demand_over_budget_confirms_everything_and_keeps_answers_fresh(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """40 PRs, 10 requests a tick: new PRs get the budget first, then the oldest answers."""
    clock = Clock(NOW)
    tasks = tuple(blocked(f"T-{i}", f"waiting for CI/merge of #{i + 1}", NOW) for i in range(40))
    attached: list[int] = []
    with serve() as gh:
        for i in range(40):
            gh.pull(REPO, i + 1)
        s = sync(gh, clock=clock, max_requests=10)
        for _ in range(20):
            out = s(Snapshot(now=clock.now, tasks=tasks))
            attached.append(len(out.conditions))
            oldest = max(clock.now - c.checked_at for c in out.conditions)  # type: ignore[operator]
            assert oldest < timedelta(minutes=15)  # within rule 4's freshness window
            clock.advance(seconds=61)  # every cached answer is due again
    assert attached[:4] == [10, 20, 30, 40]  # unconfirmed PRs first, ten a tick
    assert attached[-1] == 40
    assert oldest <= timedelta(minutes=4, seconds=5)  # fair rotation: 4 ticks per round
    assert "budget ran out" in caplog.text


def test_tick_time_budget() -> None:
    tasks = tuple(blocked(f"T-{i}", f"waiting for CI/merge of #{i + 1}", NOW) for i in range(3))
    with serve() as gh:
        s = sync(gh, tick_budget=timedelta(0))
        out = s(Snapshot(now=NOW, tasks=tasks))
        assert gh.requests == []
    assert out.tasks == tasks


def test_attached_condition_is_a_declared_wait_for_rule_1() -> None:
    """Rule 1 requeues a block with a reason it does not allow and no condition; once the
    block carries the condition attached from its reason, the wait is declared."""
    from kelpie.core.model import Session
    from kelpie.rules.exit_contract import ExitContract

    since = NOW - timedelta(minutes=30)
    task = blocked("T-1", "waiting for CI/merge of #5", since)
    session = Session("S-1", "T-1", "reviewer-1", NOW - timedelta(hours=1), ended_at=since)
    raw = Snapshot(now=NOW, tasks=(task,), sessions=(session,))
    rule = ExitContract()
    assert [a.kind for a in rule.evaluate(raw)] == [ActionKind.REQUEUE]
    with serve() as gh:
        gh.pull(REPO, 5)
        enriched = sync(gh)(raw)
    assert rule.evaluate(enriched) == []


def test_unmatched_and_already_linked_blocks_are_untouched() -> None:
    since = NOW - timedelta(hours=13)
    linked = Condition("C-9", ConditionKind.CUSTOM, task_id="T-3", registered_at=since)
    tasks = (
        blocked("T-1", "needs a design decision", since),
        blocked("T-2", "waiting for CI/merge of #1", since, condition_id="C-8"),
        blocked("T-3", "waiting for CI/merge of #1", since),
        Task("T-4", "x", Status.IN_PROGRESS, since),
    )
    with serve() as gh:
        out = sync(gh)(Snapshot(now=NOW, tasks=tasks, conditions=(linked,)))
        assert gh.requests == []
    assert out.tasks == tasks
    actions = StaleBlocks().evaluate(out)
    assert any(a.task_id == "T-1" and "no condition" in a.reason for a in actions)


def test_registered_github_conditions_are_evaluated() -> None:
    registered = Condition("C-1", ConditionKind.PR_MERGED, "T-1", {"repo": REPO, "pr": 3})
    other = Condition("C-2", ConditionKind.CUSTOM, "T-1")
    with serve() as gh:
        gh.pull(REPO, 3, merged=True, merged_at="2026-03-02T11:00:00Z")
        connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=Clock(NOW)), [REPO])
        out = ConditionSync(connector)(Snapshot(now=NOW, conditions=(registered, other)))
    assert out.conditions[0].satisfied is True and out.conditions[1] == other


def test_one_request_per_pr_per_tick() -> None:
    tasks = tuple(blocked(f"T-{i}", "waiting for CI/merge of #7", NOW) for i in range(5))
    with serve() as gh:
        gh.pull(REPO, 7)
        out = sync(gh)(Snapshot(now=NOW, tasks=tasks))
        assert len(gh.requests) == 1
    assert len({c.id for c in out.conditions}) == 5


def test_attachment_produces_the_fixture_shape() -> None:
    """The incident fixtures hold snapshots after attachment; rebuild one from raw blocks."""
    fixture = load(Path("tests/fixtures/incidents/10-block-waits-on-pr-merge.yaml"))
    raw = tuple(
        replace(t, block=replace(t.block, condition_id=None)) if t.block else t
        for t in fixture.snapshot.tasks
    )
    with serve() as gh:
        gh.pull(REPO, 123)
        gh.pull(REPO, 124)
        clock = Clock(fixture.snapshot.now)
        connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=clock), [REPO])
        out = ConditionSync(connector, load_block_conditions(PATTERNS, [REPO]))(
            replace(fixture.snapshot, tasks=raw, conditions=())
        )
    assert [t.block for t in out.tasks] == [t.block for t in fixture.snapshot.tasks]
    expected = {c.id: (c.kind, c.task_id, c.registered_at) for c in fixture.snapshot.conditions}
    assert {c.id: (c.kind, c.task_id, c.registered_at) for c in out.conditions} == expected


# --- ready tasks (dispatch gate) ----------------------------------------------------------

READY_PATTERNS = [
    *PATTERNS,
    {
        "pattern": r"after (?P<pr>#\d+) is green",
        "kind": "ci_green",
        "repo": REPO,
        "applies_to": ["ready_task"],
    },
    {
        "pattern": r"after (?P<pr>#\d+) lands",
        "kind": "pr_merged",
        "repo": REPO,
        "applies_to": ["block", "ready_task"],
    },
]


def ready(task_id: str, title: str, since: datetime = NOW - timedelta(hours=1)) -> Task:
    return Task(task_id, title, Status.READY, since, role="developer")


def ready_sync(gh: FakeGitHub, rec: Recorder | None = None) -> ConditionSync:
    connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=Clock(NOW)), [REPO])
    return ConditionSync(connector, load_block_conditions(READY_PATTERNS, [REPO]), rec)


def test_applies_to_validation() -> None:
    patterns = load_block_conditions(READY_PATTERNS, [REPO])
    assert [sorted(p.applies_to) for p in patterns] == [
        ["block"],
        ["block"],
        ["ready_task"],
        ["block", "ready_task"],
    ]
    for bad in ([], ["task"], "ready_task", [1]):
        with pytest.raises(ValueError, match="applies_to"):
            load_block_conditions(
                [{"pattern": "(?P<pr>.+)", "kind": "pr_merged", "applies_to": bad}], [REPO]
            )


def test_ready_task_title_gets_a_condition_linked_by_task_id() -> None:
    rec = Recorder()
    tasks = (
        ready("T-1", "Redo the migration after #12 is green"),
        ready("T-2", "Cleanup after #13 lands"),
        ready("T-3", "Waiting for CI/merge of #14"),  # a block-only pattern: not read here
        replace(ready("T-4", "after #15 is green"), status=Status.IN_PROGRESS),  # not ready
    )
    with serve() as gh:
        gh.pull(REPO, 12, sha=SHA)
        gh.ci(REPO, SHA, statuses=[{"state": "failure", "updated_at": "2026-03-02T11:00:00Z"}])
        gh.pull(REPO, 13)
        out = ready_sync(gh, rec)(Snapshot(now=NOW, tasks=tasks))
        assert sorted(gh.paths())[0].startswith("/repos/example/repo/")
    assert out.tasks == tasks  # nothing linked through a block
    by_id = {c.id: c for c in out.conditions}
    assert set(by_id) == {
        "ready:T-1:ci_green:example/repo#12",
        "ready:T-2:pr_merged:example/repo#13",
    }
    red = by_id["ready:T-1:ci_green:example/repo#12"]
    assert red.kind is ConditionKind.CI_STATUS and red.task_id == "T-1"
    assert red.satisfied is False and red.registered_at == NOW - timedelta(hours=1)
    assert red.params["source"] == "task_text" and red.params["pr"] == 12
    assert rec.blocks == {}  # ready-task matches are not block outcomes


def test_ready_condition_already_registered_is_not_duplicated() -> None:
    task = ready("T-1", "Cleanup after #13 lands")
    existing = Condition(
        "ready:T-1:pr_merged:example/repo#13",
        ConditionKind.PR_MERGED,
        "T-1",
        {"repo": REPO, "pr": 13},
    )
    with serve() as gh:
        gh.pull(REPO, 13)
        out = ready_sync(gh)(Snapshot(now=NOW, tasks=(task,), conditions=(existing,)))
    assert [c.id for c in out.conditions] == [existing.id]


def test_dispatch_gate_park_then_wake_follows_the_prs_current_head() -> None:
    """GitHub step -> dispatch gate -> wait -> adapter registers -> fixing push -> rule 2."""
    old, new = "1" * 40, "2" * 40
    clock = Clock(NOW)
    task = ready("T-1", "Redo the migration after #12 is green")
    with serve() as gh:
        connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=clock), [REPO])
        step = ConditionSync(connector, load_block_conditions(READY_PATTERNS, [REPO]))
        gh.pull(REPO, 12, sha=old)
        gh.ci(REPO, old, statuses=[{"state": "failure", "updated_at": "2026-03-02T11:50:00Z"}])
        red = step(Snapshot(now=NOW, tasks=(task,)))
        [wait] = DispatchGate().evaluate(red)
        assert wait.kind is ActionKind.WAIT
        [registered] = wait_conditions(wait)
        assert "sha" not in registered.params  # follows the head, not the red commit
        parked_at = NOW + timedelta(minutes=1)
        registered = replace(registered, registered_at=parked_at)
        waiting = replace(task, status=Status.WAITING, status_since=parked_at)
        # The fix is pushed and goes green.
        gh.pull(REPO, 12, sha=new)
        gh.ci(REPO, new, statuses=[{"state": "success", "updated_at": "2026-03-02T12:05:00Z"}])
        clock.advance(minutes=10)
        green = step(Snapshot(now=clock.now, tasks=(waiting,), conditions=(registered,)))
    [checked] = green.conditions
    assert checked.satisfied is True and checked.params["sha"] == new
    [wake] = WaitCondition().evaluate(green)
    assert wake.kind is ActionKind.WAKE and wake.task_id == "T-1"
    assert DispatchGate().evaluate(green) == []


def test_ready_task_title_names_several_prerequisites() -> None:
    task = ready("T-1", "Wire the orders API after #21 lands, and after #22 lands (#21 lands)")
    with serve() as gh:
        gh.pull(REPO, 21)
        gh.pull(REPO, 22)
        out = ready_sync(gh)(Snapshot(now=NOW, tasks=(task,)))
    assert [c.id for c in out.conditions] == [
        "ready:T-1:pr_merged:example/repo#21",
        "ready:T-1:pr_merged:example/repo#22",
    ]


def test_ready_task_refs_are_capped() -> None:
    title = " ".join(f"after #{n} lands" for n in range(1, MAX_READY_REFS + 3))
    task = ready("T-1", title)
    with serve() as gh:
        for n in range(1, MAX_READY_REFS + 3):
            gh.pull(REPO, n)
        out = ready_sync(gh)(Snapshot(now=NOW, tasks=(task,)))
    assert len(out.conditions) == MAX_READY_REFS


def test_block_reason_still_takes_only_the_first_pr() -> None:
    task = blocked("T-1", "waiting for CI/merge of #31 waiting for CI/merge of #32", NOW)
    with serve() as gh:
        gh.pull(REPO, 31)
        gh.pull(REPO, 32)
        out = sync(gh)(Snapshot(now=NOW, tasks=(task,)))
    (c,) = out.conditions
    assert c.params["pr"] == 31


def test_unresolvable_refs_do_not_use_up_the_cap() -> None:
    names = " ".join(f"after other/repo#{n} lands" for n in range(1, MAX_READY_REFS + 3))
    task = ready("T-1", f"{names} and after #21 lands")
    with serve() as gh:
        gh.pull(REPO, 21)
        out = ready_sync(gh)(Snapshot(now=NOW, tasks=(task,)))
    assert [c.id for c in out.conditions] == ["ready:T-1:pr_merged:example/repo#21"]


def test_title_refs_and_already_known_ones() -> None:
    task = ready("T-1", "Wire it after #41 lands, also after #42 lands")
    known = Condition(
        "ready:T-1:pr_merged:example/repo#41",
        ConditionKind.PR_MERGED,
        "T-1",
        {"repo": REPO, "pr": 41},
    )
    with serve() as gh:
        gh.pull(REPO, 41)
        gh.pull(REPO, 42)
        out = ready_sync(gh)(Snapshot(now=NOW, tasks=(task,), conditions=(known,)))
    assert [c.id for c in out.conditions] == [
        "ready:T-1:pr_merged:example/repo#41",
        "ready:T-1:pr_merged:example/repo#42",
    ]


def test_a_description_never_links_a_pr() -> None:
    """Incident 17: on a live farm 8 of 10 PRs matched from mentions in descriptions and
    comments were not the task's."""
    task = replace(ready("T-1", "Wire the orders API"), body="Context: after #21 lands")
    with serve() as gh:
        gh.pull(REPO, 21)
        out = ready_sync(gh)(Snapshot(now=NOW, tasks=(task,)))
    assert out.conditions == ()


def test_text_beyond_the_limit_is_not_read() -> None:
    task = ready("T-1", "x" * MAX_TASK_TEXT + " after #51 lands")
    with serve() as gh:
        gh.pull(REPO, 51)
        out = ready_sync(gh)(Snapshot(now=NOW, tasks=(task,)))
    assert out.conditions == ()
