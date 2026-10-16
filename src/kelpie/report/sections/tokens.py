"""Token optimization in the daily report: prompt caching, context growth and restarts
from zero (:mod:`kelpie.traces.tokens`), with recommendations priced by ``[prices]``."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence

from kelpie.core.model import Snapshot
from kelpie.core.spans import TokenUsage, counted_llm, last_at, span_key
from kelpie.report.sections import Advice, Section, SectionContext, register_section
from kelpie.traces.tokens import (
    MISS_KINDS,
    CacheGroup,
    Growth,
    MissKind,
    Restart,
    TokenSettings,
    break_hint,
    cache_groups,
    context_growth,
    excess_tokens,
    restarts,
    session_roles,
    session_steps,
)

TITLE = "Token optimization"
MAX_ROWS = 10  # rows per table
MAX_ITEMS = 5  # steps or sessions listed per line


@register_section("tokens")
def tokens(ctx: SectionContext, settings: TokenSettings | None = None) -> Section | None:
    cfg = settings or TokenSettings()
    inputs = ctx.inputs
    board = Snapshot(
        now=ctx.end,
        tasks=tuple(inputs.tasks),
        owners=tuple(inputs.owners),
        sessions=tuple(inputs.sessions),
    )
    fallback, tasks = session_roles(board, inputs.attribution)
    roles = {**fallback, **ctx.roles}  # the report's owner roles; else the task's role
    day = session_steps(ctx.spans, ctx.counted, roles, tasks)
    if not day:
        return None
    known = [s for s in inputs.spans if last_at(s) < ctx.end]
    index = {span_key(s): s for s in known}
    history = session_steps(known, counted_llm(index, known), roles, tasks)
    price = _Prices(ctx)

    groups = cache_groups(day.values(), cfg)
    growth = context_growth(day.values(), cfg)
    restarted = restarts(history, ctx.start, ctx.end, cfg)

    lines: list[str] = []
    advice: list[Advice] = []
    lines += _cache_lines(groups, price)
    advice += _cache_advice(groups, price, cfg)
    lines += ["", *_growth_lines(growth, price)]
    advice += _growth_advice(growth, price, cfg)
    lines += ["", *_restart_lines(restarted, price)]
    advice += _restart_advice(restarted, price, cfg)
    return Section(TITLE, tuple(lines), tuple(advice))


class _Prices:
    def __init__(self, ctx: SectionContext) -> None:
        self.ctx = ctx

    def usd(self, rows: Iterable[TokenUsage]) -> float:
        return self.ctx.price(rows).usd

    def missed(self, group: CacheGroup, kind: MissKind | None = None) -> float:
        """What reading a group's missed tokens from the cache would have saved."""
        found = [m for m in group.misses if kind is None or m.kind == kind]
        full = self.usd(m.usage(cached=False) for m in found)
        return max(full - self.usd(m.usage(cached=True) for m in found), 0.0)


def _usd(value: float) -> str:
    return f"${value:,.2f}" if value > 0 else "-"


def _listed(items: Sequence[str]) -> str:
    more = f" (+{len(items) - MAX_ITEMS} more)" if len(items) > MAX_ITEMS else ""
    return ", ".join(items[:MAX_ITEMS]) + more


def _model(model: str | None) -> str:
    return f"`{model}`" if model else "unknown model"


# --- prompt cache ------------------------------------------------------------------------


