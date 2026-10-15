"""Block reasons turned into conditions during snapshot assembly, and rule 4 on the result."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.connectors.github.blocks import ConditionSync, load_block_conditions
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.core.actions import ActionKind
from kelpie.core.model import Block, Condition, ConditionKind, Snapshot, Status, Task
from kelpie.rules.stale_blocks import StaleBlocks
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
