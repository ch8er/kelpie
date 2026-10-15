"""Control rules. Each rule is a pure function over a :class:`~kelpie.core.model.Snapshot`.

Implemented rules register themselves in :data:`REGISTRY`. The incident fixture suite
(``tests/fixtures/incidents``) runs every fixture whose ``rule`` is registered and skips
the rest, so a fixture starts gating CI the moment its rule lands.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kelpie.core.rule import Rule

RuleFactory = Callable[[dict[str, Any]], Rule]

REGISTRY: dict[str, RuleFactory] = {}


def register(name: str) -> Callable[[RuleFactory], RuleFactory]:
    def decorator(factory: RuleFactory) -> RuleFactory:
        if name in REGISTRY:
            raise ValueError(f"rule already registered: {name}")
        REGISTRY[name] = factory
        return factory

    return decorator


@dataclass(frozen=True, slots=True)
class PlannedRule:
    name: str
    summary: str
    incidents: tuple[int, ...]


# Numbering follows docs/plan.md ("P0. MVP — core rules"); rule 8 is the daily report.
PLANNED: tuple[PlannedRule, ...] = (
    PlannedRule(
        "exit_contract", "1. Session ended without final status / bad block -> requeue", (2,)
    ),
    PlannedRule(
        "wait_condition", "2. Register condition and exit; detect in-session waiting", (10,)
    ),
    PlannedRule(
        "orphan_status", "3. Status without owner longer than N min -> alert/reassign", (11,)
    ),
    PlannedRule("stale_blocks", "4. Block cause resolved externally -> unblock", (1, 8)),
    PlannedRule("ping_pong", "5. More than K review rounds -> escalate", (12,)),
    PlannedRule("bottlenecks", "6. Idle time per role, humans in the task graph", (9, 10, 13)),
    PlannedRule("resources", "7. Disk, memory, processes, provider quotas, auth expiry", (3, 4, 6)),
    PlannedRule("ci_economy", "9. Wasted CI builds, superseded runs, quota forecast", (5,)),
    PlannedRule("return_causes", "Root causes of task returns -> recommendation", (7,)),
    PlannedRule("checkpoints", "Commit reminder, WIP commit on exit, resume from commit", ()),
)


def load_rules() -> None:
    """Import every submodule so their ``@register`` decorators run."""
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")


load_rules()
