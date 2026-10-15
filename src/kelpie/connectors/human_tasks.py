"""Human task connector: a ``human_task`` condition holds once a human's task is finished.

An agent task that needs a person (a decision, an access grant, a review outside the farm)
waits on a task owned by that person on the same board instead of polling it. The condition
is registered by the platform or its adapter::

    Condition(id=..., kind=ConditionKind.HUMAN_TASK, task_id="<waiting task>",
              params={"task": "<the human's task>"})

The connector reads no service of its own: the human's task is part of the snapshot the
platform adapter built, and a task counts as a human's when its owner has
:attr:`~kelpie.core.model.OwnerKind.HUMAN` (for Hermes: ``human_owners`` in
``[adapters.hermes]``, which is configuration, never code). The condition is satisfied once
that task reaches a terminal status (done, failed or cancelled; the status is left in
``params["status"]`` so whoever wakes up can see which). Then the ``wait_condition`` rule
wakes the waiting task, and ``stale_blocks`` clears a block tied to it.

Outcomes of one check, all on :class:`~kelpie.core.model.Condition`:

========================  =============================================================
``satisfied=True``        the human's task is terminal (``status`` says how it ended)
``satisfied=False``       the human's task exists and is still open
``satisfied=None``        unknown; ``params["error"]`` is ``invalid_params`` (no ``task``),
                          ``not_found`` (the board does not show the task) or
                          ``not_human_task`` (its owner is not a human)
========================  =============================================================

A human task that was seen finished and then drops off the board (adapters leave out old
finished tasks) stays satisfied; one never seen finished that is not on the board is unknown,
and a waiting task that stays unmet is for ``wait_condition``'s ``max_wait`` alert.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from kelpie.core.model import Condition, ConditionKind, OwnerKind, Snapshot

PARAM = "task"
_KEYS = {"enabled"}
# Keys this connector writes into ``Condition.params``; cleared before each check.
_RESULT_PARAMS = ("status", "owner", "error", "task_left_board")


@dataclass(frozen=True, slots=True)
class HumanTaskSettings:
    """``[connectors.human_tasks]``: the connector has no source to configure beyond this."""

    enabled: bool = True


def load_human_tasks(raw: Any) -> HumanTaskSettings:
    if not isinstance(raw, Mapping):
        raise ValueError("[connectors.human_tasks] must be a table")
    if extra := set(raw) - _KEYS:
        raise ValueError(f"unknown [connectors.human_tasks] keys: {sorted(extra)}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("[connectors.human_tasks] enabled must be true or false")
    return HumanTaskSettings(enabled)


@dataclass(slots=True)
class HumanTaskConnector:
    """Evaluates ``human_task`` conditions against the snapshot it is called with.

    Used as a snapshot step (``connector(snapshot)`` returns the snapshot with every
    ``human_task`` condition checked) or through :meth:`check` after :meth:`bind`.
    """

    name: str = "human_tasks"
    kinds: frozenset[ConditionKind] = field(default=frozenset({ConditionKind.HUMAN_TASK}))
    _snapshot: Snapshot | None = field(default=None, init=False, repr=False)
    # condition id -> when it became satisfied, for a task that later left the board
    _satisfied: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        self.bind(snapshot)
        conditions = tuple(
            self.check(c) if c.kind in self.kinds else c for c in snapshot.conditions
        )
        live = {c.id for c in conditions}
        for gone in set(self._satisfied) - live:
            del self._satisfied[gone]
        return replace(snapshot, conditions=conditions)

    def bind(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot

    def check(self, condition: Condition) -> Condition:
        snapshot = self._snapshot
        if snapshot is None or condition.kind not in self.kinds:
            return condition
        params = {k: v for k, v in condition.params.items() if k not in _RESULT_PARAMS}
        ref = params.get(PARAM)
        if not isinstance(ref, str) or not ref.strip():
            return self._unknown(condition, params, snapshot, "invalid_params")
        task = snapshot.task(ref)
        if task is None:
            since = self._satisfied.get(condition.id)
            if since is None:
                return self._unknown(condition, params, snapshot, "not_found")
            params["task_left_board"] = True
            return replace(
                condition,
                params=params,
                satisfied=True,
                satisfied_at=since,
                checked_at=snapshot.now,
            )
        owner = snapshot.owner(task.owner_id) if task.owner_id else None
        if owner is None or owner.kind is not OwnerKind.HUMAN:
            return self._unknown(condition, params, snapshot, "not_human_task")
        params["status"] = task.status.value
        params["owner"] = owner.id
        if not task.status.is_terminal:
            self._satisfied.pop(condition.id, None)
            return replace(
                condition,
                params=params,
                satisfied=False,
                satisfied_at=None,
                checked_at=snapshot.now,
            )
        since = self._satisfied.get(condition.id)
        if since is None:
            since = condition.satisfied_at if condition.satisfied else None
        since = since or task.status_since
        self._satisfied[condition.id] = since
        return replace(
            condition, params=params, satisfied=True, satisfied_at=since, checked_at=snapshot.now
        )

    def _unknown(
        self, condition: Condition, params: dict[str, Any], snapshot: Snapshot, error: str
    ) -> Condition:
        self._satisfied.pop(condition.id, None)
        params["error"] = error
        return replace(
            condition, params=params, satisfied=None, satisfied_at=None, checked_at=snapshot.now
        )
