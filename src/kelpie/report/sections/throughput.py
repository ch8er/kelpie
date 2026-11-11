"""Farm throughput in the daily report (:mod:`kelpie.report.throughput`): tasks per hour,
time by stage, queue length, utilization, rework and cost per completed task, per role."""

from __future__ import annotations

from collections import defaultdict

from kelpie.core.spans import TokenUsage, usage
from kelpie.core.stages import STAGES, UNKNOWN_ROLE
from kelpie.core.transitions import history
from kelpie.report import md
from kelpie.report.attribution import CallOwners, call_owners
from kelpie.report.daily import wait_causes
from kelpie.report.rework import rework_tokens
from kelpie.report.sections import Section, SectionContext, Spend, register_section
from kelpie.report.throughput import (
    Bottleneck,
    RoleThroughput,
    bottleneck,
    throughput,
    union_hours,
)
from kelpie.report.waits import WAIT_CAUSES

TITLE = "Farm throughput"
_SHOWN_REWORK = 5  # returned tasks listed under "Tokens on rework"
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


def _money(spend: Spend, est: str) -> str:
    """Spend as the section shows it: dollars, ``+N unpriced`` for tokens of models
    without a price, ``N plan tokens`` for a subscription's (never dollars)."""
    plan = f"{spend.plan_tokens:,} plan tokens" if spend.plan_tokens else ""
    if spend.plan_tokens and not spend.billed_tokens:
        return plan
    if spend.billed_tokens and spend.unpriced_tokens == spend.billed_tokens:
        return "unpriced" + (f" and {plan}" if plan else "")
    out = f"${spend.usd:,.2f}{est}"
    out += f" +{spend.unpriced_tokens:,} unpriced" if spend.unpriced_tokens else ""
    return out + (f" and {plan}" if plan else "")


def _rework_lines(ctx: SectionContext, owners: CallOwners, est: str) -> list[str]:
    """Tokens on rework (#197 item 7): per role, then per returned task, costliest first;
    nothing when no task came back within the day. Calls an outage held are the outage's
    (under Outages), not rework."""
    own = {k: m for k, m in ctx.counted.items() if k not in ctx.outage_calls}
    found = rework_tokens(ctx.inputs.tasks, ctx.spans, own, owners, ctx.start, ctx.end)
    if not found:
        return []
    priced = [(t, ctx.price(t.rows())) for t in found]
    total = ctx.price(u for t in found for u in t.rows())
    returns = sum(t.returns for t in found)
    per_role: dict[str, list[TokenUsage]] = defaultdict(list)
    for t in found:
        for role, rows in t.per_role.items():
            per_role[role].extend(rows)
    roles = "; ".join(
        f"{md.text(r)} {(sp := ctx.price(rows)).tokens:,} ({_money(sp, '')})"
        for r, rows in sorted(per_role.items())
    )
    lines = [
        "",
        f"**Tokens on rework** ({len(found)} task(s) returned, {returns} return(s)): "
        f"{total.tokens:,} tokens ({_money(total, est)}), {total.tokens // returns:,} per "
        "return"
        + (f": {roles}" if roles else "")
        + ". A task's rework is its model calls after its first return of the day, to the "
        "end of the day, by the role that made them (the reviewer's next look included; "
        "calls an outage held are under Outages); a lower bound: a return before the day, "
        "a task no longer on the board and a call that names no task are not counted.",
        "",
    ]
    ranked = sorted(priced, key=lambda x: (-x[1].usd, -x[1].tokens, x[0].task_id))
    for t, spend in ranked[:_SHOWN_REWORK]:
        who = "; ".join(
            f"{md.text(r)} {ctx.price(rows).tokens:,}" for r, rows in t.per_role.items()
        )
        lines.append(
            f"- {md.code(t.task_id)}, {t.returns} return(s): {spend.tokens:,} tokens "
            f"({_money(spend, '')}), {spend.tokens // t.returns:,} per return"
            + (f": {who}" if who else ": no model calls linked to it since")
        )
    if len(ranked) > _SHOWN_REWORK:
        lines.append(f"- +{len(ranked) - _SHOWN_REWORK} more")
    return lines


def _role(role: str) -> str:
    return "tasks of no known role" if role == UNKNOWN_ROLE else md.text(role)


