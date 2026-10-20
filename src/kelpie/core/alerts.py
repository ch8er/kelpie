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

An event can also carry a ``headline`` (a rule's own one-line summary) and ``items``: the
tasks it is about (:class:`AlertItem`), with their title, age, what is needed and how many
tasks wait behind each, and ``snoozed``: the tasks a person snoozed, listed in a footer
(:class:`SnoozeNote`). The engine fills them from the action and the tick's snapshot;
channels lay them out (:mod:`kelpie.alerts.render`). A report summary is an event with a
``headline`` and a :class:`Summary` (its lines and a link to the full report) instead of
tasks (:mod:`kelpie.alerts.summary`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
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


class SnoozeState(StrEnum):
    """How a person's snooze bears on an alert's task (issue #176)."""

    HELD = "snoozed"  # active: the task leaves the alert, listed in its footer
    EXPIRED = "expired"  # its date passed (within the last 7 days): the task is back
    GROWN = "grown"  # more work is blocked behind it than when it was snoozed: back


@dataclass(frozen=True, slots=True)
class SnoozeNote:
    """A snooze on an alert's task, as a channel shows it: ``until`` when it ends (or
    ended), ``reason`` and ``by`` as the person gave them (untrusted text), ``marker``
    when an ``until:`` marker in the block reason set it (always shown as "from marker"),
    ``grown`` how many more tasks are blocked behind the task than when it was snoozed."""

    state: SnoozeState
    until: datetime
    reason: str | None = None
    by: str | None = None
    marker: bool = False
    grown: int = 0


@dataclass(frozen=True, slots=True)
class AlertItem:
    """One task an alert is about. ``title`` is the board's (None when the task is not on
    the board), ``age`` how long it has been in the situation (since ``since``), ``needed``
    what a human must do (a rule's or an agent's text, untrusted), ``blocking`` how many
    tasks wait behind it (None: unknown), ``snooze`` the task's snooze when it bears on the
    alert (held, or back because it expired or the work behind it grew)."""

    task_id: str
    title: str | None = None
    age: timedelta | None = None
    needed: str | None = None
    blocking: int | None = None
    since: datetime | None = None
    snooze: SnoozeNote | None = None

    @property
    def fingerprint(self) -> tuple[object, ...]:
        """When it started, to the minute (else the age as worded), so the same item sent
        again a tick later is the same item."""
        if self.since is not None:
            start: str | None = self.since.replace(second=0, microsecond=0).isoformat()
        else:
            start = format_age(self.age) if self.age is not None else None
        return (self.task_id, self.title, start, self.needed, self.blocking, self.snooze)


@dataclass(frozen=True, slots=True)
class Summary:
    """The body of a report summary: ``lines`` under the event's ``headline``, an optional
    ``title`` line above it (e.g. which day it is about) and ``link``, the full report's URL.
    All of it is the report's own text, untrusted for a chat."""

    lines: tuple[str, ...]
    link: str | None = None
    title: str | None = None


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """An alert as the engine hands it over and as a channel sees it.

    ``reason`` is the rule's own text, as it is (no wording of a transition in front of it),
    possibly with untrusted task text: channels send it as plain text or escape it.
    ``subject`` is what the alert is about, e.g. ``requeue task-12``; with ``rule`` it
    identifies the alert for de-duplication. ``age`` is how long a lasting condition has been
    open, for the transitions ``ESCALATED`` and ``RESOLVED``. ``headline`` is the rule's own
    one-line summary, if it gave one (else a channel words one); ``items`` are the tasks the
    alert is about, in the rule's order; ``snoozed`` the tasks a person snoozed, which the
    alert leaves out and lists in a footer.
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
    headline: str | None = None
    items: tuple[AlertItem, ...] = ()
    summary: Summary | None = None  # a report summary: laid out by kelpie.alerts.summary
    # Tasks a person snoozed, left out of the alert and listed in its footer (#176), and
    # how many there are in all (None: as many as listed)
    snoozed: tuple[AlertItem, ...] = ()
    snoozed_total: int | None = None

    @property
    def fingerprint(self) -> tuple[object, ...]:
        """What makes two alerts the same one for de-duplication. Ages count as they are
        worded ("4.0h"), so an alert re-sent a minute later is still the same one."""
        return (
            self.rule,
            self.subject,
            self.reason,
            self.severity,
            self.text,
            self.transition,
            format_age(self.age) if self.age is not None else None,
            self.did,
            self.did_text,
            self.headline,
            self.summary,
            tuple(item.fingerprint for item in self.snoozed),
            self.snoozed_total,
            tuple(item.fingerprint for item in self.items),
        )


# --- what Kelpie did, from the action and its outcome --------------------------------------


def _target(action: Action, key: str, fallback: str) -> str:
    value = action.params.get(key)
    return str(value) if value not in (None, "") else fallback


def _workflow(action: Action) -> str:
    """``.github/workflows/ci.yml in owner/repo``, as far as the action names it."""
    workflow = _target(action, "workflow", "the CI config")
    repo = action.params.get("repo")
    return f"{workflow} in {repo}" if repo not in (None, "") else workflow


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
        case ActionKind.PROPOSE_CI_CONFIG:
            return f"open a pull request changing {_workflow(action)}"
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
        case ActionKind.PROPOSE_CI_CONFIG:
            return f"Opened a draft pull request changing {_workflow(action)}"
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
