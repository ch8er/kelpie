"""A connector step that fails carries its last checked conditions forward (#242).

The conditions keep their original ``checked_at``, so a single failed tick changes nothing
the rules see, and a step that keeps failing lets them go stale on their own: past
``max_condition_age`` no rule acts on them, exactly as for conditions never checked.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import load_rules, load_settings, merged_snapshot
from kelpie.connectors.github.blocks import ConditionSync, load_block_conditions
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.connectors.human_tasks import HumanTaskConnector
from kelpie.core.actions import Action, ActionKind
from kelpie.core.conditions import CARRY_FOR, Contribution, carry_forward, contribution, widen
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
from kelpie.rules.dispatch_gate import DispatchGate
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
    failing: bool = False

    def snapshot(self, now: datetime) -> Snapshot:
        if self.failing:
            raise RuntimeError("adapter unreachable")
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

    def __init__(
        self,
        board: Snapshot,
        name: str,
        step: Callable[[Snapshot], Snapshot],
        other: Snapshot | None = None,
    ):
        self.now = T0
        self.metrics = Metrics()
        self.step = Flaky(step)
        self.board = Board(board)
        self.other = Board(other or Snapshot(now=T0), name="other")
        self.source = merged_snapshot(
            [self.board, self.other],  # type: ignore[list-item]
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


def failing_seconds(metrics: Metrics, name: str) -> str:
    """The gauge's value for ``name``, or "" when it has none."""
    prefix = f'kelpie_connector_failing_seconds{{connector="{name}"}} '
    lines = [x for x in metrics.render().splitlines() if x.startswith(prefix)]
    return lines[0].removeprefix(prefix) if lines else ""


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
    assert failing_seconds(ticks.metrics, "human_tasks") == "60"  # since the last good tick


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
    assert failing_seconds(ticks.metrics, "human_tasks") == "960"
    ticks.at(T0 + 17 * M, failing=False)
    assert failing_seconds(ticks.metrics, "human_tasks") == "0"


def test_a_met_check_is_never_carried(human_rules: list[Rule]) -> None:
    """A failed step wakes nothing: what it last found met may no longer hold (the person
    reopened their task), and the board it could not read would say so."""
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
    ticks.board.board = board  # H-1 reopened
    failed = ticks.at(T0 + 2 * M, failing=True)
    assert failed.conditions == board.conditions  # as the adapter gives them: unchecked
    assert wakes(failed) == set()


def test_a_met_block_condition_is_not_carried() -> None:
    since = T0 - timedelta(hours=1)
    task = Task("T-1", "t", Status.BLOCKED, since, block=Block("waiting_human: H-1", since))
    human = Task("H-1", "Approve", Status.DONE, T0 - M, owner_id="ceo")
    owners = (Owner("ceo", "owner", OwnerKind.HUMAN),)
    pattern = re.compile(r"waiting_human: (?P<task>H-\d+)")
    ticks = Ticks(
        Snapshot(now=T0, tasks=(task, human), owners=owners),
        "human_tasks",
        HumanTaskConnector([pattern]),
    )
    good = ticks.at(T0, failing=False)
    assert [c.satisfied for c in good.conditions] == [True]
    ticks.board.board = Snapshot(
        now=T0, tasks=(task, replace(human, status=Status.READY)), owners=owners
    )
    failed = ticks.at(T0 + 5 * M, failing=True)  # past stale_blocks' unblock grace
    assert failed.conditions == () and failed.tasks[0].block == task.block
    assert ActionKind.UNBLOCK not in {a.kind for a in StaleBlocks().evaluate(failed)}


# --- GitHub: a block waiting for a PR ----------------------------------------------------


def github_board(age: timedelta = timedelta(hours=5)) -> Snapshot:
    """T-1 blocked on PR #123 for longer than rule 4's ``unlinked_alert_after`` (4h): a
    bare block alerts at once."""
    since = T0 - age
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


def keys(actions: Sequence[Action]) -> list[str]:
    return sorted(a.key.split(":", 1)[0] for a in actions)


