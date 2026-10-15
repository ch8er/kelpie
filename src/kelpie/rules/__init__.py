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
from enum import StrEnum
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


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


@dataclass(frozen=True, slots=True)
class PlannedRule:
    """A rule from docs/plan.md. ``number`` is the plan's rule number, if it has one."""

    name: str
    number: int | None
    priority: Priority
    summary: str
    incidents: tuple[int, ...] = ()


_P0, _P1, _P2 = Priority.P0, Priority.P1, Priority.P2

# Mirrors docs/plan.md, which wins on any conflict. Rule 8 is the daily report (report/).
# Rules 1 and 3 gain P1 extensions (evidence for "done", stop patterns) under the same name.
PLANNED: tuple[PlannedRule, ...] = (
    # First: a WIP commit must happen before any rule requeues the task.
    PlannedRule("checkpoints", None, _P0, "Commit reminder, WIP commit on exit, resume"),
    PlannedRule("exit_contract", 1, _P0, "No final status / bad block -> requeue", (2,)),
    PlannedRule("wait_condition", 2, _P0, "Register condition and exit; wake the task", (10,)),
    PlannedRule("orphan_status", 3, _P0, "Ownerless status > N min -> alert/reassign", (11,)),
    PlannedRule("stale_blocks", 4, _P0, "Block cause resolved externally -> unblock", (1, 8)),
    PlannedRule("ping_pong", 5, _P0, "More than K review rounds -> escalate", (12,)),
    PlannedRule("bottlenecks", 6, _P0, "Idle time per role, holds, human gates", (9, 10, 14, 16)),
    PlannedRule("resources", 7, _P0, "Disk, memory, processes, quotas, auth expiry", (3, 4, 6)),
    PlannedRule("ci_economy", 9, _P0, "Wasted CI builds, superseded runs, quota forecast", (5,)),
    PlannedRule("loop_breaker", 10, _P0, "Identical calls, error streaks, token budget -> stop"),
    PlannedRule("no_progress", 11, _P0, "Tokens spent without status, commit or artifact"),
    PlannedRule("conflict_warning", 12, _P0, "Live tasks editing the same files -> warn", (7,)),
    PlannedRule("dispatch_gate", None, _P0, "Ready task with an unmet prerequisite -> waiting"),
    PlannedRule("needs_you", 13, _P1, "Everything waiting for a human, by cost of delay", (13,)),
    PlannedRule("compaction_gate", 14, _P2, "Gate violated right after context compaction"),
    PlannedRule("return_causes", None, _P1, "Root causes of task returns -> recommendation", (7,)),
)


def load_rules() -> None:
    """Import every submodule so their ``@register`` decorators run."""
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")


load_rules()
