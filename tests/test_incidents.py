"""Incident fixture suite. Fixtures for not-yet-implemented rules are validated and skipped."""

from __future__ import annotations

from pathlib import Path

import pytest

from kelpie.rules import PLANNED, REGISTRY
from tests.incidents import all_paths, load, matches

# Fixtures of an implemented rule that need a part of it which has not landed yet.
# They must fail until it does (strict xfail): the PR that lands the missing part removes
# the entry, and the suite tells it to if it forgets.
PENDING = {
    "06-llm-quota-and-expired-auth": "resources: provider quotas and auth expiry not implemented",
}

PATHS = all_paths()
IDS = [p.stem for p in PATHS]


def test_all_thirteen_incidents_covered() -> None:
    assert set(range(1, 14)) <= {load(p).number for p in PATHS}


@pytest.mark.parametrize("path", PATHS, ids=IDS)
def test_fixture_is_valid(path: Path) -> None:
    incident = load(path)
    assert incident.rule in {r.name for r in PLANNED}
    assert incident.expected, "fixture must expect at least one action"
    assert incident.not_expected, "fixture must include a negative case"


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(p, id=p.stem, marks=pytest.mark.xfail(strict=True, reason=PENDING[p.stem]))
        if p.stem in PENDING
        else pytest.param(p, id=p.stem)
        for p in PATHS
    ],
)
def test_rule_handles_incident(path: Path) -> None:
    incident = load(path)
    if incident.rule not in REGISTRY:
        pytest.skip(f"rule {incident.rule!r} not implemented yet")
    rule = REGISTRY[incident.rule](incident.config)
    actions = list(rule.evaluate(incident.snapshot))
    for spec in incident.expected:
        assert any(matches(a, spec) for a in actions), f"missing {spec}; got {actions}"
    for spec in incident.not_expected:
        assert not any(matches(a, spec) for a in actions), f"unexpected {spec}; got {actions}"
