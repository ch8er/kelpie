"""Unanswered asks in the daily report (#297): agent-hours that agents' questions to other
agents stayed unanswered within the day, by the agent asked.

An ask is what rule ``unanswered_asks`` keeps open (:func:`kelpie.rules.unanswered_asks.
open_asks`): an agent's comment on an open task that mentions another agent of the farm with
a question mark. The board is read as it was at the end of the day: only comments written by
then count, so an answer given after the day leaves the ask open within it. An ask answered
within the day is not counted (its answer closes it), which makes the numbers a lower bound.
Each open ask counts the hours from when it was asked (or the start of the day) to the end
of the day, against the agent it was put to; a recipient waited on by several asks counts
each. No advice: the rule reminds the recipient while it is idle."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from kelpie.core.model import Snapshot
from kelpie.report import md
from kelpie.report.sections import Section, SectionContext, register_section
from kelpie.rules.unanswered_asks import open_asks

TITLE = "Unanswered asks"
MIN_HOURS = 0.1  # an ask open less than this within the day is not listed
_SHOWN = 10  # recipients listed; the rest are counted


@register_section("asks")
def asks(ctx: SectionContext) -> Section | None:
    inputs = ctx.inputs
    tasks = tuple(
        replace(t, comments=tuple(c for c in t.comments if c.at <= ctx.end))
        for t in inputs.tasks
        if t.comments
    )
    found = open_asks(Snapshot(ctx.end, tasks=tasks, owners=tuple(inputs.owners)))
    hours: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    tasks_of: dict[str, set[str]] = defaultdict(set)
    for ask in found:
        start = max(ask.comment.at, ctx.start)
        if start >= ctx.end:
            continue
        h = (ctx.end - start).total_seconds() / 3600
        if h < MIN_HOURS:
            continue
        who = ask.recipient.id
        hours[who] += h
        counts[who] += 1
        tasks_of[who].add(ask.task.id)
    if not hours:
        return None
    ranked = sorted(hours.items(), key=lambda x: (-x[1], x[0]))
    total = sum(hours.values())
    lines = [
        f"Agents' questions to other agents left {total:.1f} agent-hours unanswered within "
        "the day (asks still open at its end; one answered within the day is not counted), "
        "by the agent asked:",
        "",
        "| Asked | Open asks | Agent-hours | Tasks |",
        "| --- | ---: | ---: | --- |",
    ]
    for who, h in ranked[:_SHOWN]:
        ids = sorted(tasks_of[who])
        shown = ", ".join(md.code(t, cell=True) for t in ids[:3])
        more = f" +{len(ids) - 3}" if len(ids) > 3 else ""
        lines.append(f"| {md.text(who)} | {counts[who]} | {h:.1f} | {shown}{more} |")
    if len(ranked) > _SHOWN:
        lines += ["", f"{len(ranked) - _SHOWN} more agent(s) asked."]
    return Section(TITLE, tuple(lines))
