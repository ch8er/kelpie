"""LLM provider quota connector (stub): rate limits, quota resets, auth expiry."""

from __future__ import annotations

from dataclasses import dataclass, field

from kelpie.core.model import Condition, ConditionKind


@dataclass(slots=True)
class LLMQuotaConnector:
    name: str = "llm_quota"
    kinds: frozenset[ConditionKind] = field(default=frozenset({ConditionKind.QUOTA_RESET}))

    def check(self, condition: Condition) -> Condition:
        raise NotImplementedError
