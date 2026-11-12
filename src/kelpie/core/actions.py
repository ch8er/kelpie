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
    # Open a pull request with a better CI configuration for one pipeline (rule 9), by the
    # CI connector that reported its builds.
    PROPOSE_CI_CONFIG = "propose_ci_config"
    # Lift a hold past its expiry (rule 6): ``params.hold`` names it, ``params.hold_kind``
    # says what it is (label, flag, pause) and ``params.lift_prs`` the pull requests that
    # carry a label hold. Carried out only by the executor the hold names as its owner.
    LIFT_HOLD = "lift_hold"
    # Hand a problem to the operator agent as a card on its queue (#347, ``SupportsDelegate``);
    # ``params.card`` holds the card's title, body and idempotency key.
    # Hand a problem to the operator agent as a card on its queue (#347; the adapters that
    # create cards come in a later part); ``params.card`` holds the card's title, body and
    # idempotency key.
    DELEGATE = "delegate"
    ESCALATE = "escalate"
    ALERT = "alert"
    # A person snoozed a task from a chat button (#176). Ledger only: recorded with the
    # press (who, what, until); no rule emits it and no executor carries it out.
    SNOOZE = "snooze"
    # A person answered an agent's question from a chat button (#300). Ledger only: the
    # audit line around the comment (and unblock) the answer writes; no rule emits it.
    ANSWER = "answer"


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

    ``about_wait`` marks an alert that a task's block or wait lasts too long or names no
    condition, not that it is broken (a dead end, stale evidence). While a farm-wide
    incident that explains the task's wait is open (an outage: failing CI jobs, a block
    storm, :mod:`kelpie.core.incidents`), the engine folds such alerts under it.

    ``observe_only`` makes the engine handle the action as in observe mode whatever its
    rule's mode: a new check inside a rule that already runs live stays in observe until its
    own setting switches it on.

    ``own_budget`` gives an alert that agents' text can multiply (one per agent a comment
    mentions) a rate-limit budget of its own, per rule and task, instead of the task's: it
    can never use up the budget a stop or a requeue of that task needs (#390).
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
    observe_only: bool = False
    own_budget: bool = False
