"""What an alert says, as the engine words it, and how to read it back (pure, no I/O).

The engine calls ``Alerter.send(title, body, severity)``. The title and the body carry the
transition of a long-lived state and the outcome of the action in fixed wording, defined
here once so that alert channels can show both without guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from kelpie.core.actions import Severity


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


# Wording the engine puts in front of the reason of a state alert (see Engine._state_alert),
# and in the title of an alert that is not a plain notification.
ESCALATED_PREFIX = "Still open after "
CHANGED_PREFIX = "Changed: "
RESOLVED_PREFIX = "Resolved after "
SUGGEST_TAG = "suggest"
FAILED_TAG = "failed"
NOT_EXECUTED_SUFFIX = " (not executed, needs a human)"

_TITLE = re.compile(r"\[(?P<rule>[^\]]*)\] (?P<tag>(?:suggest|failed) )?(?P<what>.*)", re.DOTALL)


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

    @property
    def fingerprint(self) -> tuple[str, str, str, Severity, str | None, Transition, Did]:
        """What makes two alerts the same one for de-duplication."""
        return (
            self.rule,
            self.subject,
            self.body,
            self.severity,
            self.text,
            self.transition,
            self.did,
        )


def parse_alert(title: str, body: str, severity: str) -> AlertMessage:
    """Read an engine alert back. Unknown wording degrades to a plain notification."""
    try:
        level = Severity(severity)
    except ValueError:
        level = Severity.WARNING
    did = Did.NOTIFIED
    if title.endswith(NOT_EXECUTED_SUFFIX):
        title, did = title[: -len(NOT_EXECUTED_SUFFIX)], Did.NOT_EXECUTED
    m = _TITLE.fullmatch(title)
    rule, subject = (m["rule"], m["what"]) if m else ("", title)
    if m and m["tag"]:
        tag = m["tag"].strip()
        if tag == SUGGEST_TAG:
            did = Did.SUGGESTED
        elif tag == FAILED_TAG:
            did = Did.FAILED
    transition = Transition.NEW
    if body.startswith(RESOLVED_PREFIX):
        transition = Transition.RESOLVED
    elif body.startswith(ESCALATED_PREFIX):
        transition = Transition.ESCALATED
    elif body.startswith(CHANGED_PREFIX):
        transition = Transition.CHANGED
    return AlertMessage(rule, subject, body, level, transition, did)
