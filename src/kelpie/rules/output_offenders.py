"""Repeated huge tool outputs: ask the owner of the role's instructions to add a filter.

A command (or tool) whose output floods one role's context above ``[traces.outputs]
threshold_tokens`` per call on ``repeat_days`` days is a repeated offender
(``Snapshot.output_offenders``, ranked from stored tool spans the way the daily report
ranks them, with the filter it recommends). One agent cannot fix that for the next one:
the role's instructions must say how to run the command. So this rule addresses the owner
of those instructions (``[traces.outputs] owners``, role -> owner) with a ``comment`` on
the task of the latest unfiltered call: the command, how much it floods, the filter and
the estimated saving. Without a task to comment on, it alerts instead.

Each comment is made once per role, command and filter within the ledger's
``once_window`` (7 days): a fix needs time to land, and the daily report verifies it
before/after. An offender that adopted a filter (its latest call ran through one) is no
longer an offender.

In act mode the comment goes through the task's platform adapter (Hermes cannot comment
yet: the action fails and Kelpie alerts instead); in observe and suggest mode it is an
alert. The daily report lists the same offenders under "needs a human".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import OutputOffender, Snapshot
from kelpie.rules import register

NAME = "output_offenders"


@dataclass(frozen=True, slots=True)
class OutputOffenders:
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        return [self._ask(o) for o in snapshot.output_offenders]

    def _ask(self, o: OutputOffender) -> Action:
        role = o.role or "agents"
        who = o.owner or f"Whoever owns the {role} instructions"
        reason = (
            f"{who}: `{o.command}` floods the {role} context, {o.mean_tokens:,.0f} tokens per "
            f"call on average over {o.calls} calls, above the threshold on {o.days} days. Add "
            f"to the {role} instructions: {o.how}. Estimated saving: about "
            f"{o.saved_tokens:,} tokens; the daily report checks it before/after."
        )
        return Action(
            ActionKind.COMMENT if o.latest_task else ActionKind.ALERT,
            NAME,
            reason,
            task_id=o.latest_task,
            key=f"{NAME}:{role}:{o.command}:{o.filter}",
            severity=Severity.WARNING,
            once=True,
            params={
                "owner": o.owner,
                "role": o.role,
                "command": o.command,
                "filter": o.filter,
                "days": o.days,
                "calls": o.calls,
                "mean_tokens": o.mean_tokens,
                "saved_tokens": o.saved_tokens,
                "tasks": list(o.tasks[:10]),
            },
        )


@register(NAME)
def build(config: dict[str, Any]) -> OutputOffenders:
    if config:
        raise ValueError(
            f"unknown parameters: {sorted(config)} (thresholds and owners are in [traces.outputs])"
        )
    return OutputOffenders()
