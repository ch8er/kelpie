"""Snoozes in the rules (#176): which alerts a snooze holds, and when they come back."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.alerts import SnoozeState, Transition
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Snooze,
    Status,
    Task,
)
from kelpie.rules._snoozes import behind_count, check, describe
from kelpie.rules.needs_you import NeedsYou
from kelpie.rules.needs_you import build as build_needs_you
from kelpie.rules.stale_blocks import StaleBlocks
from kelpie.rules.wait_condition import WaitCondition
from tests.fakes import FakeAdapter, FakeAlerter
from tests.incidents import load

NOW = datetime(2026, 10, 19, 10, tzinfo=UTC)
H = timedelta(hours=1)
OWNERS = (
    Owner("ceo", "owner", OwnerKind.HUMAN),
    Owner("agent-ops-1", "ops"),
)
NY = NeedsYou(default_approver="ceo")


def question(tid: str, since: datetime, reason: str = "waiting_human: needs_input: ok?") -> Task:
    return Task(
        tid, f"agent {tid}", Status.BLOCKED, since, role="ops", owner_id="agent-ops-1",
        block=Block(reason, since),
    )  # fmt: skip


def snooze(
    tid: str,
    until: datetime = NOW + 7 * 24 * H,
    behind: int | None = 0,
    source: str = "cli",
    reason: str | None = "deferred to the release",
    by: str | None = "ceo",
) -> Snooze:
    return Snooze(tid, until, reason, by, NOW - 24 * H, source, behind)  # type: ignore[arg-type]


def snap(
    tasks: list[Task],
    snoozes: list[Snooze] = (),  # type: ignore[assignment]
    deps: list[Dependency] = (),  # type: ignore[assignment]
    conditions: list[Condition] = (),  # type: ignore[assignment]
    now: datetime = NOW,
) -> Snapshot:
    return Snapshot(
        now,
        tuple(tasks),
        OWNERS,
        tuple(deps),
        tuple(conditions),
        snoozes=MappingProxyType({s.task_id: s for s in snoozes}),
    )


# --- the check -----------------------------------------------------------------------------


def never() -> int:
    raise AssertionError("the count is not needed")


def test_check_tells_held_expired_and_grown_apart() -> None:
    board = snap([], [snooze("a"), snooze("b", until=NOW - H), snooze("c", behind=1)])
    assert check(board, "none", never) is None
    held = check(board, "a", lambda: 0)
    assert held is not None and held.holds and held.state is SnoozeState.HELD
    expired = check(board, "b", never)  # an expired snooze never counts the work behind it
    assert expired is not None and expired.state is SnoozeState.EXPIRED and not expired.holds
    assert check(board, "c", lambda: 1).holds  # as much as when it was snoozed: held
    grown = check(board, "c", lambda: 4)
    assert grown is not None and grown.state is SnoozeState.GROWN and grown.grown == 3


def test_a_snooze_expired_more_than_a_week_ago_is_no_snooze() -> None:
    board = snap([], [snooze("a", until=NOW - 7 * 24 * H - timedelta(seconds=1))])
    assert check(board, "a", never) is None
    edge = snap([], [snooze("a", until=NOW - 7 * 24 * H)])
    assert check(edge, "a", never).state is SnoozeState.EXPIRED  # type: ignore[union-attr]


def test_latched_growth_stays_until_the_snooze_is_replaced_or_expires() -> None:
    latched = replace(snooze("a", behind=3), grown_at=NOW - H, grown_by=2)
    found = check(snap([], [latched]), "a", never)  # the count is not read again
    assert found is not None and found.state is SnoozeState.GROWN and found.grown == 2
    later = replace(latched, grown_at=NOW + H)  # latched after now (a clock skew): not yet
    assert check(snap([], [later]), "a", lambda: 3).holds  # type: ignore[union-attr]
    gone = replace(latched, until=NOW - H)
    assert check(snap([], [gone]), "a", never).state is SnoozeState.EXPIRED  # type: ignore[union-attr]
    assert not latched.holds_at(NOW) and latched.holds_at(NOW - 2 * H)
    assert not latched.holds_at(NOW - 25 * H)  # before it was set


def test_a_snooze_without_a_count_never_comes_back_on_growth() -> None:
    board = snap([], [snooze("m", behind=None, source="marker"), snooze("u", behind=None)])
    assert check(board, "m", never).holds  # type: ignore[union-attr]
    assert check(board, "u", never).holds  # type: ignore[union-attr]


def test_the_texts_name_the_marker_and_the_growth() -> None:
    board = snap(
        [],
        [snooze("m", NOW - H, None, "marker"), snooze("c", behind=1), snooze("a", reason=None)],
    )
    expired = check(board, "m", never)
    assert expired is not None
    assert expired.text() == f"its snooze (from marker) expired at {(NOW - H).isoformat()}"
    assert expired.params()["source"] == "marker" and "grown" not in expired.params()
    grown = check(board, "c", lambda: 3)
    assert grown is not None and grown.text().startswith("2 more task(s) blocked since its snooze")
    assert grown.params()["grown"] == 2 and grown.params()["behind_at_snooze"] == 1
    held = check(board, "a", lambda: 0)
    assert held is not None and held.text().startswith("snoozed until ")
    assert (
        describe(board.snoozes["a"], "R2")
        == f"'R2' (a) is snoozed until {(NOW + 168 * H).isoformat()} (by ceo)"
    )
    assert describe(board.snoozes["m"]).endswith("(from marker)")
    assert describe(replace(board.snoozes["a"], by=None)).endswith("+00:00")


# --- rule 13 -------------------------------------------------------------------------------


def test_a_snoozed_item_leaves_the_digest_for_its_footer() -> None:
    tasks = [question("t_1", NOW - 3 * H), question("t_2", NOW - 2 * H)]
    plain = NY.evaluate(snap(tasks))
    (digest,) = NY.evaluate(snap(tasks, [snooze("t_1", behind=None, source="marker")]))
    assert plain[0].key == "needs_you:ceo:t_1,t_2"
    assert digest.key == "needs_you:ceo:t_2" and digest.state == plain[0].state
    assert digest.params["approvals"] == ["t_2"] and digest.params["waiting"] == ["t_2"]
    assert digest.params["waiting_agent_tasks"] == 1 and digest.params["cost_hours"] == 2.0
    assert [i["task_id"] for i in digest.params["items"]] == ["t_2"]
    assert "snooze" not in digest.params["items"][0]
    assert digest.since == NOW - 2 * H  # the oldest counted wait
    (footer,) = digest.params["snoozed"]
    assert footer["task_id"] == "t_1" and footer["title"] == "agent t_1"
    assert footer["snooze"]["state"] == "snoozed" and footer["snooze"]["source"] == "marker"
    assert footer["waiting"] == ["t_1"]  # what is parked on it, as ``waiting`` counts it
    assert digest.params["snoozed_total"] == 1
    assert digest.reason.endswith("Snoozed, not counted: 1 item(s).")


def test_a_snoozed_wait_does_not_raise_the_severity() -> None:
    old = [question("t_1", NOW - 30 * H), question("t_2", NOW - 2 * H)]
    assert NY.evaluate(snap(old))[0].severity.value == "critical"
    (digest,) = NY.evaluate(snap(old, [snooze("t_1")]))
    assert digest.severity.value == "warning"


def test_a_digest_of_only_snoozed_items_does_not_open() -> None:
    tasks = [question("t_1", NOW - 3 * H)]
    board = snap(tasks, [snooze("t_1")])
    assert NY.evaluate(board) == []
    text = NY.resolved("needs_you:ceo", board)
    assert text is not None
    assert text.startswith("Everything waiting for ceo is snoozed: 'agent t_1' (t_1) is snoozed")
    assert "(deferred to the release, by ceo)" in text


def test_the_resolve_notice_is_the_engines_unless_everything_is_snoozed() -> None:
    tasks = [question("t_1", NOW - 3 * H), question("t_2", NOW - 2 * H)]
    assert NY.resolved("needs_you:ceo", snap(tasks, [snooze("t_1")])) is None  # t_2 is live
    assert NY.resolved("needs_you:ceo", snap(tasks)) is None  # nothing snoozed
    assert NY.resolved("stale_blocks:t_1", snap(tasks, [snooze("t_1"), snooze("t_2")])) is None
    many = [question(f"t_{n}", NOW - 3 * H) for n in range(7)]
    text = NY.resolved("needs_you:ceo", snap(many, [snooze(t.id) for t in many]))
    assert text is not None and "; and 2 more." in text
    unassigned = NeedsYou().resolved(
        "needs_you:unassigned", snap(tasks, [snooze("t_1"), snooze("t_2")])
    )
    assert unassigned is not None and unassigned.startswith(
        "Everything waiting for nobody assigned"
    )


def test_an_expired_snooze_brings_its_item_back_marked() -> None:
    tasks = [question("t_1", NOW - 3 * H)]
    (digest,) = NY.evaluate(snap(tasks, [snooze("t_1", until=NOW - 2 * H)]))
    (item,) = digest.params["items"]
    assert item["task_id"] == "t_1"
    assert item["snooze"]["state"] == "expired"
    assert item["snooze"]["until"] == (NOW - 2 * H).isoformat()
    assert digest.params["snoozed"] == [] and digest.params["snoozed_total"] == 0
    assert "(its snooze expired at" in digest.reason


def test_growth_behind_a_snoozed_item_brings_it_back_marked() -> None:
    tasks = [
        question("t_1", NOW - 3 * H),
        Task("t_2", "next", Status.BACKLOG, NOW - 3 * H, role="ops"),
        Task("t_3", "after", Status.BACKLOG, NOW - 3 * H, role="ops"),
    ]
    deps = [Dependency("t_2", "t_1"), Dependency("t_3", "t_1")]
    held = NY.evaluate(snap(tasks[:2], [snooze("t_1", behind=1)], deps[:1]))
    assert held == []  # as much work behind it as when it was snoozed
    (digest,) = NY.evaluate(snap(tasks, [snooze("t_1", behind=1)], deps))
    (item,) = digest.params["items"]
    assert item["snooze"]["state"] == "grown" and item["snooze"]["grown"] == 1
    assert "1 more task(s) blocked since its snooze" in digest.reason


def test_the_engine_announces_a_snooze_once_and_nothing_while_it_holds() -> None:
    """The issue's complaint: no repeat alerts for a deliberate wait. Setting the snooze
    changes the digest once ("changed", the item in the footer); while it holds the ticks
    add nothing, not even at an escalation step; the expiry brings the item back."""
    tasks = [question("t_1", NOW - 3 * H), question("t_2", NOW - 2 * H)]
    config = EngineConfig(
        dry_run=False,
        modes={"needs_you": Mode.SUGGEST},
        state_escalations=(timedelta(hours=4),),
    )
    alerter = FakeAlerter()
    engine = Engine([NY], adapters=[FakeAdapter()], alerter=alerter, config=config)
    until = NOW + 2 * H
    ticks = [
        snap(tasks, now=NOW),
        snap(tasks, [snooze("t_1", until=until)], now=NOW + timedelta(minutes=1)),
        snap(tasks, [snooze("t_1", until=until)], now=NOW + timedelta(minutes=30)),
        snap(tasks, [snooze("t_1", until=until)], now=NOW + timedelta(minutes=90)),
        snap(tasks, [snooze("t_1", until=until)], now=until + timedelta(minutes=1)),
    ]
    sent: list[list[tuple[Transition, tuple[str, ...], tuple[str, ...]]]] = []
    for board in ticks:
        before = len(alerter.events)
        engine.tick(lambda board=board: board)
        sent.append(
            [
                (
                    e.transition,
                    tuple(i.task_id for i in e.items),
                    tuple(i.task_id for i in e.snoozed),
                )
                for e in alerter.events[before:]
            ]
        )
    assert sent[0] == [(Transition.NEW, ("t_1", "t_2"), ())]
    assert sent[1] == [(Transition.CHANGED, ("t_2",), ("t_1",))]
    assert sent[2] == [] and sent[3] == []
    assert sent[4] == [(Transition.CHANGED, ("t_1", "t_2"), ())]
    back = alerter.events[-1].items[0]
    assert back.snooze is not None and back.snooze.state is SnoozeState.EXPIRED


# --- rule 4 --------------------------------------------------------------------------------


def blocked(tid: str, since: datetime, reason: str, cond: str | None = None) -> Task:
    return Task(
        tid, f"agent {tid}", Status.BLOCKED, since, role="ops", owner_id="agent-ops-1",
        block=Block(reason, since, cond),
    )  # fmt: skip


def test_rule_4_holds_reminders_but_never_a_broken_reference() -> None:
    tasks = [
        blocked("t_1", NOW - 10 * H, "needs_input: which region?"),
        blocked("t_2", NOW - 10 * H, "waiting for C-9", "C-9"),  # names a condition nobody sees
    ]
    board = snap(tasks, [snooze("t_1"), snooze("t_2")])
    (alert,) = StaleBlocks().evaluate(board)
    assert alert.task_id == "t_2" and "which Kelpie cannot see" in alert.reason
    assert "snooze" not in alert.params


def test_rule_4_alerts_again_when_the_work_behind_the_block_grows() -> None:
    tasks = [
        blocked("t_1", NOW - 10 * H, "waiting for merge", "C-1"),
        Task("t_2", "next", Status.BACKLOG, NOW - 3 * H, role="ops"),
        Task("t_3", "after", Status.BACKLOG, NOW - 3 * H, role="ops"),
    ]
    pr = Condition("C-1", ConditionKind.PR_MERGED, "t_1", {"pr": "1"}, False, checked_at=NOW)
    deps = [Dependency("t_2", "t_1"), Dependency("t_3", "t_1")]
    assert StaleBlocks().evaluate(snap(tasks, [snooze("t_1", behind=2)], deps, [pr])) == []
    (alert,) = StaleBlocks().evaluate(snap(tasks, [snooze("t_1", behind=1)], deps, [pr]))
    assert "limit 8h" in alert.reason
    assert alert.reason.endswith(
        "1 more task(s) blocked since its snooze (until " + (NOW + 168 * H).isoformat() + ")."
    )
    assert alert.params["snooze"]["state"] == "grown"


def test_rule_4_resolve_notice_says_the_block_is_snoozed() -> None:
    task = blocked("t_1", NOW - 10 * H, "needs_input: which region?")
    state = f"stale_blocks:t_1:{task.block.since.isoformat()}"  # type: ignore[union-attr]
    rule = StaleBlocks()
    text = rule.resolved(state, snap([task], [snooze("t_1")]))
    assert text is not None and text.startswith("'agent t_1' (t_1) is snoozed until ")
    assert text.endswith(
        "no reminders on its block until then, unless more work is blocked behind it."
    )
    assert rule.resolved(state, snap([task])) is None  # not snoozed: the engine's notice
    assert rule.resolved(state, snap([task], [snooze("t_1", until=NOW - H)])) is None
    assert rule.resolved("stale_blocks:t_9:x", snap([task], [snooze("t_1")])) is None
    young = blocked("t_1", NOW - 2 * H, "needs_input: which region?")  # no alert due yet
    state = f"stale_blocks:t_1:{young.block.since.isoformat()}"  # type: ignore[union-attr]
    assert rule.resolved(state, snap([young], [snooze("t_1")])) is None  # not the snooze's doing
    gone = blocked("t_1", NOW - 10 * H, "waiting for merge", "C-1")
    merged = Condition("C-1", ConditionKind.PR_MERGED, "t_1", {"pr": "1"}, True, NOW, NOW)
    state = f"stale_blocks:t_1:{gone.block.since.isoformat()}"  # type: ignore[union-attr]
    board = snap([gone], [snooze("t_1")], conditions=[merged])
    assert rule.resolved(state, board) is None  # its condition holds: the block's own news
    unchecked = Condition("C-1", ConditionKind.PR_MERGED, "t_1", {"pr": "1"}, False,
                          checked_at=NOW - 3 * H)  # fmt: skip
    board = snap([gone], [snooze("t_1")], conditions=[unchecked])
    assert rule.resolved(state, board) is None  # stale evidence still reported: not held


# --- who reads snoozes ---------------------------------------------------------------------

RULES = Path(__file__).parents[2] / "src" / "kelpie" / "rules"
HONOUR = {"needs_you.py", "stale_blocks.py", "wait_condition.py", "_snoozes.py"}
SNOOZE_MODULES = {"kelpie.rules._snoozes", "kelpie.core.snooze"}


SNOOZE_NAMES = {"snoozes", "_snoozes"}


def reads_snoozes(source: str, package: str = "kelpie.rules") -> bool:
    """Whether a module (in ``package``) imports the snooze helpers in any form (absolute,
    relative, as a module or from one), ``active_snooze``, reads a ``.snoozes`` attribute
    (``snapshot.snoozes``) or names one in a string (``getattr(snapshot, "snoozes")``)."""
    return any(_reads(node, package) for node in ast.walk(ast.parse(source)))


def _reads(node: ast.AST, package: str) -> bool:
    if isinstance(node, ast.ImportFrom):
        if node.level:
            parts = package.split(".")
            base = ".".join(parts[: len(parts) - node.level + 1])
            module = f"{base}.{node.module}" if node.module else base
        else:
            module = node.module or ""
        names = {f"{module}.{a.name}" for a in node.names}
        named = any(a.name == "active_snooze" for a in node.names)
        return module in SNOOZE_MODULES or bool(names & SNOOZE_MODULES) or named
    if isinstance(node, ast.Import):
        return any(a.name in SNOOZE_MODULES for a in node.names)
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in SNOOZE_NAMES
    return isinstance(node, ast.Attribute) and node.attr in SNOOZE_NAMES


@pytest.mark.parametrize("path", sorted(RULES.glob("*.py")), ids=lambda p: p.name)
def test_only_rules_13_4_and_2_read_snoozes(path: Path) -> None:
    """Agent-health rules (loop_breaker, no_progress, exit_contract, orphan_status, ...) never
    honour a snooze, a person's or a marker's: only rules 13, 4 and 2 read them, for their
    repeat human-wait alerts."""
    assert reads_snoozes(path.read_text(encoding="utf-8")) == (path.name in HONOUR), path.name


def test_the_check_sees_every_way_of_reading_snoozes() -> None:
    assert reads_snoozes("from kelpie.rules._snoozes import check")
    assert reads_snoozes("from kelpie.core.snooze import EXPIRED_KEPT")
    assert reads_snoozes("import kelpie.core.snooze")
    assert reads_snoozes("from kelpie.core.model import active_snooze")
    assert reads_snoozes("def f(s):\n    return s.snoozes")
    assert reads_snoozes("from kelpie.rules import _snoozes")
    assert reads_snoozes("from . import _snoozes")
    assert reads_snoozes("from ._snoozes import checker")
    assert reads_snoozes("from .. import core\nfrom ..core import snooze")
    assert reads_snoozes("from kelpie.core import snooze")
    assert reads_snoozes("import kelpie.rules._snoozes as s")
    assert reads_snoozes("def f(s):\n    return getattr(s, 'snoozes')")
    assert not reads_snoozes("# snapshot.snoozes is not read here\nx = 'snoozed'")
    assert not reads_snoozes('"""A docstring that mentions snoozes."""')
    assert not reads_snoozes("from kelpie.core import model\nfrom . import _tasks")


def test_rule_13_and_rule_4_still_reach_a_snoozed_task_exactly_once() -> None:
    """Rule 4 leaves rule 13's items to it, snoozed or not: a snoozed approval is in the
    footer, never in a rule 4 alert too."""
    tasks = [question("t_1", NOW - 10 * H), question("t_2", NOW - 10 * H)]
    board = snap(tasks, [snooze("t_1")])
    four = StaleBlocks(inbox=NY).evaluate(board)
    (digest,) = NY.evaluate(board)
    assert four == []
    assert digest.kind is ActionKind.ESCALATE
    assert [s["task_id"] for s in digest.params["snoozed"]] == ["t_1"]
    assert digest.params["approvals"] == ["t_2"]


# --- the growth baseline -------------------------------------------------------------------


def test_the_work_behind_counts_open_tasks_through_dependencies_and_conditions() -> None:
    h = Task("H-1", "sign", Status.READY, NOW - 9 * H, role="owner", owner_id="ceo")
    by_condition = blocked("A-1", NOW - 5 * H, "waiting_human: H-1")
    by_block = blocked("A-2", NOW - 5 * H, "waiting_human: sign", "C-2")
    later = Task("A-3", "after", Status.BACKLOG, NOW - 5 * H, role="ops")
    done = Task("A-4", "done", Status.DONE, NOW - 5 * H, role="ops")
    conditions = [
        Condition("C-1", ConditionKind.HUMAN_TASK, "A-1", {"task": "H-1"}, False),
        Condition("C-2", ConditionKind.HUMAN_TASK, None, {"task": "H-1"}, False),
        Condition("C-3", ConditionKind.PR_MERGED, "A-1", {"task": "H-1"}, False),  # no task ref
    ]
    deps = [Dependency("A-3", "A-1"), Dependency("A-4", "H-1"), Dependency("H-1", "H-1")]
    board = snap([h, by_condition, by_block, later, done], deps=deps, conditions=conditions)
    assert behind_count(board, "H-1") == 3  # A-1, A-2 and, behind A-1, A-3; not the done A-4
    assert behind_count(board, "A-1") == 1 and behind_count(board, "nowhere") == 0


def test_the_work_behind_does_not_flap_with_status_or_sessions() -> None:
    h = Task("H-1", "sign", Status.READY, NOW - 9 * H, role="owner", owner_id="ceo")
    dep = Task("D-1", "next", Status.BACKLOG, NOW - H, role="ops")
    deps = [Dependency("D-1", "H-1")]
    working = replace(dep, status=Status.IN_PROGRESS)
    session = Session("S-1", "D-1", "agent-ops-1", NOW - H, last_heartbeat=NOW)
    live = replace(snap([h, working], deps=deps), sessions=(session,))
    counts = {
        behind_count(snap([h, dep], deps=deps), "H-1"),
        behind_count(live, "H-1"),
        behind_count(snap([h, working], deps=deps), "H-1"),
    }
    assert counts == {1}
    finished = replace(dep, status=Status.DONE)
    assert behind_count(snap([h, finished], deps=deps), "H-1") == 0


# --- dead ends on a snoozed item -----------------------------------------------------------

FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"


def test_a_dead_end_on_a_snoozed_item_is_announced_exactly_once() -> None:
    """The person's snoozed task whose PR closed unmerged leaves the footer for the digest,
    and rule 4 leaves it to the digest: one announcement, not none and not two."""
    incident = load(FIXTURES / "13-snoozed-item-dead-end.yaml")
    board, inbox = incident.snapshot, build_needs_you(dict(incident.config))
    (digest,) = inbox.evaluate(board)
    (item,) = digest.params["items"]
    assert item["task_id"] == "H-1" and "never clear on its own" in item["needed"]
    assert "snooze" not in item  # live because of the news, not marked as back
    assert [i["task_id"] for i in digest.params["snoozed"]] == ["H-2"]
    assert [a for a in StaleBlocks(inbox=inbox).evaluate(board) if a.task_id == "H-1"] == []


# --- rule 2 --------------------------------------------------------------------------------


def waiting(tid: str, since: datetime) -> Task:
    return Task(tid, f"agent {tid}", Status.WAITING, since, role="ops", owner_id="agent-ops-1")


def test_rule_2_holds_the_overdue_reminder_of_a_snoozed_human_task_only() -> None:
    h = Task("H-1", "sign", Status.READY, NOW - 40 * H, role="owner", owner_id="ceo")
    cond = Condition("C-1", ConditionKind.HUMAN_TASK, "W-1", {"task": "H-1"}, False, checked_at=NOW)
    rule = WaitCondition()
    board = [h, waiting("W-1", NOW - 30 * H)]
    (overdue,) = rule.evaluate(snap(board, conditions=[cond]))
    assert overdue.key is not None and overdue.key.startswith("overdue:")
    assert rule.evaluate(snap(board, [snooze("H-1", behind=1)], conditions=[cond])) == []
    assert rule.evaluate(snap(board, [snooze("H-1", behind=None)], conditions=[cond])) == []
    expired = snooze("H-1", until=NOW - H)
    assert len(rule.evaluate(snap(board, [expired], conditions=[cond]))) == 1
    own = snooze("W-1")  # the waiting task's own snooze: not the person's deferral
    assert len(rule.evaluate(snap(board, [own], conditions=[cond]))) == 1
    grown = [*board, Task("D-1", "d", Status.BACKLOG, NOW, role="ops")]
    deps = [Dependency("D-1", "W-1")]
    assert len(rule.evaluate(snap(grown, [snooze("H-1", behind=1)], deps, [cond]))) == 1
    held = [snooze("H-1", behind=1)]
    unchecked = replace(cond, satisfied=None)  # not known to be unmet
    assert len(rule.evaluate(snap(board, held, conditions=[unchecked]))) == 1
    broken = replace(cond, params={"task": "H-1", "error": "not_human_task"})
    assert len(rule.evaluate(snap(board, held, conditions=[broken]))) == 1


# --- the engine, across ticks --------------------------------------------------------------


def ticks(rules: list[Any], boards: list[Snapshot]) -> list[list[tuple[Transition, str]]]:
    config = EngineConfig(
        dry_run=False,
        modes={"needs_you": Mode.SUGGEST, "stale_blocks": Mode.SUGGEST},
        state_escalations=(timedelta(hours=4), timedelta(hours=24)),
        state_resolve_after=timedelta(minutes=10),
    )
    alerter = FakeAlerter()
    engine = Engine(rules, adapters=[FakeAdapter()], alerter=alerter, config=config)
    out = []
    for board in boards:
        before = len(alerter.events)
        engine.tick(lambda board=board: board)
        out.append([(e.transition, e.reason) for e in alerter.events[before:]])
    return out


def test_an_all_snoozed_inbox_resolves_with_the_hook_and_reopens_on_expiry() -> None:
    task = question("t_1", NOW - 3 * H)
    until = NOW + 6 * H
    held = snooze("t_1", until=until)
    m = timedelta(minutes=1)
    sent = ticks(
        [NY],
        [
            snap([task], now=NOW),
            snap([task], [held], now=NOW + m),
            snap([task], [held], now=NOW + 15 * m),
            snap([task], [held], now=NOW + 5 * H),
            snap([task], [held], now=until + m),
            snap([task], [held], now=until + 2 * H),
        ],
    )
    assert [t for t, _ in sent[0]] == [Transition.NEW]
    assert sent[1] == []  # unseen for a minute: not resolved yet
    ((resolved, why),) = sent[2]
    assert resolved is Transition.RESOLVED
    assert why.startswith("Everything waiting for ceo is snoozed: 'agent t_1' (t_1)")
    assert sent[3] == []
    ((opened, reason),) = sent[4]
    assert opened is Transition.NEW and "its snooze expired" in reason
    assert sent[5] == []  # a new state, with its own escalation steps


def test_growth_over_ticks_re_alerts_once_and_a_status_toggle_never_does() -> None:
    t1, t2 = question("t_1", NOW - 3 * H), question("t_2", NOW - 2 * H)
    dep = Task("d_1", "next", Status.BACKLOG, NOW, role="ops")
    m = timedelta(minutes=20)
    held = snooze("t_1", behind=1)
    one = [Dependency("d_1", "t_1")]
    boards = [snap([t1, t2, dep], [held], one, now=NOW)]
    for k in range(1, 5):  # the dependent toggles between the backlog and work
        moved = dep if k % 2 == 0 else replace(dep, status=Status.IN_PROGRESS)
        boards.append(snap([t1, t2, moved], [held], one, now=NOW + k * m))
    more = Task("d_2", "more", Status.BACKLOG, NOW, role="ops")
    two = [*one, Dependency("d_2", "t_1")]
    boards.append(snap([t1, t2, dep, more], [held], two, now=NOW + 5 * m))
    boards.append(snap([t1, t2, dep, more], [held], two, now=NOW + 6 * m))
    sent = ticks([NY], boards)
    assert [t for t, _ in sent[0]] == [Transition.NEW]
    assert sent[1:5] == [[], [], [], []]
    ((changed, reason),) = sent[5]
    assert changed is Transition.CHANGED and "1 more task(s) blocked since its snooze" in reason
    assert sent[6] == []
