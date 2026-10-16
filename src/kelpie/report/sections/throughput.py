"""Farm throughput in the daily report (:mod:`kelpie.report.throughput`): tasks per hour,
time by stage, queue length, utilization, rework and cost per completed task, per role."""

from __future__ import annotations

from collections import defaultdict

from kelpie.core.spans import TokenUsage, usage
from kelpie.report.sections import Section, SectionContext, register_section
from kelpie.report.throughput import STAGES, UNKNOWN_ROLE, task_roles, throughput

TITLE = "Farm throughput"
STAGE_NAMES = {
    "working": "Working",
    "queue": "Queue",
    "review": "Review",
    "waiting": "Waiting for conditions",
    "ownerless": "Ownerless",
}


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
    est = " (estimate)" if inputs.estimated else ""
    lines = [
        "Per role, for the day. Utilization: agent-hours with a session running over agents "
        "x hours. Hand-offs: sessions that ended with a declared status (reviews count "
        "here). Mean queue: tasks waiting for an agent, averaged over the day. Rework: share "
        "of the role's active tasks sent back within the day.",
        "",
        "| Role | Agents | Utilization | Completed | Tasks/hour | Hand-offs/hour | "
        f"Mean queue | Queue at end | Rework | Cost per completed task{est} |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in roles:
        util = "-" if r.utilization is None else f"{r.utilization:.0%}"
        rework = "-" if r.rework_rate is None else f"{r.rework_rate:.0%} ({r.returns} returns)"
        cost = ctx.price(spent.get(r.role, ())).usd
        per = f"${cost / len(r.completed):,.2f}" if r.completed and cost > 0 else "-"
        lines.append(
            f"| {r.role} | {r.agents} | {util} | {len(r.completed)} | {r.tasks_per_hour:.2f} | "
            f"{r.handoffs_per_hour:.2f} | {r.mean_queue:.1f} | {r.queue_now} | {rework} | "
            f"{per} |"
        )
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
            lines.append(f"| {r.role} | {cells} |")
    lines += [
        "",
        "Stages are rebuilt from the board's current statuses, the sessions and their "
        "declared end statuses, and Kelpie's own alert states: the board keeps no status "
        "history, so a status change between sessions is placed at the next known change.",
    ]
    return Section(TITLE, tuple(lines))
