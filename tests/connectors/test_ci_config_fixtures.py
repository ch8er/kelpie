"""Rule 9's waste kinds, each through to the workflow diff Kelpie would propose.

Each fixture in ``tests/fixtures/ci_config/`` is a snapshot whose builds waste minutes in one
way (``waste``: a :data:`kelpie.rules.ci_economy.CATEGORIES` entry, or ``all``), a workflow
file, and the expected outcome: the changes the rule names, the ones the GitHub renderer
places and leaves out, and the exact diff (``<id>.diff`` beside it; empty when nothing is
placed); ``snapshot.builds_from`` takes the builds of the fixtures it names. The diff must
be the same byte for byte on every run."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from kelpie.connectors.github.workflow_config import render
from kelpie.core.actions import Action, ActionKind
from kelpie.core.serde import snapshot_from_dict
from kelpie.rules import REGISTRY
from kelpie.rules.ci_economy import CATEGORIES

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ci_config"
PATHS = sorted(FIXTURES.glob("*.yaml"))


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict) and data["id"] == path.stem
    snapshot = data["snapshot"]
    # ``builds_from``: the builds of the other fixtures named, together.
    for other in snapshot.pop("builds_from", ()):
        snapshot.setdefault("builds", []).extend(
            load(FIXTURES / f"{other}.yaml")["snapshot"]["builds"]
        )
    return data


def proposal(fixture: dict[str, Any]) -> Action:
    rule = REGISTRY["ci_economy"](fixture["config"])
    found = [
        a
        for a in rule.evaluate(snapshot_from_dict(fixture["snapshot"]))
        if a.kind is ActionKind.PROPOSE_CI_CONFIG
    ]
    assert len(found) == 1, found
    return found[0]


def test_every_waste_kind_has_a_fixture() -> None:
    kinds = {load(p)["waste"] for p in PATHS}
    assert kinds == {*CATEGORIES, "all"}


@pytest.mark.parametrize("path", PATHS, ids=lambda p: p.stem)
def test_waste_kind_renders_the_expected_diff(path: Path) -> None:
    fixture = load(path)
    expected = fixture["expected"]
    action = proposal(fixture)
    changes = action.params["changes"]
    assert [c["change"] for c in changes] == expected["changes"]
    workflow, default = fixture["workflow"], fixture["default_branch"]
    rendered = render(workflow, changes, default)
    assert list(rendered.applied) == expected["applied"]
    assert [list(x) for x in rendered.left_out] == expected["left_out"]
    diff = rendered.diff(workflow, action.params["workflow"])
    assert diff == path.with_suffix(".diff").read_text()
    # Deterministic: the same snapshot gives the same proposal and the same bytes.
    again = proposal(fixture)
    assert again == action
    assert render(workflow, again.params["changes"], default).diff(workflow, "x") == (
        rendered.diff(workflow, "x")
    )
    if rendered.applied:
        # What is written still parses, and a second pass places nothing more.
        assert yaml.safe_load(rendered.text)
        assert not render(rendered.text, changes, default).applied
