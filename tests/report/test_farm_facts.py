"""Suggestions check what the farm already has (live review 2, incident 20: a farm using
auto-merge was told to enable it)."""

from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest

from kelpie.report import farm
from kelpie.report.daily import CHAOS_CAUSES, remedy
from kelpie.report.farm import UNKNOWN_FARM, FarmFacts
from kelpie.report.waits import WAIT_CAUSES, suggestion

ALL_FACTS = [
    FarmFacts(*values) for values in product((None, False, True), repeat=4)
]  # every combination of approvers, hold_expiry, block_conditions, budgets


@pytest.mark.parametrize("facts", ALL_FACTS)
def test_merge_automation_is_never_advised(facts: FarmFacts) -> None:
    texts = [suggestion(c, facts) for c in WAIT_CAUSES]
    texts += [remedy(r, facts, can) for r in CHAOS_CAUSES for can in (True, False)]
    assert not any("auto-merge" in t or "automerge" in t for t in texts)
    assert "red or queued checks" in suggestion("merge", facts)


def test_advice_to_configure_needs_the_farm_to_lack_it() -> None:
    lacking = FarmFacts(approvers=False, hold_expiry=False, block_conditions=False, budgets=False)
    having = FarmFacts(approvers=True, hold_expiry=True, block_conditions=True, budgets=True)
    for facts, expected in ((lacking, True), (having, False), (UNKNOWN_FARM, False)):
        assert ("approvers" in farm.people(facts)) is expected
        assert ("an owner and an expiry" in farm.holds(facts)) is expected
        assert ("[[budgets]]" in farm.runaway_by_hand(facts)) is expected
        assert ("match the block reasons" in farm.unregistered_waits(facts)) is expected
    assert "extend [[block_conditions]]" in farm.stale_blocks(having, can_act=True)
    assert "attach conditions" in farm.stale_blocks(lacking, can_act=True)
    assert "could not tie" in farm.stale_blocks(UNKNOWN_FARM, can_act=True)
    assert "can alert" in farm.stale_blocks(lacking, can_act=False)


def test_remedies_route_through_the_facts() -> None:
    having = FarmFacts(approvers=True, hold_expiry=True, block_conditions=True, budgets=True)
    assert remedy("bottlenecks:hold", having, True) == farm.holds(having)
    assert remedy("needs_you", having, True) == farm.people(having)
    assert remedy("loop_breaker", having, False) == farm.runaway_by_hand(having)
    assert "act mode" in remedy("loop_breaker", having, True)  # it can act: say so
    assert remedy("orphan_status", having, False) != remedy("orphan_status", having, True)


def test_the_report_reads_the_facts_from_config(tmp_path: Path) -> None:
    from kelpie.app import load_settings
    from kelpie.report.cli import farm_facts

    bare = tmp_path / "bare.toml"
    bare.write_text("")
    assert farm_facts(load_settings(bare)) == FarmFacts(False, False, False, False)
    full = tmp_path / "full.toml"
    full.write_text(
        '[engine.rules.needs_you]\ndefault_approver = "person-a"\n'
        '[engine.rules.bottlenecks]\nhold_labels = ["freeze"]\nhold_expiry = "4h"\n'
        '[[budgets]]\nname = "day"\nscope = "day"\nunit = "tokens"\nsoft = 5000\n'
    )
    assert farm_facts(load_settings(full)) == FarmFacts(True, True, False, True)


def test_the_biggest_wait_and_the_advice_follow_the_facts() -> None:
    from datetime import UTC, date, datetime

    from kelpie.core.model import Owner, Status, Task
    from kelpie.report.daily import ReportInputs, build_report, render_markdown

    held = Task("T-1", "t", Status.WAITING, datetime(2026, 3, 2, 1, tzinfo=UTC),
                role="developer", labels=("freeze",))  # fmt: skip
    having = FarmFacts(hold_expiry=True)
    inputs = ReportInputs(
        day=date(2026, 3, 2), tz=UTC, tasks=(held,), owners=(Owner("dev-1", "developer"),),
        hold_labels=("freeze",), farm=having,
    )  # fmt: skip
    report = build_report(inputs, sections={})
    assert report.biggest_wait is not None and report.biggest_wait.cause == "hold"
    assert farm.holds(having) in render_markdown(report)
    assert "an owner and an expiry" not in render_markdown(report)