def _cache_lines(groups: Sequence[CacheGroup], price: _Prices) -> list[str]:
    lines = [
        "**Prompt cache** per role and model: hit rate is cached over input tokens; a miss is "
        "a call that read less than half of the previous prompt from the cache (break: the "
        "prefix changed; expired: a pause past the cache lifetime; partial: only a fixed "
        "prefix is cached). Missed tokens are priced as uncached minus cached input.",
        "",
        "| Role | Model | Calls | Hit rate | Breaks | Expired | Partial | Missed tokens | USD |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rows = sorted(groups, key=lambda g: (-price.missed(g), -g.missed(), g.role, g.model or ""))
    for g in rows[:MAX_ROWS]:
        lines.append(
            f"| {g.role} | {_model(g.model)} | {g.calls} | {g.hit_rate:.0%} | "
            f"{len(g.of('break'))} | {len(g.of('expired'))} | {len(g.of('partial'))} | "
            f"{g.missed():,} | {_usd(price.missed(g))} |"
        )
    broken = [g for g in rows if g.of("break")]
    if broken:
        lines += ["", "Where the cache breaks:", ""]
        for g in broken[:MAX_ROWS]:
            steps = [
                f"step {m.step.index} of {m.step.session_id}"
                + (f" (after `{m.step.after_tool}`)" if m.step.after_tool else "")
                for m in g.of("break")
            ]
            lines.append(f"- {g.role} on {_model(g.model)}: {_listed(steps)}; {break_hint(g)}.")
    return lines


def _cache_advice(groups: Sequence[CacheGroup], price: _Prices, cfg: TokenSettings) -> list[Advice]:
    out: list[Advice] = []
    ttl = round(cfg.cache_ttl.total_seconds() / 60)
    for g in groups:
        for kind in MISS_KINDS:
            misses = g.of(kind)
            missed = g.missed(kind)
            if missed < cfg.min_saving_tokens:
                continue
            tasks = tuple(sorted({m.task_id for m in misses if m.task_id}))
            where = f"{g.role} on {_model(g.model)}"
            if kind == "break":
                title = f"Fix prompt-cache breaks for {where}"
                detail = (
                    f"hit rate {g.hit_rate:.0%}; {len(misses)} of {g.checked} checked calls read "
                    f"far less from the cache than the call before; {break_hint(g)}"
                )
            elif kind == "expired":
                title = f"Keep the prompt cache warm for {where}"
                detail = (
                    f"{len(misses)} calls came more than {ttl} minutes after the previous one "
                    "and re-read the prompt uncached: shorten pauses (polling, long tool runs) "
                    "or use a longer cache lifetime where the provider offers one"
                )
            else:
                if g.cached_tokens:
                    title = f"Cache the growing conversation for {where}"
                    detail = (
                        f"hit rate {g.hit_rate:.0%}; {len(misses)} calls read only a fixed "
                        "prefix from the cache: move the cache breakpoint to the latest message"
                    )
                else:
                    title = f"Turn on prompt caching for {where}"
                    detail = (
                        f"no cache reads in {g.calls} calls, {len(misses)} of which resent the "
                        "previous prompt: turn prompt caching on (or check that the platform "
                        "reports cache reads)"
                    )
            out.append(
                Advice(
                    title,
                    f"{detail}. Up to {missed:,} tokens a day could be read from the cache "
                    "(cache-write costs not counted)",
                    usd=price.missed(g, kind),
                    tasks=tasks,
                )
            )
    return out


# --- context growth ----------------------------------------------------------------------


def _growth_lines(growth: Sequence[Growth], price: _Prices) -> list[str]:
    by_role: dict[str, list[Growth]] = defaultdict(list)
    for g in growth:
        by_role[g.session.role].append(g)
    lines = [
        "**Context growth** per step (input tokens of each model call; compactions left out):",
        "",
        "| Role | Sessions | Median steps | Median growth per step | Largest context | Runaway |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    rows = sorted(by_role.items(), key=lambda kv: (-max(g.peak for g in kv[1]), kv[0]))
    for role, found in rows[:MAX_ROWS]:
        lines.append(
            f"| {role} | {len(found)} | {statistics.median(g.steps for g in found):g} | "
            f"{round(statistics.median(g.per_step for g in found)):,} | "
            f"{max(g.peak for g in found):,} | {sum(g.runaway for g in found)} |"
        )
    runaway = [g for g in growth if g.runaway]
    if runaway:
        lines += ["", "Runaway sessions (grew without compacting):", ""]
        for g in runaway[:MAX_ROWS]:
            task = f", {g.session.task_id}" if g.session.task_id else ""
            lines.append(
                f"- {g.session.session_id} ({g.session.role}{task}): {g.steps} steps, context "
                f"{g.first:,} → {g.last:,} tokens, +{round(g.per_step):,} per step; "
                f"{excess_tokens(g):,} tokens re-read above the compaction point "
                f"({_usd(price.usd(g.excess))})."
            )
    return lines


def _growth_advice(growth: Sequence[Growth], price: _Prices, cfg: TokenSettings) -> list[Advice]:
    by_role: dict[str, list[Growth]] = defaultdict(list)
    for g in growth:
        if g.runaway:
            by_role[g.session.role].append(g)
    out = []
    for role, found in sorted(by_role.items()):
        excess = sum(excess_tokens(g) for g in found)
        if excess < cfg.min_saving_tokens:
            continue
        out.append(
            Advice(
                f"Bound the context of {role} sessions",
                (
                    f"{len(found)} session(s) grew past {cfg.runaway_tokens:,} tokens without "
                    f"compacting (largest {max(g.peak for g in found):,}); compacting or "
                    f"handing off at {cfg.compact_at:,} tokens would avoid re-reading about "
                    f"{excess:,} tokens a day"
                ),
                usd=price.usd(u for g in found for u in g.excess),
                tasks=tuple(sorted({g.session.task_id for g in found if g.session.task_id})),
            )
        )
    return out


# --- restarts ----------------------------------------------------------------------------


def _restart_lines(found: Sequence[Restart], price: _Prices) -> list[str]:
    if not found:
        return ["**Restarts from zero**: none (no task started over in a new session)."]
    by_role: dict[str, list[Restart]] = defaultdict(list)
    for r in found:
        by_role[r.session.role].append(r)
    lines = [
        "**Restarts from zero**: a new session of a task that rebuilt the context an earlier "
        "session already had; burned are its steps until it got back there.",
        "",
        "| Role | Restarts | Tokens burned | USD | Tasks |",
        "|---|---:|---:|---:|---|",
    ]
    rows = sorted(by_role.items(), key=lambda kv: (-sum(r.tokens for r in kv[1]), kv[0]))
    for role, rs in rows[:MAX_ROWS]:
        tasks = sorted({r.session.task_id for r in rs if r.session.task_id})
        lines.append(
            f"| {role} | {len(rs)} | {sum(r.tokens for r in rs):,} | "
            f"{_usd(price.usd(u for r in rs for u in r.usage()))} | {_listed(tasks)} |"
        )
    lines.append("")
    for r in found[:MAX_ROWS]:
        lines.append(
            f"- {r.session.session_id} restarted {r.session.task_id} after "
            f"{r.previous.session_id}: started at {r.session.steps[0].input_tokens:,} tokens "
            f"where the earlier session stopped at {r.previous.steps[-1].input_tokens:,}; "
            f"{len(r.ramp)} steps and {r.tokens:,} tokens to get back."
        )
    return lines


def _restart_advice(found: Sequence[Restart], price: _Prices, cfg: TokenSettings) -> list[Advice]:
    by_role: dict[str, list[Restart]] = defaultdict(list)
    for r in found:
        by_role[r.session.role].append(r)
    out = []
    for role, rs in sorted(by_role.items()):
        tokens = sum(r.tokens for r in rs)
        if tokens < cfg.min_saving_tokens:
            continue
        out.append(
            Advice(
                f"Resume {role} tasks instead of restarting from zero",
                (
                    f"{len(rs)} session(s) rebuilt the context an earlier session of the "
                    f"task had, {tokens:,} tokens; end sessions with a handoff note (what is "
                    "done, open files, next step) and start the next one from it"
                ),
                usd=price.usd(u for r in rs for u in r.usage()),
                tasks=tuple(sorted({r.session.task_id for r in rs if r.session.task_id})),
            )
        )
    return out
