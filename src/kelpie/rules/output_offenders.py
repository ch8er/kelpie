"""Repeated huge tool outputs: ask the owner of the role's instructions to add a filter.

A command (or tool) whose output floods one role's context above ``[traces.outputs]
threshold_tokens`` per call on ``repeat_days`` days is a repeated offender
(``Snapshot.output_offenders``, ranked from tool spans with the daily report's ranking and
the filter it recommends, see :class:`kelpie.traces.store.OffenderWatch`). One agent
cannot fix that for the next one: the role's instructions must say how to run the
command. So this rule addresses the owner of those instructions (``[traces.outputs]
owners``, role -> owner) with a ``comment`` on the most recent offending task that is
still open on the board: the command, how much it floods, the filter and the estimated
saving. Without such a task it alerts instead.

The request is made once per role, command and filter within the ledger's
``once_window`` (7 days), whichever task it lands on (``once_per_key``): a fix needs time
to land, and the daily report verifies it before/after. An offender that adopted a filter
(its latest call ran through one) is no longer an offender.

Only a plain command or tool name is posted on the board (letters, digits and ``._:/+-``,
at most 64 characters); any other name, which an exporter controls, is only alerted, in a
cleaned form. In act mode the comment goes through the task's platform adapter (Hermes:
``hermes kanban comment``); in suggest mode it is an alert, in observe mode it is logged.
The daily report lists the same offenders under "needs a human".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import OutputOffender, Snapshot
from kelpie.rules import register

NAME = "output_offenders"
SAFE_NAME = re.compile(r"[\w.:/+-]{1,64}", re.ASCII)


@dataclass(frozen=True, slots=True)
class OutputOffenders:
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        open_tasks = {t.id for t in snapshot.tasks if not t.status.is_terminal}
        return [self._ask(o, open_tasks) for o in snapshot.output_offenders]

    def _ask(self, o: OutputOffender, open_tasks: set[str]) -> Action:
        role = _clean(o.role or "agents")
        safe = SAFE_NAME.fullmatch(o.command) is not None
        command = o.command if safe else _clean(o.command)
        task = next((t for t in o.recent_tasks if t in open_tasks), None) if safe else None
        who = o.owner or f"Whoever owns the {role} instructions"
        reason = (
            f"{who}: `{command}` floods the {role} context, {o.mean_tokens:,.0f} tokens per "
            f"call on average over {o.calls} calls, above the threshold on {o.days} days. Add "
            f"to the {role} instructions: {o.how}. Estimated saving: about "
            f"{o.saved_tokens:,} tokens; the daily report checks it before/after."
        )
        return Action(
            ActionKind.COMMENT if task else ActionKind.ALERT,
            NAME,
            reason,
            task_id=task,
            key=f"{NAME}:{role}:{command}:{o.filter}",
            severity=Severity.WARNING,
            once=True,
            once_per_key=True,
            params={
                "owner": o.owner,
                "role": o.role,
                "command": command,
                "filter": o.filter,
                "days": o.days,
                "calls": o.calls,
                "mean_tokens": o.mean_tokens,
                "saved_tokens": o.saved_tokens,
                "tasks": list(o.tasks[:10]),
            },
        )


def _clean(text: str) -> str:
    """A name an exporter or the board controls, reduced to safe characters."""
    kept = re.sub(r"[^\w.:/+-]", "_", text, flags=re.ASCII)[:64]
    return kept or "?"


@register(NAME)
def build(config: dict[str, Any]) -> OutputOffenders:
    if config:
        raise ValueError(
            f"unknown parameters: {sorted(config)} (thresholds and owners are in [traces.outputs])"
        )
    return OutputOffenders()
