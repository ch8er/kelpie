"""Suggestions check what the farm already has (live review 2, incident 20: a farm using
auto-merge was told to enable it)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import product
from pathlib import Path

import pytest

from kelpie.app import load_settings
from kelpie.core.config import Mode
from kelpie.core.model import Owner, Status, Task
from kelpie.report import farm
from kelpie.report.cli import farm_facts
from kelpie.report.daily import CHAOS_CAUSES, ReportInputs, build_report, remedy, render_markdown
from kelpie.report.farm import UNKNOWN_FARM, FarmFacts
from kelpie.report.waits import WAIT_CAUSES, suggestion

LACKING = FarmFacts(
    approvers=False, hold_expiry=False, hold_owners=False, block_conditions=False, budgets=False
)
HAVING = FarmFacts(
    approvers=True, hold_expiry=True, hold_owners=True, block_conditions=True, budgets=True
)
ALL_FACTS = [
    FarmFacts(a, frozenset(), e, o, b, g)
    for a, e, o, b, g in product((None, False, True), repeat=5)
]


@pytest.mark.parametrize("facts", ALL_FACTS)
def test_merge_automation_is_never_advised(facts: FarmFacts) -> None:
    texts = [suggestion(c, facts) for c in WAIT_CAUSES]
    texts += [
        remedy(r, facts, can, acts)
        for r in CHAOS_CAUSES
        for can, acts in ((True, False), (False, False), (True, True))
    ]
    assert not any("auto-merge" in t or "automerge" in t for t in texts)
    assert "red or queued checks" in suggestion("merge", facts)


def test_advice_to_configure_needs_the_farm_to_lack_it() -> None:
    for facts, expected in ((LACKING, True), (HAVING, False), (UNKNOWN_FARM, False)):
        assert ("approvers" in farm.people(facts)) is expected
        assert ("an owner and an expiry" in farm.holds(facts)) is expected
        assert ("[[budgets]]" in farm.runaway_by_hand(facts)) is expected
        assert ("[[budgets]]" in farm.runaway_acting(facts)) is expected
        assert ("match the block reasons" in farm.unregistered_waits(facts)) is expected
    assert "extend [[block_conditions]]" in farm.stale_blocks(HAVING, can_act=True)
    assert "attach conditions" in farm.stale_blocks(LACKING, can_act=True)
    assert "could not tie" in farm.stale_blocks(UNKNOWN_FARM, can_act=True)
    assert "Kelpie can tell" in farm.stale_blocks(LACKING, can_act=False)
    # hold texts name only what is missing, and never promise an alert
    assert "an expiry (" in farm.holds(FarmFacts(hold_expiry=False, hold_owners=True))
    assert "an owner (" in farm.holds(FarmFacts(hold_expiry=True, hold_owners=False))
    assert all("alerted" not in farm.holds(f) for f in ALL_FACTS)


def test_approvers_per_role() -> None:
    some = FarmFacts(approvers=False, approver_roles=frozenset({"developer"}))
    assert "approvers" not in farm.people(some, "developer")
    assert "agent roles that have none" in farm.people(some, "reviewer")
    assert "agent roles that have none" in farm.people(some)  # the chaos line: no role


def test_a_rule_already_acting_is_not_told_to_act() -> None:
    assert remedy("loop_breaker", HAVING, True, acts=True) == farm.runaway_acting(HAVING)
    assert "act mode" not in remedy("loop_breaker", HAVING, True, acts=True)
    assert "let orphan_status" not in remedy("orphan_status", HAVING, True, acts=True)
    assert "act mode" in remedy("loop_breaker", HAVING, True)  # observing: it may act
    assert remedy("loop_breaker", HAVING, False) == farm.runaway_by_hand(HAVING)
    assert remedy("bottlenecks:hold", HAVING, True) == farm.holds(HAVING)
    assert remedy("needs_you", HAVING, True) == farm.people(HAVING)


def config(tmp_path: Path, text: str) -> FarmFacts:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return farm_facts(load_settings(path))


def test_the_report_reads_the_facts_from_config(tmp_path: Path) -> None:
    assert config(tmp_path, "") == FarmFacts(False, frozenset(), False, False, False, False)
    full = config(
        tmp_path,
        '[engine.rules.needs_you]\ndefault_approver = "person-a"\n'
        '[engine.rules.needs_you.approvers]\nreviewer = "person-b"\n'
        '[engine.rules.bottlenecks]\nhold_labels = ["freeze"]\nhold_expiry = "4h"\n'
        '[[budgets]]\nname = "day"\nscope = "day"\nunit = "tokens"\nsoft = 5000\n'
        '[connectors.github]\nrepos = ["example/app"]\n'
        '[[block_conditions]]\npattern = "merge of #(?P<pr>\\\\d+)"\nkind = "pr_merged"\n',
    )
    assert full == FarmFacts(True, frozenset({"reviewer"}), True, False, True, True)


def test_holds_facts_are_per_label(tmp_path: Path) -> None:
    mixed = config(
        tmp_path,
        '[engine.rules.bottlenecks]\nhold_labels = ["b"]\n'
        '[[engine.rules.bottlenecks.holds]]\nlabel = "a"\nowner = "person-a"\n',
    )
    assert (mixed.hold_expiry, mixed.hold_owners) == (False, False)  # "b" never expires
    owned = config(
        tmp_path,
        '[[engine.rules.bottlenecks.holds]]\nlabel = "a"\nowner = "person-a"\n'
        '[[engine.rules.bottlenecks.holds]]\nlabel = "c"\nowner = "person-b"\n',
    )
    assert (owned.hold_expiry, owned.hold_owners) == (True, True)


def test_dispatch_gate_patterns_are_not_block_conditions(tmp_path: Path) -> None:
    facts = config(
        tmp_path,
        '[connectors.github]\nrepos = ["example/app"]\n'
        '[[block_conditions]]\npattern = "after #(?P<pr>\\\\d+)"\nkind = "pr_merged"\n'
        'applies_to = ["ready_task"]\n',
    )
    assert facts.block_conditions is False


def report_with(**kw: object) -> str:
    inputs = ReportInputs(day=date(2026, 3, 2), tz=UTC, **kw)  # type: ignore[arg-type]
    return render_markdown(build_report(inputs, sections={}))


def test_the_biggest_wait_and_the_advice_follow_the_facts() -> None:
    held = Task("T-1", "t", Status.WAITING, datetime(2026, 3, 2, 1, tzinfo=UTC),
                role="developer", labels=("freeze",))  # fmt: skip
    having = FarmFacts(hold_expiry=True, hold_owners=True)
    markdown = report_with(
        tasks=(held,), owners=(Owner("dev-1", "developer"),), hold_labels=("freeze",), farm=having
    )
    assert farm.holds(having) in markdown and "an owner and an expiry" not in markdown


def test_a_merge_wait_in_the_report_is_diagnosed_not_automated() -> None:
    from kelpie.core.model import Condition, ConditionKind

    task = Task("T-2", "t", Status.WAITING, datetime(2026, 3, 2, 1, tzinfo=UTC), role="reviewer")
    wait = Condition("m", ConditionKind.PR_MERGED, "T-2", {"repo": "example/app"})
    markdown = report_with(tasks=(task,), conditions=(wait,), modes={"loop_breaker": Mode.ACT})
    assert "PRs" in markdown and farm.merges() in markdown
    assert "auto-merge" not in markdown


def test_a_filter_the_farm_already_runs_is_not_installed_again() -> None:
    from kelpie.report.daily import _recommendations
    from kelpie.traces.outputs import DEFAULT_FILTERS, OutputGroup, Recommendation

    rtk = next(f for f in DEFAULT_FILTERS if f.name == "rtk")
    group = OutputGroup(role="developer", key="cargo test", calls=40, tokens=400_000,
                        max_tokens=20_000, estimated=False)  # fmt: skip
    rec = Recommendation(group, rtk, 240_000, repeated=False, owner=None, platform_known=False)
    inputs = ReportInputs(day=date(2026, 3, 2), tz=UTC)
    from kelpie.report.daily import _Pricer

    def advice(in_use: frozenset[str]) -> str:
        [a] = _recommendations(inputs, (), (), (rec,), _Pricer(inputs.prices), {}, (), None, in_use)
        return a.detail

    assert "install RTK" in advice(frozenset())
    assert "install RTK" not in advice(frozenset({"rtk"}))
    assert "route it through rtk" in advice(frozenset({"rtk"}))
