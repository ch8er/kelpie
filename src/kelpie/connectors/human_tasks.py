"""Human task connector: a ``human_task`` condition holds once a human's task is done.

An agent task that needs a person (a decision, an access grant, a review outside the farm)
waits on a task owned by that person on the same board instead of polling it. The condition
is registered by the platform or its adapter, or attached from a block reason (below)::

    Condition(id=..., kind=ConditionKind.HUMAN_TASK, task_id="<waiting task>",
              params={"task": "<the human's task>"})

The connector reads no service of its own: the human's task is part of the snapshot the
platform adapter built, and a task counts as a human's when its owner has
:attr:`~kelpie.core.model.OwnerKind.HUMAN` (for Hermes: ``human_owners`` in
``[adapters.hermes]``, which is configuration, never code). Then the ``wait_condition`` rule
wakes a waiting task, and ``stale_blocks`` clears a block tied to it.

Outcomes of one check, all on :class:`~kelpie.core.model.Condition`:

==========================  ===========================================================
``satisfied=True``          the human's task is ``done``
``satisfied=False``         the task exists and is still open
``satisfied=False`` and     the task ended without being done (``failed`` or
``params["terminal"]``      ``cancelled``): the condition can never be met, rules 2 and 4
                            alert at once instead of waiting
``satisfied=None``          unknown; ``params["error"]`` is ``invalid_params`` (no
                            ``task``), ``not_found`` (the board does not show the task) or
                            ``not_human_task`` (its owner is not a human)
==========================  ===========================================================

``params["status"]`` and ``params["owner"]`` describe the task. A task seen done that then
drops off the board (adapters leave out old finished tasks) stays satisfied; one never seen
done that is not on the board is unknown, and a wait that stays unmet is for the rules'
``max_wait`` / ``max_block`` alerts.

Block reasons. Agents often block their own task with a reason that names the task they wait
on ("waiting_human: approval in T-42"). ``block_patterns`` (configuration, never code) are
case-insensitive regular expressions with a named group ``task``. During snapshot assembly a
blocked task with no condition whose reason matches gets a ``human_task`` condition, linked
through ``Block.condition_id``, but only when the task the reason names is on the board and
owned by a human; otherwise the block is left as it is. Rule 4 then treats the block as a
declared wait, like one tied to a GitHub condition.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from kelpie.core.conditions import bare_block
from kelpie.core.model import Condition, ConditionKind, OwnerKind, Snapshot, Status, Task

PARAM = "task"
MAX_REASON = 2000  # characters of a block reason the patterns look at
_KEYS = {"enabled", "block_patterns"}
# Keys this connector writes into ``Condition.params``; cleared before each check.
_RESULT_PARAMS = ("status", "owner", "error", "terminal", "task_left_board")
_TRIM = " \t.,;:!?()[]{}<>\"'`"


@dataclass(frozen=True, slots=True)
class HumanTaskSettings:
    """``[connectors.human_tasks]``: block-reason patterns are the only source to configure."""

    enabled: bool = True
    block_patterns: tuple[re.Pattern[str], ...] = ()


def load_human_tasks(raw: Any) -> HumanTaskSettings:
    if not isinstance(raw, Mapping):
        raise ValueError("[connectors.human_tasks] must be a table")
    if extra := set(raw) - _KEYS:
        raise ValueError(f"unknown [connectors.human_tasks] keys: {sorted(extra)}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("[connectors.human_tasks] enabled must be true or false")
    tables = raw.get("block_patterns", [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ValueError(
            "[connectors.human_tasks] block_patterns must be an array of tables "
            "([[connectors.human_tasks.block_patterns]])"
        )
    patterns: list[re.Pattern[str]] = []
    for i, table in enumerate(tables):
        where = f"[[connectors.human_tasks.block_patterns]] #{i + 1}"
        if extra := set(table) - {"pattern"}:
            raise ValueError(f"{where}: unknown keys {sorted(extra)}")
        source = table.get("pattern")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"{where}: pattern must be a non-empty string")
        try:
            pattern = re.compile(source, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"{where}: pattern does not compile: {exc}") from None
        if PARAM not in pattern.groupindex:
            raise ValueError(f"{where}: pattern needs a named group (?P<task>...)")
        patterns.append(pattern)
    return HumanTaskSettings(enabled, tuple(patterns))


@dataclass(slots=True)
class HumanTaskConnector:
    """Evaluates ``human_task`` conditions against the snapshot it is called with.

    Used as a snapshot step (``connector(snapshot)`` attaches block conditions, then returns
    the snapshot with every ``human_task`` condition checked) or through :meth:`check` after
    :meth:`bind`.
    """

    block_patterns: Sequence[re.Pattern[str]] = ()
    name: str = "human_tasks"
    kinds: frozenset[ConditionKind] = field(default=frozenset({ConditionKind.HUMAN_TASK}))
    _snapshot: Snapshot | None = field(default=None, init=False, repr=False)
    # condition id -> when it became satisfied, for a task that later left the board
    _satisfied: dict[str, datetime] = field(default_factory=dict, init=False, repr=False)

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        snapshot = self._attach(snapshot) if self.block_patterns else snapshot
        self.bind(snapshot)
        conditions = tuple(
            self.check(c) if c.kind in self.kinds else c for c in snapshot.conditions
        )
        live = {c.id for c in conditions}
        # While part of the farm is not observed (multi-host: a silent agent), its
        # conditions are missing, not gone: keep what was seen done.
        if not snapshot.unobserved:
            for gone in set(self._satisfied) - live:
                del self._satisfied[gone]
        return replace(snapshot, conditions=conditions)

    def bind(self, snapshot: Snapshot) -> None:
        self._snapshot = snapshot

    # --- block reasons -------------------------------------------------------------------

    def _attach(self, snapshot: Snapshot) -> Snapshot:
        tasks = list(snapshot.tasks)
        added: list[Condition] = []
        for i, task in enumerate(tasks):
            if not bare_block(task, snapshot):
                continue
            condition = self._condition_for(task, snapshot)
            if condition is None:
                continue
            block = task.block
            assert block is not None
            tasks[i] = replace(task, block=replace(block, condition_id=condition.id))
            added.append(condition)
        if not added:
            return snapshot
        return replace(snapshot, tasks=tuple(tasks), conditions=snapshot.conditions + tuple(added))

    def _condition_for(self, task: Task, snapshot: Snapshot) -> Condition | None:
        block = task.block
        assert block is not None
        reason = block.reason[:MAX_REASON]
        for pattern in self.block_patterns:
            m = pattern.search(reason)
            ref = (m["task"] or "").strip(_TRIM) if m else ""
            # Multi-host or several boards: the reason names a task of the same board, by
            # its local id, and only that board's tasks count (never a namespaced id of
            # another machine or board).
            if ref and (own := task.local_id) is not None:
                ref = f"{task.id[: len(task.id) - len(own)]}{ref}"
            if not ref or ref == task.id or not _is_human_task(snapshot, ref):
                continue
            return Condition(
                id=f"block:{task.id}:human_task:{ref}",
                kind=ConditionKind.HUMAN_TASK,
                task_id=task.id,
                params={PARAM: ref, "source": "block_reason"},
                registered_at=block.since,
            )
        return None

    # --- evaluation ----------------------------------------------------------------------

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
        if task.status is not Status.DONE:
            self._satisfied.pop(condition.id, None)
            if task.status.is_terminal:  # failed or cancelled: it will never be done
                params["terminal"] = True
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


def _is_human_task(snapshot: Snapshot, task_id: str) -> bool:
    task = snapshot.task(task_id)
    owner = snapshot.owner(task.owner_id) if task is not None and task.owner_id else None
    return owner is not None and owner.kind is OwnerKind.HUMAN
