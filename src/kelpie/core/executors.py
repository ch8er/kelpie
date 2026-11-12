"""Executors carry out actions. The engine routes each action to one of them by name."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from kelpie.core.actions import Action, ActionKind, ActionResult
from kelpie.core.ledger import Outcome
from kelpie.core.model import Condition, ConditionKind
from kelpie.core.offers import Expect
from kelpie.core.ports import (
    ActionConnector,
    PlatformAdapter,
    SupportsAnswerCheck,
    SupportsClose,
    SupportsDelegate,
    SupportsLiftHold,
    SupportsReassign,
    SupportsStop,
    SupportsWait,
    UnsupportedAction,
    declared_actions,
)

log = logging.getLogger(__name__)

# The kinds an answer from the chat writes (#300), each re-checked by the adapter first.
_CHECKED = frozenset({ActionKind.COMMENT, ActionKind.UNBLOCK})


class Executor(Protocol):
    @property
    def name(self) -> str: ...

    def execute(self, action: Action) -> bool | Outcome | str:
        """Carry out ``action``. Return False if this executor cannot do it, or an
        :class:`Outcome` other than executed (``NOTHING_TO_DO``). A non-empty string means
        done, and says what happened in specifics a human reads after the engine's own
        past-tense line (e.g. when a stopped worker actually exits)."""
        ...


@dataclass(slots=True)
class PlatformExecutor:
    """Maps task actions onto a :class:`PlatformAdapter` and its optional capabilities.

    An adapter that raises :class:`UnsupportedAction` lacks the capability, as if it did not
    implement the protocol: the engine alerts a human instead (logged with the adapter's
    reason)."""

    adapter: PlatformAdapter

    @property
    def name(self) -> str:
        return self.adapter.name

    def execute(self, action: Action) -> bool | str | Outcome:
        declared = declared_actions(self.adapter)
        if declared is not None and action.kind not in declared:
            # An adapter that declares its actions carries out only those (a wake is not a
            # requeue on an adapter that leaves wake out).
            log.warning("%s does not declare %s", self.name, action.kind)
            return False
        try:
            return self._execute(action)
        except UnsupportedAction as exc:
            log.warning("%s does not support %s: %s", self.name, action.kind, exc)
            return False

    def _execute(self, action: Action) -> bool | str | Outcome:
        adapter = self.adapter
        if action.kind is ActionKind.STOP:
            # Stops target a session; heartbeat loops may have no task at all.
            session_id = action.params.get("session_id")
            if not session_id or not isinstance(adapter, SupportsStop):
                return False
            detail = adapter.stop(session_id, action.reason)
            if detail is ActionResult.NOTHING_TO_DO:
                return Outcome.NOTHING_TO_DO  # the session was over: nothing was stopped
            if detail is ActionResult.REFUSED:
                return False  # a human is told instead
            if detail is ActionResult.DONE or detail is None:  # (a StrEnum is a str too)
                return True
            return detail if isinstance(detail, str) and detail.strip() else True
        if action.kind is ActionKind.LIFT_HOLD:  # a hold, not a task: its held tasks
            return self._lift_hold(action)
        tid = action.task_id
        if tid is None:
            return False
        if "expect" in action.params:  # an answer from the chat (#300): re-checked first
            expect = Expect.from_params(action.params["expect"])
            if expect is None or action.kind not in _CHECKED:
                return False
            if not isinstance(adapter, SupportsAnswerCheck):
                log.warning("%s cannot re-check an answer; not written", self.name)
                return False
            adapter.check_answer(tid, expect)  # StaleAnswer: nothing written
        match action.kind:
            case ActionKind.REQUEUE | ActionKind.WAKE:
                # Waking a task means putting it back in the ready queue.
                adapter.requeue(tid, action.reason)
            case ActionKind.UNBLOCK:
                adapter.unblock(tid, action.reason)
            case ActionKind.COMMENT:
                adapter.comment(tid, action.reason)
            case ActionKind.WIP_COMMIT:
                made = adapter.wip_commit(
                    tid,
                    action.reason,
                    branch=action.params.get("branch"),
                    draft_pr=bool(action.params.get("draft_pr", False)),
                )
                # what was committed, for the ledger and the message
                return made if isinstance(made, str) and made.strip() else True
            case ActionKind.REASSIGN if action.params.get("owner_id") and isinstance(
                adapter, SupportsReassign
            ):
                adapter.reassign(tid, action.params["owner_id"], action.reason)
            case ActionKind.DELEGATE if isinstance(adapter, SupportsDelegate):
                card = action.params.get("card")
                operator = action.params.get("operator")
                if not isinstance(card, dict) or not isinstance(operator, str):
                    return False
                parts = [card.get(k) for k in ("title", "body", "key")]
                if not all(isinstance(p, str) and p for p in parts):
                    return False
                made = adapter.delegate(tid, operator, *parts)
                # the card's id, for the ledger: delegation reads it back (``card <id>``)
                return f"card {made} for {operator}"
            case ActionKind.CLOSE if isinstance(adapter, SupportsClose):
                adapter.close(tid, action.reason)
            case ActionKind.WAIT if isinstance(adapter, SupportsWait):
                conditions = wait_conditions(action)
                if not conditions:
                    return False
                adapter.wait(tid, conditions, action.reason)
            case _:
                return False
        return True

    def _lift_hold(self, action: Action) -> bool | str:
        adapter = self.adapter
        hold, kind = action.params.get("hold"), action.params.get("hold_kind", "label")
        held = action.params.get("held_tasks")
        if (
            not isinstance(adapter, SupportsLiftHold)
            or not isinstance(hold, str)
            or not hold
            or not isinstance(kind, str)
            or not isinstance(held, list)
        ):
            return False
        ids = [t for t in held if isinstance(t, str) and t]
        if not ids:
            return False
        done = adapter.lift_hold(hold, kind, ids, action.reason)
        return done if isinstance(done, str) and done.strip() else True


@dataclass(slots=True)
class ConnectorExecutor:
    """Runs the actions a connector declares in ``actions``."""

    connector: ActionConnector

    @property
    def name(self) -> str:
        return self.connector.name

    def execute(self, action: Action) -> bool | Outcome:
        if action.kind not in self.connector.actions:
            return False
        result = self.connector.execute(action)
        if result is False or result is ActionResult.REFUSED:
            return False
        if result is ActionResult.NOTHING_TO_DO:
            return Outcome.NOTHING_TO_DO
        return True


def wait_conditions(action: Action) -> list[Condition]:
    """The conditions a ``wait`` action carries in ``params.conditions`` (``id``, ``kind``,
    ``params``), for the task it parks. Malformed entries are left out."""
    raw = action.params.get("conditions")
    result: list[Condition] = []
    for item in raw if isinstance(raw, list) else ():
        if not isinstance(item, dict):
            continue
        cid, kind, params = item.get("id"), item.get("kind"), item.get("params", {})
        if not isinstance(cid, str) or kind not in ConditionKind.__members__.values():
            continue
        if not isinstance(params, dict):
            continue
        result.append(
            Condition(cid, ConditionKind(kind), task_id=action.task_id, params=dict(params))
        )
    return result
