"""A connector step that fails carries its last checked conditions forward (#242).

The conditions keep their original ``checked_at``, so a single failed tick changes nothing
the rules see, and a step that keeps failing lets them go stale on their own: past
``max_condition_age`` no rule acts on them, exactly as for conditions never checked.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import load_rules, load_settings, merged_snapshot
from kelpie.connectors.github.blocks import ConditionSync, load_block_conditions
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.connectors.human_tasks import HumanTaskConnector
from kelpie.core.actions import Action, ActionKind
from kelpie.core.conditions import Contribution, carry_forward, contribution
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Owner,
    OwnerKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.core.rule import Rule
from kelpie.metrics import Metrics
from kelpie.rules.stale_blocks import StaleBlocks
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

T0 = datetime(2026, 10, 19, 10, tzinfo=UTC)
M = timedelta(minutes=1)
HUMAN = ConditionKind.HUMAN_TASK
REPO = "example/repo"


@dataclass
class Board:
    """An adapter with a fixed board."""

    board: Snapshot
    name: str = "board"

    def snapshot(self, now: datetime) -> Snapshot:
        return Snapshot(
            now=now,
            tasks=self.board.tasks,
            owners=self.board.owners,
            conditions=self.board.conditions,
        )


@dataclass
class Flaky:
    """A connector step that raises while ``failing``."""

    step: Callable[[Snapshot], Snapshot]
    failing: bool = False

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        if self.failing:
            raise RuntimeError("connector unreachable")
        return self.step(snapshot)


class Ticks:
    """``merged_snapshot`` on a driven clock, with one connector step."""

    def __init__(self, board: Snapshot, name: str, step: Callable[[Snapshot], Snapshot]):
        self.now = T0
        self.metrics = Metrics()
        self.step = Flaky(step)
        self.source = merged_snapshot(
            [Board(board)],  # type: ignore[list-item]
            self.metrics,
            lambda: self.now,
            enrich=[("task_status", lambda s: s), (name, self.step)],
        )

    def at(self, now: datetime, *, failing: bool) -> Snapshot:
        self.now, self.step.failing = now, failing
        return self.source()


def run(rules: Sequence[Rule], snapshot: Snapshot) -> list[Action]:
    return [a for r in rules for a in r.evaluate(snapshot)]


def summary(actions: Sequence[Action]) -> list[tuple[str, str, str | None, tuple[str, ...]]]:
    """What the actions do, without the texts (they name the time)."""
    return sorted(
        (a.rule, a.kind.value, a.task_id, tuple(sorted(map(str, a.params.get("waiting", ())))))
        for a in actions
    )


# --- human_tasks: three digest waits ----------------------------------------------------

HUMAN_CONFIG = """
[storage]
path = ":memory:"

[traces]
enabled = false

[connectors.human_tasks]

