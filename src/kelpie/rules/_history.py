"""Readings of a task's observed status history (``Snapshot.status_history``, #157) for
rules: how long it waited to be picked up, and how often it bounced back.

The history starts when Kelpie started watching and covers a window
(:data:`kelpie.core.transitions.HISTORY_WINDOW`): a reading over it is a lower bound, and
None means "not seen", never zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta

from kelpie.core.model import Status, StatusStep

# Statuses a task reaches when work on it is handed on, and those it is sent back to.
HANDED_ON = frozenset({Status.REVIEW, Status.ACCEPTED, Status.DONE})
SENT_BACK = frozenset({Status.BACKLOG, Status.READY, Status.IN_PROGRESS})


def bounces(steps: Sequence[StatusStep]) -> int:
    """How often the task went back from review, accepted or done to backlog, ready or in
    progress, within the history. Its first step may be the status the window began in,
    so one bounce may be older than the window."""
    return sum(1 for s in steps if s.previous in HANDED_ON and s.status in SENT_BACK)


def time_to_accept(steps: Sequence[StatusStep]) -> timedelta | None:
    """From the task's first observed move into ``ready`` to the first move into
    ``in_progress`` after it: how long ready work waited for an agent. None when the
    history shows no such pair (not ready yet, still waiting, or picked up before Kelpie
    watched)."""
    ready = next((s for s in steps if s.status is Status.READY), None)
    if ready is None:
        return None
    started = next((s for s in steps if s.status is Status.IN_PROGRESS and s.at >= ready.at), None)
    return None if started is None else started.at - ready.at
