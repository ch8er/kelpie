"""Unanswered asks in the daily report (#297): agent-hours that agents' questions to other
agents stayed unanswered within the day, by the agent asked.

An ask is what :func:`kelpie.rules.unanswered_asks.open_asks` finds: an agent's comment on
an open task that mentions another agent of the farm with a question mark, unanswered. That
includes asks on blocked or waiting tasks, which the rule leaves to rules 2, 4 and 13 for its
reminders: they are unanswered all the same. Only comments written before the day ended
count, so an answer given after the day leaves the ask open within it. A task finished now
counts as open until it finished (its status since), as far as the board tells: a task
finished after the day was open all of it, one finished within the day until then. An ask
answered within the day is not counted (its answer closes it), which makes the numbers a
lower bound. Each open ask counts the hours from when it was asked (or the start of the day)
to the end of the day (or its task's finish), against the agent it was put to; a recipient
waited on by several asks counts each. No advice: the rule reminds the recipient while it
is idle."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime

from kelpie.core.model import Snapshot, Status
from kelpie.report import md
from kelpie.report.sections import Section, SectionContext, register_section
from kelpie.rules.unanswered_asks import open_asks

TITLE = "Unanswered asks"
MIN_HOURS = 0.1  # an ask open less than this within the day is not listed
_SHOWN = 10  # recipients listed; the rest are counted


@register_section("asks")
def asks(ctx: SectionContext) -> Section | None:
    inputs = ctx.inputs
    until: dict[str, datetime] = {}  # a task finished within the day: open until then
    tasks = []
    for t in inputs.tasks:
        if not t.comments:
            continue
        if t.status.is_terminal:
            if t.status_since <= ctx.start:
                continue  # finished before the day
            if t.status_since < ctx.end:
                until[t.id] = t.status_since
            t = replace(t, status=Status.IN_PROGRESS)  # open within the day
        tasks.append(replace(t, comments=tuple(c for c in t.comments if c.at < ctx.end)))
    found = open_asks(Snapshot(ctx.end, tasks=tuple(tasks), owners=tuple(inputs.owners)))
    hours: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    tasks_of: dict[str, set[str]] = defaultdict(set)
    for ask in found:
        start = max(ask.comment.at, ctx.start)
        stop = until.get(ask.task.id, ctx.end)
        if start >= stop:
            continue
        h = (stop - start).total_seconds() / 3600
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
        "the day (asks unanswered at its end, or at their task's finish; one answered within "
        "the day is not counted), "
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