[engine.rules.needs_you]
human_wait_alert_after = "1h"
default_approver = "ceo"
"""


def digest_board() -> Snapshot:
    """W-1..W-3 wait on H-1, the CEO's task, by human_task conditions (as the adapter
    gives them: registered, never checked)."""
    since = T0 - timedelta(hours=30)
    tasks = [
        Task("H-1", "Sign the contract", Status.READY, since, role="owner", owner_id="ceo"),
    ]
    conditions = []
    for n in (1, 2, 3):
        tasks.append(Task(f"W-{n}", "Integrate", Status.WAITING, since, owner_id="agent-dev-1"))
        conditions.append(
            Condition(f"C-{n}", HUMAN, f"W-{n}", {"task": "H-1"}, registered_at=since)
        )
    return Snapshot(
        now=T0,
        tasks=tuple(tasks),
        owners=(
            Owner("ceo", "owner", OwnerKind.HUMAN),
            Owner("agent-dev-1", "developer"),
        ),
        conditions=tuple(conditions),
    )


@pytest.fixture
def human_rules(tmp_path: Path) -> list[Rule]:
    path = tmp_path / "kelpie.toml"
    path.write_text(HUMAN_CONFIG)
    return load_rules(load_settings(path).engine).rules


def test_human_tasks_failing_one_tick_changes_nothing(human_rules: list[Rule]) -> None:
    ticks = Ticks(digest_board(), "human_tasks", HumanTaskConnector())
    good = ticks.at(T0, failing=False)
    assert all(c.checked_at == T0 and c.satisfied is False for c in good.conditions)
    expected = summary(run(human_rules, good))
    # The three waits are in the CEO's digest, and nothing alerts about them.
    assert ("needs_you", "escalate", None, ("W-1", "W-2", "W-3")) in expected
    assert not [s for s in expected if s[0] == "wait_condition"]

    failed = ticks.at(T0 + M, failing=True)
    assert failed.conditions == good.conditions  # carried, with their original checked_at
    assert summary(run(human_rules, failed)) == expected  # no alert, nothing resolved
    assert ticks.metrics.connector_errors["human_tasks"] == 1
    assert ticks.metrics.connector_failing_for("human_tasks") == timedelta(0)


def test_human_tasks_failing_past_max_condition_age_goes_stale(
    human_rules: list[Rule],
) -> None:
    ticks = Ticks(digest_board(), "human_tasks", HumanTaskConnector())
    ticks.at(T0, failing=False)
    ticks.at(T0 + M, failing=True)
    late = ticks.at(T0 + 16 * M, failing=True)
    assert all(c.checked_at == T0 for c in late.conditions)
    # As if never checked: the digest cannot vouch for the waits, rule 2 says the connector
    # is not checking them.
    unchecked = Snapshot(
        now=late.now, tasks=late.tasks, owners=late.owners, conditions=digest_board().conditions
    )
    assert summary(run(human_rules, late)) == summary(run(human_rules, unchecked))
    assert {a.task_id for a in run(human_rules, late) if a.rule == "wait_condition"} == {
        "W-1",
        "W-2",
        "W-3",
    }
    assert ticks.metrics.connector_failing_for("human_tasks") == 15 * M
    assert 'kelpie_connector_failing_seconds{connector="human_tasks"} 900' in (
        ticks.metrics.render()
    )
    ticks.at(T0 + 17 * M, failing=False)
    assert ticks.metrics.connector_failing_for("human_tasks") is None
    assert 'kelpie_connector_failing_seconds{connector="human_tasks"} 0' in (ticks.metrics.render())


def test_a_carried_human_task_done_wakes_only_while_fresh(human_rules: list[Rule]) -> None:
    board = digest_board()
    done = tuple(
        t if t.id != "H-1" else Task(t.id, t.title, Status.DONE, T0 - M, owner_id="ceo")
        for t in board.tasks
    )
    ticks = Ticks(
        Snapshot(now=T0, tasks=done, owners=board.owners, conditions=board.conditions),
        "human_tasks",
        HumanTaskConnector(),
    )

    def wakes(snapshot: Snapshot) -> set[str | None]:
        return {a.task_id for a in run(human_rules, snapshot) if a.kind is ActionKind.WAKE}

    assert wakes(ticks.at(T0, failing=False)) == {"W-1", "W-2", "W-3"}
    assert wakes(ticks.at(T0 + 5 * M, failing=True)) == {"W-1", "W-2", "W-3"}
    assert wakes(ticks.at(T0 + 16 * M, failing=True)) == set()


# --- GitHub: a block waiting for a PR ----------------------------------------------------


def github_board() -> Snapshot:
    since = T0 - timedelta(hours=3)
    task = Task(
        "T-1",
        "Review change",
        Status.BLOCKED,
        since,
        role="reviewer",
        block=Block("waiting for ci/merge of #123", since),
    )
    return Snapshot(now=T0, tasks=(task,))


def github_step(gh: FakeGitHub, clock: Clock) -> ConditionSync:
    connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=clock), [REPO])
    patterns = [
        {"pattern": r"waiting for ci/merge of (?P<pr>\S+)", "kind": "pr_merged", "repo": REPO}
    ]
    return ConditionSync(connector, load_block_conditions(patterns, [REPO]))


@pytest.mark.parametrize("merged", [False, True])
def test_github_failing_one_tick_changes_nothing(merged: bool) -> None:
    rule = StaleBlocks()
    with serve() as gh:
        gh.pull(REPO, 123, merged=merged, state="closed" if merged else "open")
        ticks = Ticks(github_board(), "github", github_step(gh, Clock(T0)))
        good = ticks.at(T0, failing=False)
    (condition,) = good.conditions
    assert condition.checked_at == T0 and condition.satisfied is merged
    expected = summary(rule.evaluate(good))
    assert [s[1] for s in expected] == (["unblock"] if merged else [])

    failed = ticks.at(T0 + M, failing=True)
    assert failed.conditions == good.conditions and failed.tasks == good.tasks
    assert summary(rule.evaluate(failed)) == expected
    assert ticks.metrics.connector_errors["github"] == 1


def test_github_failing_past_max_condition_age_goes_stale() -> None:
    rule = StaleBlocks()
    with serve() as gh:
        gh.pull(REPO, 123, merged=True, state="closed")
        ticks = Ticks(github_board(), "github", github_step(gh, Clock(T0)))
        ticks.at(T0, failing=False)
    assert [a.kind for a in rule.evaluate(ticks.at(T0 + 15 * M, failing=True))] == [
        ActionKind.UNBLOCK
    ]
    late = ticks.at(T0 + 16 * M, failing=True)
    (condition,) = late.conditions
    assert condition.checked_at == T0 and late.tasks[0].block is not None
    assert late.tasks[0].block.condition_id == condition.id
    assert ActionKind.UNBLOCK not in {a.kind for a in rule.evaluate(late)}


# --- the mechanism ------------------------------------------------------------------------


def test_a_failing_step_with_no_good_tick_carries_nothing() -> None:
    ticks = Ticks(digest_board(), "human_tasks", HumanTaskConnector())
    first = ticks.at(T0, failing=True)
    assert first.conditions == digest_board().conditions


def test_nothing_is_carried_onto_a_condition_that_changed() -> None:
    since = T0 - timedelta(hours=1)
    given = Condition("C-1", HUMAN, "W-1", {"task": "H-1"}, registered_at=since)
    checked = Condition(
        "C-1", HUMAN, "W-1", {"task": "H-1"}, False, checked_at=T0, registered_at=since
    )
    last = contribution(
        Snapshot(now=T0, conditions=(given,)), Snapshot(now=T0, conditions=(checked,))
    )
    assert last == Contribution((checked,), (given,), ())
    later = T0 + M
    assert carry_forward(Snapshot(now=later, conditions=(given,)), last).conditions == (checked,)
    # Registered again (a new wait): the old check is not its evidence.
    again = Condition("C-1", HUMAN, "W-1", {"task": "H-1"}, registered_at=later)
    assert carry_forward(Snapshot(now=later, conditions=(again,)), last).conditions == (again,)
    # Checked by someone else meanwhile (an adapter that reports results): theirs stands.
    theirs = Condition(
        "C-1", HUMAN, "W-1", {"task": "H-1"}, True, later, later, registered_at=since
    )
    assert carry_forward(Snapshot(now=later, conditions=(theirs,)), last).conditions == (theirs,)
    # Gone from the board: not brought back.
    assert carry_forward(Snapshot(now=later), last).conditions == ()


def test_an_attached_condition_comes_back_only_onto_the_same_block() -> None:
    since = T0 - timedelta(hours=1)
    task = Task("T-1", "t", Status.BLOCKED, since, block=Block("waiting for #1", since))
    condition = Condition("block:T-1", ConditionKind.PR_MERGED, "T-1", checked_at=T0)
    linked = Task(
        "T-1", "t", Status.BLOCKED, since, block=Block("waiting for #1", since, "block:T-1")
    )
    last = contribution(
        Snapshot(now=T0, tasks=(task,)),
        Snapshot(now=T0, tasks=(linked,), conditions=(condition,)),
    )
    later = T0 + M
    out = carry_forward(Snapshot(now=later, tasks=(task,)), last)
    assert out.tasks == (linked,) and out.conditions == (condition,)
    # The block changed (another reason, or blocked again): the old condition is not its.
    for block in (Block("waiting for #2", since), Block("waiting for #1", later)):
        moved = Task("T-1", "t", Status.BLOCKED, since, block=block)
        out = carry_forward(Snapshot(now=later, tasks=(moved,)), last)
        assert out.tasks == (moved,) and out.conditions == ()
    # The task left the board.
    assert carry_forward(Snapshot(now=later), last) == Snapshot(now=later)
    # An unlinked attached condition comes back while its task is on the board.
    bare = Condition("pre:T-1", ConditionKind.PR_MERGED, "T-1", checked_at=T0)
    last = Contribution((bare,))
    assert carry_forward(Snapshot(now=later, tasks=(task,)), last).conditions == (bare,)
    assert carry_forward(Snapshot(now=later), last).conditions == ()


def test_metrics_render_the_failure_streak() -> None:
    metrics = Metrics()
    assert "kelpie_connector" not in metrics.render()
    metrics.connector_failed("github", T0)
    metrics.connector_failed("github", T0 + 2 * M)
    rendered = metrics.render()
    assert 'kelpie_connector_errors_total{connector="github"} 2' in rendered
    assert 'kelpie_connector_failing_seconds{connector="github"} 120' in rendered
    metrics.connector_ok("github", T0 + 3 * M)
    assert 'kelpie_connector_failing_seconds{connector="github"} 0' in metrics.render()
    assert metrics.connector_failing_for("github") is None
