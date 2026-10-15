"""Actions emitted by rules. The engine executes them through a PlatformAdapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActionKind(StrEnum):
    REQUEUE = "requeue"
    UNBLOCK = "unblock"
    COMMENT = "comment"
    WIP_COMMIT = "wip_commit"
    WAKE = "wake"
    REASSIGN = "reassign"
    ESCALATE = "escalate"
    ALERT = "alert"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Action:
    """A side effect a rule asks for. Rules only describe actions; they never run them."""

    kind: ActionKind
    rule: str
    reason: str
    task_id: str | None = None
    severity: Severity = Severity.INFO
    params: dict[str, Any] = field(default_factory=dict)
