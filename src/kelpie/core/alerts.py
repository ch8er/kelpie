"""What an alert says, as the engine words it, and how to read it back (pure, no I/O).

The engine calls ``Alerter.send(title, body, severity, did=...)``. The title and the body
carry the transition of a long-lived state in fixed wording, defined here once so that alert
channels can show it without guessing. ``did`` is the line that says what Kelpie itself did,
rendered by the engine from the action's kind and params, the rule's mode and the actual
outcome (:func:`suggested_line`, :func:`done_line`, :func:`failed_line`): rules only describe
the situation and what they want done, never whether it was done.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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


# Wording the engine puts in front of the reason of a state alert (see Engine._state_alert),
# and in the title of an alert that is not a plain notification.
ESCALATED_PREFIX = "Still open after "
CHANGED_PREFIX = "Changed: "
RESOLVED_PREFIX = "Resolved after "
SUGGEST_TAG = "suggest"
FAILED_TAG = "failed"
DONE_TAG = "done"
NOT_EXECUTED_SUFFIX = " (not executed, needs a human)"
# An alert that reports on effects of the same tick names their keys in this param
# (a string or a list): its ``did`` line is then theirs (see Engine.execute).
ANNOUNCES = "announces"
MAX_FAILURE = 200  # characters of a failure reason shown to a human

_TITLE = re.compile(
    r"\[(?P<rule>[^\]]*)\] (?P<tag>(?:suggest|failed|done) )?(?P<what>.*)", re.DOTALL
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True, slots=True)
class AlertMessage:
    """An alert as a channel sees it. ``body`` is the reason, possibly with untrusted task
    text: channels send it as plain text or escape it."""

    rule: str
    subject: str  # e.g. "requeue task-12"
    body: str
    severity: Severity
    transition: Transition
    did: Did
    text: str | None = None  # free text (e.g. a report): sent as is, without the alert layout
    did_text: str | None = None  # what Kelpie did, as the engine rendered it (else from did)

    @property
    def fingerprint(
        self,
    ) -> tuple[str, str, str, Severity, str | None, Transition, Did, str | None]:
        """What makes two alerts the same one for de-duplication."""
        return (
            self.rule,
            self.subject,
            self.body,
            self.severity,
            self.text,
            self.transition,
            self.did,
            self.did_text,
        )


def parse_alert(title: str, body: str, severity: str, did: str | None = None) -> AlertMessage:
    """Read an engine alert back. Unknown wording degrades to a plain notification.
    ``did`` is the engine's line about what Kelpie did, kept as it is."""
    try:
        level = Severity(severity)
    except ValueError:
        level = Severity.WARNING
    did_text = did
    kind = Did.NOTIFIED
    if title.endswith(NOT_EXECUTED_SUFFIX):
        title, kind = title[: -len(NOT_EXECUTED_SUFFIX)], Did.NOT_EXECUTED
    m = _TITLE.fullmatch(title)
    rule, subject = (m["rule"], m["what"]) if m else ("", title)
    if m and m["tag"]:
        tag = m["tag"].strip()
        if tag == SUGGEST_TAG:
            kind = Did.SUGGESTED
        elif tag == FAILED_TAG:
            kind = Did.FAILED
        elif tag == DONE_TAG:
            kind = Did.DONE
    transition = Transition.NEW
    if body.startswith(RESOLVED_PREFIX):
        transition = Transition.RESOLVED
    elif body.startswith(ESCALATED_PREFIX):
        transition = Transition.ESCALATED
    elif body.startswith(CHANGED_PREFIX):
        transition = Transition.CHANGED
    return AlertMessage(rule, subject, body, level, transition, kind, did_text=did_text or None)


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
