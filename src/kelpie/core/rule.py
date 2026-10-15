"""Rule contract: a pure function from a snapshot (and config) to a list of actions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from kelpie.core.actions import Action
from kelpie.core.model import Snapshot


@runtime_checkable
class Rule(Protocol):
    """Rules MUST be pure: no I/O, no clock reads (use ``snapshot.now``), no LLM calls."""

    name: str

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]: ...
