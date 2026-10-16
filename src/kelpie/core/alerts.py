"""What an alert says, as the engine hands it to an alerter (pure, no I/O).

The engine calls ``Alerter.send(event)`` with an :class:`AlertEvent`: the rule, the subject,
the rule's reason, the severity, the transition of a long-lived state, and what Kelpie
itself did, all as separate fields. A channel lays the fields out and never reads them back
out of text. ``did_text`` is the line that says what Kelpie did, rendered by the engine from
the action's kind and params, the rule's mode and the actual outcome
(:func:`suggested_line`, :func:`done_line`, :func:`failed_line`): rules only describe the
situation and what they want done, never whether it was done. The wording of a transition
("Still open after 4.0h: ") is defined here once (:func:`lead_in`), for the engine's ledger
and for every channel.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from kelpie.core.actions import Action, ActionKind, Severity


class Transition(StrEnum):
    NEW = "new"
    ESCALATED = "still open"
    CHANGED = "changed"
    RESOLVED = "resolved"


class Did(StrEnum):
    """What Kelpie itself did about the alert's action."""

    NOTIFIED = "notified"  # an alert action: telling a human is the whole action
    SUGGESTED = "suggested"  # mode suggest: nothing was changed
    FAILED = "failed"  # the action was tried and failed
    NOT_EXECUTED = "not_executed"  # no executor could carry it out
    DONE = "done"  # mode act: the action was carried out


# An alert that reports on effects of the same tick names their keys in this param
# (a string or a list): its ``did`` line is then theirs (see Engine.execute).
ANNOUNCES = "announces"
MAX_FAILURE = 200  # characters of a failure reason shown to a human

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def format_age(age: timedelta) -> str:
    """A duration as an alert shows it: ``4.0h``, or whole minutes below an hour."""
    hours = age.total_seconds() / 3600
    return f"{hours:.1f}h" if hours >= 1 else f"{max(0, int(age.total_seconds() // 60))}m"


def lead_in(transition: Transition, age: timedelta | None = None) -> str:
    """The words that put a reason in the context of its transition: ``Still open after
    4.0h: ``, ``Changed: ``, ``Resolved after 1.0h: ``; nothing for a new alert. Without an
    ``age`` the short labels ``Still open. `` and ``Resolved. `` stand in."""
    match transition:
        case Transition.ESCALATED:
            return f"Still open after {format_age(age)}: " if age is not None else "Still open. "
        case Transition.CHANGED:
            return "Changed: "
        case Transition.RESOLVED:
            return f"Resolved after {format_age(age)}: " if age is not None else "Resolved. "
        case _:
            return ""


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """An alert as the engine hands it over and as a channel sees it.

    ``reason`` is the rule's own text, as it is (no wording of a transition in front of it),
    possibly with untrusted task text: channels send it as plain text or escape it.
    ``subject`` is what the alert is about, e.g. ``requeue task-12``; with ``rule`` it
    identifies the alert for de-duplication. ``age`` is how long a lasting condition has been
    open, for the transitions ``ESCALATED`` and ``RESOLVED``.
    """

    rule: str
    subject: str
    reason: str
    severity: Severity
    transition: Transition = Transition.NEW
    did: Did = Did.NOTIFIED
    did_text: str | None = None  # what Kelpie did, as the engine rendered it (else from did)
    age: timedelta | None = None
    text: str | None = None  # free text (e.g. a report): sent as is, without the alert layout

    @property
    def fingerprint(
        self,
    ) -> tuple[str, str, str, Severity, str | None, Transition, timedelta | None, Did, str | None]:
        """What makes two alerts the same one for de-duplication."""
        return (
            self.rule,
            self.subject,
            self.reason,
            self.severity,
            self.text,
            self.transition,
            self.age,
            self.did,
            self.did_text,
        )


# --- what Kelpie did, from the action and its outcome --------------------------------------


def _target(action: Action, key: str, fallback: str) -> str:
    value = action.params.get(key)
    return str(value) if value not in (None, "") else fallback


def intent(action: Action) -> str:
    """What the action asks for, in the imperative ("clear the block on t_1")."""
    task = action.task_id or "the task"
    match action.kind:
        case ActionKind.UNBLOCK:
            return f"clear the block on {task}"
        case ActionKind.REQUEUE:
            return f"return {task} to the queue"
        case ActionKind.WAKE:
            return f"wake {task}"
        case ActionKind.WAIT:
            return f"park {task} until its conditions hold"
        case ActionKind.REASSIGN:
            return f"assign {task} to {_target(action, 'owner_id', 'another owner')}"
        case ActionKind.CLOSE:
            return f"close {task} (move it to done)"
        case ActionKind.STOP:
            return f"stop run {_target(action, 'session_id', 'of ' + task)}"
        case ActionKind.CANCEL_BUILD:
            return f"cancel build {_target(action, 'build_id', 'of ' + task)}"
        case ActionKind.COMMENT:
            return f"comment on {task}"
        case ActionKind.WIP_COMMIT:
            return f"make a WIP commit on {task}"
        case ActionKind.ALERT | ActionKind.ESCALATE:
            return "send this alert"
        case _:
            return f"{action.kind} {task}"


def _past(action: Action) -> str:
    task = action.task_id or "the task"
    match action.kind:
        case ActionKind.UNBLOCK:
            return f"Cleared the block on {task}"
        case ActionKind.REQUEUE:
            return f"Returned {task} to the queue"
        case ActionKind.WAKE:
            return f"Woke {task}"
        case ActionKind.WAIT:
            return f"Parked {task} until its conditions hold"
        case ActionKind.REASSIGN:
            return f"Assigned {task} to {_target(action, 'owner_id', 'another owner')}"
        case ActionKind.CLOSE:
            return f"Closed {task} (moved it to done)"
        case ActionKind.STOP:
            return f"Stopped run {_target(action, 'session_id', 'of ' + task)}"
        case ActionKind.CANCEL_BUILD:
            done = f"Cancelled build {_target(action, 'build_id', 'of ' + task)}"
            newer = action.params.get("superseded_by")
            return done + (f" (superseded by {newer})" if newer else "")
        case ActionKind.COMMENT:
            return f"Commented on {task}"
        case ActionKind.WIP_COMMIT:
            return f"Made a WIP commit on {task}"
        case _:
            return f"Did {action.kind} {task}"


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def suggested_line(action: Action) -> str:
    return f"Suggested: {intent(action)} (not done, rule in suggest mode)."


def done_line(action: Action, detail: str | None = None) -> str:
    """Past tense, with the executor's specifics when it gave some."""
    detail = _CONTROL.sub(" ", detail or "").strip()
    return _sentence(f"{_past(action)}; {detail}" if detail else _past(action))


def failure_reason(exc: BaseException) -> str:
    """A failure as a human may read it. Exception text can carry what a CLI or a server
    printed (tokens included), so only a reason the exception marks safe to show
    (``public_reason``) is used; anything else is named by its type, details in the log."""
    public = getattr(exc, "public_reason", None)
    if isinstance(public, str) and public.strip():
        text = _CONTROL.sub(" ", public).strip()
        return text if len(text) <= MAX_FAILURE else text[: MAX_FAILURE - 3] + "..."
    return f"{type(exc).__name__} (details in the Kelpie log)"


def failed_line(action: Action, reason: str) -> str:
    return f"Failed: could not {intent(action)}: {reason.rstrip('. ')}, needs a human."


def not_executed_line(action: Action) -> str:
    return f"Failed: nothing here can {intent(action)}, needs a human."
