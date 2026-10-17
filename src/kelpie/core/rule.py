"""Rule contract: a pure function from a snapshot (and config) to a list of actions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from kelpie.core.actions import Action
from kelpie.core.model import Snapshot


@runtime_checkable
class Rule(Protocol):
    """Rules MUST be pure: no I/O, no clock reads (use ``snapshot.now``), no LLM calls.

    A rule may set ``boards = False`` when its farm-wide states do not depend on the
    platforms' boards (only on resources, providers or CI), or ``boards(state_id) -> bool``
    to say it per state: those states then resolve as usual while part of a multi-host farm
    is not observed (see ``Snapshot.unobserved``).

    A rule may also define ``resolved(state_id, snapshot) -> str | None``: what is true of
    one of its states now that it stopped reporting it ("disk h:/ is 80% full (400 of
    500 GB), under its threshold and not filling."), for the "Resolved" notice. Pure like
    ``evaluate``. Without it, or when it returns None, the notice names the task and quotes
    what is no longer reported, as the past ('no longer reported: "T-1 has no owner"')."""

    name: str

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]: ...
