"""Human task connector (stub): conditions that wait on a person."""

from __future__ import annotations

from dataclasses import dataclass, field

from kelpie.core.model import Condition, ConditionKind


@dataclass(slots=True)
class HumanTaskConnector:
    name: str = "human_tasks"
    kinds: frozenset[ConditionKind] = field(default=frozenset({ConditionKind.HUMAN_TASK}))

    def check(self, condition: Condition) -> Condition:
        raise NotImplementedError