def test_github_failing_one_tick_changes_nothing() -> None:
    rule = StaleBlocks()
    with serve() as gh:
        gh.pull(REPO, 123)
        ticks = Ticks(github_board(), "github", github_step(gh, Clock(T0)))
        good = ticks.at(T0, failing=False)
    (condition,) = good.conditions
    assert condition.checked_at == T0 and condition.satisfied is False
    assert rule.evaluate(good) == []
    # Without carrying, the block is bare for that tick: rule 4's unlinked alert.
    assert keys(rule.evaluate(Snapshot(now=T0 + M, tasks=github_board().tasks))) == ["unlinked"]

    failed = ticks.at(T0 + M, failing=True)
    assert failed.conditions == good.conditions and failed.tasks == good.tasks
    assert rule.evaluate(failed) == []  # no alert, so nothing to resolve on the next tick
    assert ticks.metrics.connector_errors["github"] == 1


def test_github_failing_past_max_condition_age_goes_stale() -> None:
    """The block stays linked to its stale condition: no rule acts on it, and after
    ``stale_evidence_after`` (1h) rule 4 says the evidence is stale (not that the block
    names no condition, which it does)."""
    rule = StaleBlocks()
    with serve() as gh:
        gh.pull(REPO, 123)
        ticks = Ticks(github_board(), "github", github_step(gh, Clock(T0)))
        ticks.at(T0, failing=False)
    late = ticks.at(T0 + 16 * M, failing=True)
    (condition,) = late.conditions
    assert condition.checked_at == T0 and late.tasks[0].block is not None
    assert late.tasks[0].block.condition_id == condition.id
    assert rule.evaluate(late) == []
    assert keys(rule.evaluate(ticks.at(T0 + 61 * M, failing=True))) == ["stale-evidence"]


def test_a_merged_pr_is_not_carried() -> None:
    rule = StaleBlocks()
    with serve() as gh:
        gh.pull(REPO, 123, merged=True, state="closed")
        ticks = Ticks(github_board(timedelta(hours=1)), "github", github_step(gh, Clock(T0)))
        assert [a.kind for a in rule.evaluate(ticks.at(T0, failing=False))] == [ActionKind.UNBLOCK]
    failed = ticks.at(T0 + M, failing=True)
    assert failed.conditions == () and rule.evaluate(failed) == []


# --- the mechanism ------------------------------------------------------------------------


def test_a_failing_step_with_no_good_tick_carries_nothing() -> None:
    ticks = Ticks(digest_board(), "human_tasks", HumanTaskConnector())
    first = ticks.at(T0, failing=True)
    assert first.conditions == digest_board().conditions
    assert failing_seconds(ticks.metrics, "human_tasks") == "0"
    assert failing_seconds(Ticks(digest_board(), "x", HumanTaskConnector()).metrics, "x") == ""


def test_nothing_is_carried_onto_a_condition_that_changed() -> None:
    since = T0 - timedelta(hours=1)
    w1 = (Task("W-1", "t", Status.WAITING, since),)
    given = Condition("C-1", HUMAN, "W-1", {"task": "H-1"}, registered_at=since)
    checked = Condition(
        "C-1", HUMAN, "W-1", {"task": "H-1"}, False, checked_at=T0, registered_at=since
    )
    last = contribution(
        Snapshot(now=T0, tasks=w1, conditions=(given,)),
        Snapshot(now=T0, tasks=w1, conditions=(checked,)),
    )
    assert last == Contribution((checked,), (given,), ())
    later = T0 + M

    def carried(*conditions: Condition, tasks: tuple[Task, ...] = w1) -> tuple[Condition, ...]:
        return carry_forward(
            Snapshot(now=later, tasks=tasks, conditions=conditions), last
        ).conditions

    assert carried(given) == (checked,)
    # Registered again (a new wait): the old check is not its evidence.
    again = Condition("C-1", HUMAN, "W-1", {"task": "H-1"}, registered_at=later)
    assert carried(again) == (again,)
    # Checked by someone else meanwhile (an adapter that reports results): theirs stands.
    theirs = Condition(
        "C-1", HUMAN, "W-1", {"task": "H-1"}, True, later, later, registered_at=since
    )
    assert carried(theirs) == (theirs,)
    # Gone from the board: not brought back.
    assert carried() == ()
    # Its task no longer waits (a ready task's condition gates it: never carried).
    assert carried(given, tasks=(replace(w1[0], status=Status.READY),)) == (given,)
    # Two conditions by that id (two adapters): which one the check was is unknown.
    other = replace(given, task_id="W-2")
    assert carried(given, other) == (given, other)
    # Checked longer ago than CARRY_FOR.
    late = carry_forward(Snapshot(now=T0 + CARRY_FOR + M, tasks=w1, conditions=(given,)), last)
    assert late.conditions == (given,)


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


