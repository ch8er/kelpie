from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from kelpie.core.actions import Action
from kelpie.core.model import Snapshot
from kelpie.rules import PLANNED, REGISTRY, register


@dataclass
class _Noop:
    name: str = "_noop"

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        return []


def test_register_and_reject_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kelpie.rules.REGISTRY", dict(REGISTRY))
    import kelpie.rules as rules

    def factory(config: dict[str, Any]) -> _Noop:
        return _Noop()

    assert register("_noop")(factory) is factory
    assert rules.REGISTRY["_noop"]({}).name == "_noop"
    with pytest.raises(ValueError, match="already registered"):
        register("_noop")(factory)


def test_planned_rules_are_unique() -> None:
    names = [r.name for r in PLANNED]
    numbers = [r.number for r in PLANNED if r.number is not None]
    assert len(names) == len(set(names))
    assert len(numbers) == len(set(numbers))


def test_plan_numbering_is_complete() -> None:
    # Rule 8 is the daily report, implemented in kelpie.report rather than as a rule.
    numbers = {r.number for r in PLANNED if r.number is not None}
    assert numbers == set(range(1, 15)) - {8}


def test_every_rule_module_is_imported() -> None:
    import pkgutil
    import sys

    import kelpie.rules as rules

    for module in pkgutil.iter_modules(rules.__path__):
        assert f"kelpie.rules.{module.name}" in sys.modules
