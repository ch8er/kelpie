"""GitHub connector (stub): PR merged / CI status conditions."""

from __future__ import annotations

from dataclasses import dataclass, field

from kelpie.core.model import Condition, ConditionKind


@dataclass(slots=True)
class GitHubConnector:
    name: str = "github"
    kinds: frozenset[ConditionKind] = field(
        default=frozenset({ConditionKind.PR_MERGED, ConditionKind.CI_STATUS})
    )

    def check(self, condition: Condition) -> Condition:
        raise NotImplementedError
