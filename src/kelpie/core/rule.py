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
    platforms' boards (only on resources, providers or CI): they then resolve as usual while
    part of a multi-host farm is not observed (see ``Snapshot.unobserved``)."""

    name: str

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]: ...