def test_a_ready_tasks_gate_is_never_carried() -> None:
    """Carrying would let ``dispatch_gate`` park a task on what a failed step last saw."""
    ready = Task("T-1", "Ship after #123", Status.READY, T0 - M)
    gate = Condition("ready:T-1", ConditionKind.PR_MERGED, "T-1", satisfied=False, checked_at=T0)

    def gating(snapshot: Snapshot) -> Snapshot:
        return replace(snapshot, conditions=(*snapshot.conditions, gate))

    ticks = Ticks(Snapshot(now=T0, tasks=(ready,)), "github", gating)
    rule = DispatchGate()
    assert [a.kind for a in rule.evaluate(ticks.at(T0, failing=False))] == [ActionKind.WAIT]
    failed = ticks.at(T0 + M, failing=True)
    assert failed.conditions == () and rule.evaluate(failed) == []


def test_widen_is_bounded_by_carry_for() -> None:
    since = T0 - timedelta(hours=1)
    gone = Condition("C-9", HUMAN, "W-9", {"task": "H-1"}, False, checked_at=T0)
    last = Contribution((gone,), (replace(gone, satisfied=None, checked_at=None),))
    board = Snapshot(now=T0 + M, tasks=(Task("W-1", "t", Status.WAITING, since),))
    assert widen(Contribution(), last, board).conditions == (gone,)
    later = replace(board, now=T0 + CARRY_FOR + M)
    assert widen(Contribution(), last, later) == Contribution()


def test_a_partial_tick_keeps_what_it_did_not_see() -> None:
    """A tick that missed part of the farm (an adapter failed) keeps the last good tick's
    conditions for the tasks it did not see, so a failure on the next tick finds them."""
    board = digest_board()
    here = Snapshot(
        now=T0,
        tasks=tuple(t for t in board.tasks if t.id != "W-1"),
        owners=board.owners,
        conditions=tuple(c for c in board.conditions if c.task_id != "W-1"),
    )
    there = Snapshot(
        now=T0,
        tasks=tuple(t for t in board.tasks if t.id == "W-1"),
        conditions=tuple(c for c in board.conditions if c.task_id == "W-1"),
    )
    ticks = Ticks(here, "human_tasks", HumanTaskConnector(), other=there)
    good = {c.id: c for c in ticks.at(T0, failing=False).conditions}
    ticks.other.failing = True
    ticks.at(T0 + M, failing=False)  # W-1's adapter missing: C-1 not checked
    ticks.other.failing = False
    back = {c.id: c for c in ticks.at(T0 + 2 * M, failing=True).conditions}
    assert back["C-1"] == good["C-1"]  # from tick T0
    assert back["C-2"].checked_at == T0 + M
    # A complete tick replaces it all: W-1 gone for good, C-1 is not kept.
    ticks.other.board = Snapshot(now=T0)
    ticks.at(T0 + 3 * M, failing=False)
    ticks.other.board = there
    assert ticks.at(T0 + 4 * M, failing=True).conditions[-1] == there.conditions[0]


def test_metrics_render_the_failure_streak() -> None:
    metrics = Metrics()
    assert "kelpie_connector" not in metrics.render()
    metrics.connector_ok("github", T0)
    metrics.connector_failed("github", T0 + M)
    metrics.connector_failed("github", T0 + 3 * M)
    rendered = metrics.render()
    assert 'kelpie_connector_errors_total{connector="github"} 2' in rendered
    assert failing_seconds(metrics, "github") == "180"  # since the last good tick
    metrics.connector_ok("github", T0 + 4 * M)
    assert failing_seconds(metrics, "github") == "0"
    metrics.connector_failed("github", T0)  # the clock stepped back
    assert failing_seconds(metrics, "github") == "0"
