"""Control rules. Each rule is a pure function over a :class:`~kelpie.core.model.Snapshot`.

Implemented rules register themselves in :data:`REGISTRY`. The incident fixture suite
(``tests/fixtures/incidents``) runs every fixture whose ``rule`` is registered and skips
the rest, so a fixture starts gating CI the moment its rule lands.
"""

from __future__ import annotations

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


PLANNED: tuple[PlannedRule, ...] = (
    PlannedRule("exit_contract", "Session ended without final status / bad block -> requeue", (2,)),
    PlannedRule("wait_condition", "Register condition and exit; detect in-session polling", (10,)),
    PlannedRule("orphan_status", "Status without owner longer than N min -> alert/reassign", (11,)),
    PlannedRule("stale_blocks", "Block cause resolved externally -> unblock", (1, 8)),
    PlannedRule("ping_pong", "More than K review rounds -> escalate", (12,)),
    PlannedRule("bottlenecks", "Idle time and bottlenecks by role", (9, 13)),
    PlannedRule("resources", "Disk, memory, processes, LLM quotas, expiring auth", (3, 4, 5, 6)),
    PlannedRule("file_contention", "Shared-file conflicts driving returns", (7,)),
)
