"""Issue 142: the report's "Let <rule> act" advice is a config ``kelpie serve`` starts with,
on every kind of farm; where a rule cannot act, the report says what is missing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from kelpie.app import ConfigError, act_blockers, build_adapters, build_app, load_settings
from kelpie.core.config import Mode
from kelpie.core.ledger import Outcome
from kelpie.report.cli import sources
from kelpie.report.daily import CHAOS_CAUSES, FIX_KINDS, build_report
from kelpie.rules import PLANNED, REGISTRY
from tests.report.test_daily import at, day_inputs, entry, opened
from tests.test_app_adapters import FARMS

# Rules whose chaos a would-fix action of theirs could have cut: "Let <rule> act" candidates.
FIXERS = {
    p.name: p
    for p in PLANNED
    if p.name in CHAOS_CAUSES and p.name in REGISTRY and p.effects & FIX_KINDS
}
# A rule's condition id as its state is stored; loop_breaker's name a session.
STATE_IDS = {"loop_breaker": "loop_breaker:identical_calls:s-1"}


def config(tmp_path: Path, farm: str, extra: str = "") -> Path:
    path = tmp_path / "kelpie.toml"
    db = tmp_path / "kelpie.db"
    path.write_text(f'[storage]\npath = "{db}"\n[traces]\nenabled = false\n{FARMS[farm]}{extra}')
    return path


@pytest.mark.parametrize("farm", sorted(FARMS))
def test_every_act_advice_starts(
    tmp_path: Path, farm: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    monkeypatch.setenv("KELPIE_GITHUB_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setenv("KELPIE_TEST_AGENT_TOKEN", "0123456789abcdef" * 3)
    settings = load_settings(config(tmp_path, farm))
    blockers = sources(settings).act_blockers
    advised = []
    for rule, planned in sorted(FIXERS.items()):
        kind = sorted(planned.effects & FIX_KINDS)[0]
        report = build_report(
            replace(
                day_inputs(
                    actions=[entry(at(9), rule, kind, Outcome.SUGGESTED, "T-1", Mode.SUGGEST)],
                    states=[opened(STATE_IDS.get(rule, f"{rule}:T-1"), rule, at(8), at(8), at(12))],
                    modes={rule: Mode.SUGGEST},
                ),
                act_blockers=blockers,
            )
        )
        assert [line.rule for line in report.chaos] == [rule]
        titles = [a.title for a in report.recommendations]
        if f"Let {rule} act" not in titles:
            assert rule in blockers, (farm, rule)  # nothing missing, yet not advised
            continue
        advised.append(rule)
        acting = f'[engine]\ndry_run = false\n[engine.rules.{rule}]\nmode = "act"\n'
        app = build_app(load_settings(config(tmp_path, farm, acting)))  # must not refuse
        app.close()
    if farm in ("hermes", "claude code", "openclaw", "hub"):  # these farms stop runaways
        assert "loop_breaker" in advised, farm
    for rule in FIXERS:
        if rule in blockers and blockers[rule].endswith("yet"):
            assert rule not in advised


def test_a_refused_rule_is_never_advised_and_kelpie_says_why(tmp_path: Path) -> None:
    settings = load_settings(config(tmp_path, "hermes"))
    blockers = act_blockers(settings, build_adapters(settings))
    assert blockers["exit_contract"].refused
    acting = '[engine]\ndry_run = false\n[engine.rules.exit_contract]\nmode = "act"\n'
    with pytest.raises(ConfigError, match="Hermes requeues only tasks Kelpie parked"):
        build_app(load_settings(config(tmp_path, "hermes", acting)))
    report = build_report(
        day_inputs(
            actions=[
                entry(
                    at(9),
                    "exit_contract",
                    sorted(FIXERS["exit_contract"].effects & FIX_KINDS)[0],
                    Outcome.SUGGESTED,
                    "T-1",
                    Mode.SUGGEST,
                )
            ],
            states=[opened("exit_contract:T-1", "exit_contract", at(8), at(8), at(12))],
            modes={"exit_contract": Mode.SUGGEST},
            act_blockers={"exit_contract": blockers["exit_contract"].reason},
        )
    )
    titles = [a.title for a in report.recommendations]
    assert "Let exit_contract act" not in titles
    details = " ".join(a.detail for a in report.recommendations)
    assert "let exit_contract" not in details  # no act advice in the remedy either
    assert "act mode is not available here: Hermes requeues only tasks Kelpie parked" in details


def test_a_rule_acting_on_some_adapters_is_advised_with_where_it_alerts(
    tmp_path: Path,
) -> None:
    from kelpie.report.daily import render_markdown

    settings = load_settings(config(tmp_path, "claude code and github issues"))
    found = sources(settings)
    assert "stale_blocks" not in found.act_blockers  # GitHub Issues clears blocks
    rule = "stale_blocks"
    report = build_report(
        replace(
            day_inputs(
                actions=[entry(at(9), rule, "unblock", Outcome.SUGGESTED, "T-1", Mode.SUGGEST)],
                states=[opened(f"{rule}:T-1", rule, at(8), at(8), at(12))],
                modes={rule: Mode.SUGGEST},
            ),
            act_blockers=found.act_blockers,
            act_gaps=found.act_gaps,
        )
    )
    (advice,) = [a for a in report.recommendations if a.title == f"Let {rule} act"]
    gap = "Claude Code can't clear blocks yet"
    assert f"(on some tasks it would alert instead: {gap})" in advice.detail
    text = render_markdown(report)
    assert f"(on some tasks act mode would alert instead: {gap})" in text
    assert "act mode is not available here" not in text
