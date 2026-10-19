"""Actions emitted by rules. The engine executes them through a PlatformAdapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class ActionKind(StrEnum):
    REQUEUE = "requeue"
    UNBLOCK = "unblock"
    COMMENT = "comment"
    WIP_COMMIT = "wip_commit"
    WAKE = "wake"
    # Park a task in ``waiting`` on ``params.conditions`` (dispatch gate, conflict
    # serialization); rule 2 wakes it when they hold.
    WAIT = "wait"
    REASSIGN = "reassign"
    # Move a task to done: a parent whose subtasks are all finished (``SupportsClose``).
    CLOSE = "close"
    STOP = "stop"
    CANCEL_BUILD = "cancel_build"
    ESCALATE = "escalate"
    ALERT = "alert"


class ActionResult(StrEnum):
    """What an action connector made of an action (``ActionConnector.execute``)."""

    DONE = "done"  # carried out
    NOTHING_TO_DO = "nothing_to_do"  # nothing left to do (e.g. the build already finished)
    REFUSED = "refused"  # not allowed or not a known target: a human is told instead


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Action:
    """A side effect a rule asks for. Rules only describe actions; they never run them.

    ``executor`` names the platform adapter or connector that must carry it out. When it
    is empty the engine routes by the task's ``source``.

    ``key`` identifies "the same action" for de-duplication when params hold volatile
    values (e.g. ``key="disk:host-a"`` for a disk alert whose params carry ``used_pct``).
    Without it every param counts.

    ``state`` marks an alert about a long-lived condition (an orphaned task, a block nothing
    will clear). The engine then alerts on transitions only: once when the state opens,
    again at escalation thresholds measured from ``since`` (when the condition began), and
    a short "resolved" when the rule stops reporting it. Ignored on non-alert actions.

    ``once`` asks the ledger to carry the action out once per fingerprint (within the
    ledger's ``once_window``) instead of once per cooldown: for comments that must not
    pile up on a task, like a resume note.

    ``once_per_key`` makes ``key`` alone (with the rule) the fingerprint, whatever the
    task, kind or executor: for a request that may land on a different task each time
    (or as an alert when there is none) but must be made once.

    ``about_wait`` marks an alert about a task's block or wait lasting too long (not about a
    wait that is broken). While a farm-wide incident that explains the task's wait is open
    (a CI outage, :mod:`kelpie.core.incidents`), the engine folds such alerts under it.
    """

    kind: ActionKind
    rule: str
    reason: str
    task_id: str | None = None
    executor: str | None = None
    key: str | None = None
    severity: Severity = Severity.INFO
    params: dict[str, Any] = field(default_factory=dict)
    state: str | None = None
    since: datetime | None = None
    once: bool = False
    once_per_key: bool = False
    about_wait: bool = False