def _bottleneck_lines(found: Bottleneck | None) -> list[str]:
    """The day's bottleneck, first in the section: where tasks waited longest."""
    if found is None:
        return []
    util = "" if found.utilization is None else f", its agents busy {found.utilization:.0%}"
    nxt = ""
    if found.runner_up is not None:
        role, stage, hours = found.runner_up
        nxt = f" Next: {_role(role)} in {STAGE_NAMES[stage].lower()}, {hours:.1f} task-hours."
    return [
        f"**Bottleneck of the day**: {_role(found.role)} in "
        f"{STAGE_NAMES[found.stage].lower()}, "
        f"{found.hours:.1f} task-hours waited ({found.share:.0%} of all task-hours waited "
        f"in any role and stage){util}.{nxt}",
        "",
    ]


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
        history(inputs.transitions),
        inputs.watched,
    )
    if not roles:
        return None
    owners = ctx.owners or call_owners(inputs.tasks, inputs.owners, (), ctx.roles)
    spent: dict[str, list[TokenUsage]] = defaultdict(list)
    held: dict[str, list[TokenUsage]] = defaultdict(list)  # an outage's (#197 item 6)
    own = {k: m for k, m in ctx.counted.items() if k not in ctx.outage_calls}
    for row in usage(ctx.spans, own):
        spent[owners.row_role(row)].append(row)
    if ctx.outage_calls:
        outage = {k: m for k, m in ctx.counted.items() if k in ctx.outage_calls}
        for row in usage(ctx.spans, outage):
            held[owners.row_role(row)].append(row)
    est = " (estimate)" if inputs.estimated and inputs.spend is None else ""
    if ctx.spend.unknown_usd > 0:
        est += " (estimate, mode not set)"
    lines = _bottleneck_lines(bottleneck(roles))
    lines += [
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
    if held:
        parts = []
        for r in sorted(held):
            spend = ctx.price(held[r])
            unpriced += spend.unpriced_tokens
            parts.append(f"{md.text(r)} {spend.tokens:,} tokens, ${spend.usd:,.2f}{est}")
        lines.append(
            "Spend of tasks while an outage held them (under Outages, not in the cost per "
            f"completed task): {'; '.join(parts)}."
        )
    if unpriced:
        lines.append(f"{unpriced:,} tokens of models without a price are not in the dollars.")
    lines += _rework_lines(ctx, owners, est)
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
    lines += ["", _stage_source(roles, ctx)]
    return Section(TITLE, tuple(lines))


def _stage_source(roles: list[RoleThroughput], ctx: SectionContext) -> str:
    """Where the stages came from (#157): the status changes Kelpie recorded, for how many
    of the tasks with stage time, and how long it watched within the day; inference for the
    rest."""
    active = {t for r in roles for t in r.active}
    recorded = {t for r in roles for t in r.recorded}
    watched = union_hours(
        [(max(a, ctx.start), min(b, ctx.end)) for a, b in ctx.inputs.watched if b > ctx.start]
    )
    day = (ctx.end - ctx.start).total_seconds() / 3600
    if recorded:
        source = (
            f"Status changes come from Kelpie's own record for {len(recorded)} of "
            f"{len(active)} task(s) (it watched the boards {watched:.1f} of the day's "
            f"{day:.0f} h; a change is known to within a tick, and one made while "
            "`kelpie serve` was down is placed at the board's own `status_since` when that "
            "falls inside the gap). For the other tasks, and while Kelpie was not watching, "
            "they are inferred: "
        )
    else:
        source = (
            "No status change Kelpie recorded decided a stage here (`kelpie serve` records "
            "them from its first tick on), so status changes are inferred: "
        )
    return (
        "Stages are rebuilt from the board's current statuses, the sessions and their "
        "declared end statuses, Kelpie's own alert states and its recorded status changes. "
        + source
        + "the board keeps no status history, so a status change between sessions is placed "
        "at the next known change, and a task counts from its first known moment of the day "
        "(queue time before its first pickup is left out: a lower bound). The board only "
        "looks back so far (Hermes `lookback`), and a session with no end or heartbeat counts "
        "until midnight. A task's waiting time counts under what it waits (or last waited) "
        "for now: a hold label, a person (rule 13), else the kind of its condition; the part "
        "inside an open outage counts under it (a CI outage of rule 9, a block storm of rule "
        "4, a tool failure of rule 10). Before/after verification of fixes still infers "
        "stages on both sides, so it compares like with like."
    )
