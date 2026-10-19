"""Farm throughput in the daily report (:mod:`kelpie.report.throughput`): tasks per hour,
time by stage, queue length, utilization, rework and cost per completed task, per role."""

from __future__ import annotations

from collections import defaultdict

from kelpie.core.spans import TokenUsage, usage
from kelpie.core.stages import STAGES, UNKNOWN_ROLE, task_roles
from kelpie.report import md
from kelpie.report.daily import wait_causes
from kelpie.report.sections import Section, SectionContext, Spend, register_section
from kelpie.report.throughput import throughput
from kelpie.report.waits import WAIT_CAUSES

TITLE = "Farm throughput"
STAGE_NAMES = {
    "working": "Working",
    "queue": "Queue",
    "review": "Review",
    "waiting": "Waiting for conditions",
    "ownerless": "Ownerless",
}


def _per_task(spend: Spend, completed: int) -> str:
    if not completed:
        return "*none done*"
    if not spend.tokens:
        return "*no spend*"
    plan = f"{spend.plan_tokens // completed:,} plan tokens" if spend.plan_tokens else ""
    if spend.plan_tokens and not spend.billed_tokens:
        return plan  # a subscription: no dollars
    if spend.unpriced_tokens == spend.billed_tokens:
        return "*unpriced*" + (f" and {plan}" if plan else "")
    out = f"${spend.usd / completed:,.2f}"
    out += f" +{spend.unpriced_tokens:,} unpriced" if spend.unpriced_tokens else ""
    return out + (f" and {plan}" if plan else "")


@register_section("throughput")
def throughput_section(ctx: SectionContext) -> Section | None:
    inputs = ctx.inputs
    roles = throughput(
        inputs.tasks,
        inputs.sessions,
        inputs.owners,
        [row.state for row in inputs.states],
        ctx.start,
        ctx.end,
        inputs.review_roles,
        ctx.roles,
        wait_causes(inputs, ctx.start, ctx.end).split,  # as the report's biggest wait
    )
    if not roles:
        return None
    of_task = task_roles(inputs.tasks, inputs.owners)
    spent: dict[str, list[TokenUsage]] = defaultdict(list)
    for row in usage(ctx.spans, ctx.counted):
        role = (of_task.get(row.task_id) if row.task_id else None) or (
            ctx.roles.get(row.session_id) if row.session_id else None
        )
        spent[role or UNKNOWN_ROLE].append(row)
    est = " (estimate)" if inputs.estimated and inputs.spend is None else ""
    if ctx.spend.unknown_usd > 0:
        est += " (estimate, mode not set)"
    lines = [
        "Per role, for the day. Utilization: agent-hours with a session running over agents "
        "x hours (the live `kelpie_role_agents_with_work` counts agents with any open task "
        "instead). Hand-offs: sessions that ended with a declared status, and every review "
        "session that ended. Mean queue: tasks of the role waiting for an agent (ready, or in "
        "progress with no session running), averaged over the day; queue at end: those still "
        "waiting at midnight (the live gauge counts ownerless tasks only). Rework: share of "
        "the tasks the role worked on that came back to it within the day, counted for the "
        "role that redid the work, not the reviewer that sent it back; - for a role that "
        "only reviews (the live returns gauge counts returns the same way; its done count "
        "goes by the task's role). "
        "Reopened: done tasks moved back out of done within "
        "the day, over the day's completions (those still done plus those reopened; "
        'rule 1\'s "done" needs evidence).',
        "",
        "| Role | Agents | Utilization | Completed | Tasks/hour | Hand-offs/hour | "
        f"Mean queue | Queue at end | Rework | Reopened | Cost per completed task{est} |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    listed = {r.role for r in roles}
    unpriced = 0
    for r in roles:
        util = "-" if r.utilization is None else f"{r.utilization:.0%}"
        rework = "-" if r.rework_rate is None else f"{r.rework_rate:.0%} ({r.returns} return(s))"
        completions = len(r.completed) + r.reopened  # a reopened task completed first
        reopened = f"{r.reopened} of {completions}" if completions else "-"
        spend = ctx.price(spent.get(r.role, ()))
        unpriced += spend.unpriced_tokens
        lines.append(
            f"| {md.text(r.role)} | {r.agents} | {util} | {len(r.completed)} | "
            f"{r.tasks_per_hour:.2f} | {r.handoffs_per_hour:.2f} | {r.mean_queue:.1f} | "
            f"{r.queue_now} | {rework} | {reopened} | {_per_task(spend, len(r.completed))} |"
        )
    lines += [
        "",
        "Cost per completed task: all the role's spend of the day (work still in progress, "
        "reviews) over the tasks it completed, so it is noisy with one or two completions. "
        "*none done*: no task completed; *no spend*: no model calls linked to the role; "
        "*unpriced*: only models without a price in [prices]; `+N unpriced`: tokens left out "
        "of the dollars; `N plan tokens`: tokens of providers on a subscription per completed "
        "task, never dollars.",
    ]
    others = sorted(r for r in spent if r not in listed)
    if others:
        extra = ctx.price(u for r in others for u in spent[r])
        unpriced += extra.unpriced_tokens
        lines.append(
            f"Spend of roles not listed ({', '.join(md.text(r) for r in others)}): "
            f"{extra.tokens:,} tokens, ${extra.usd:,.2f}{est}."
        )
    if unpriced:
        lines.append(f"{unpriced:,} tokens of models without a price are not in the dollars.")
    staged = [r for r in roles if r.stages]
    if staged:
        lines += [
            "",
            "**Task time by stage** (task-hours within the day, by the task's role):",
            "",
            "| Role | " + " | ".join(STAGE_NAMES[s] for s in STAGES) + " |",
            "|---|" + "---:|" * len(STAGES),
        ]
        for r in staged:
            cells = " | ".join(f"{r.stages.get(s, 0.0):.1f}" for s in STAGES)
            lines.append(f"| {md.text(r.role)} | {cells} |")
    waiting = [r for r in roles if r.waits]
    if waiting:
        lines += ["", "**Waiting for conditions, by cause** (task-hours, tasks):", ""]
        for r in waiting:
            causes = sorted(r.waits.items(), key=lambda x: (-x[1], x[0]))
            causes_text = "; ".join(
                f"{WAIT_CAUSES[c]} {h:.1f} ({len(r.waited.get(c, ()))})" for c, h in causes
            )
            lines.append(f"- {md.text(r.role)}: {causes_text}")
    lines += [
        "",
        "Stages are rebuilt from the board's current statuses, the sessions and their "
        "declared end statuses, and Kelpie's own alert states: the board keeps no status "
        "history, so a status change between sessions is placed at the next known change. "
        "A task counts from its first known moment of the day, so queue time before its "
        "first pickup is left out (a lower bound); the board only looks back so far "
        "(Hermes `lookback`), and a session with no end or heartbeat counts until midnight. "
        "A task's waiting time counts under what it waits (or last waited) for now: a hold "
        "label, a person (rule 13), else the kind of its condition; the part inside an open "
        "CI outage (rule 9) counts under the outage.",
    ]
    return Section(TITLE, tuple(lines))
