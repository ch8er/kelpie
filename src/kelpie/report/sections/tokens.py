"""Token optimization in the daily report: prompt caching, context growth and restarts
from zero (:mod:`kelpie.traces.tokens`), with recommendations priced by ``[prices]``.

Recommendations here overlap and are not additive: a runaway session's re-read context and
its cache misses are the same tokens, and huge tool outputs (their own section) and model
fit (``tokens_fit``) price some of them again. Sessions of an unknown role get no advice,
and neither do sessions in a ``loop_breaker`` or ``no_progress`` state on the day: their
tokens are already burned in the cost of chaos, and the remedy there is to stop or slow
them, not to cache them."""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence

from kelpie.core.actions import ActionKind
from kelpie.core.ledger import Outcome
from kelpie.core.model import Snapshot, Status
from kelpie.core.spans import TokenUsage, counted_llm, last_at, span_key
from kelpie.report.daily import session_of
from kelpie.report.sections import Advice, Section, SectionContext, register_section
from kelpie.traces.tokens import (
    MISS_KINDS,
    UNKNOWN_ROLE,
    CacheGroup,
    Growth,
    MissKind,
    Restart,
    TokenSettings,
    break_hint,
    cache_groups,
    context_growth,
    excess_tokens,
    reports_cache,
    restarts,
    session_roles,
    session_steps,
)

TITLE = "Token optimization"
MAX_ROWS = 10  # rows per table
MAX_ITEMS = 5  # steps or sessions listed per line
# A session that ended with one of these said how it ended (a real end or a hand-off); any
# other end (none, in progress, ready) left its work unfinished.
DECLARED_ENDS = frozenset(
    {
        Status.DONE,
        Status.FAILED,
        Status.CANCELLED,
        Status.REVIEW,
        Status.ACCEPTED,
        Status.BLOCKED,
        Status.WAITING,
    }
)
OVERLAP_NOTE = (
    "These estimates overlap and are not additive: a runaway session's re-read context "
    "includes its cache misses, and huge tool outputs and model fit price some of the same "
    "tokens again."
)


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
    counted = counted_llm({span_key(s): s for s in known}, known)
    history = session_steps(known, counted, roles, tasks)
    price = _Prices(ctx)

    groups = cache_groups(day.values(), cfg)
    growth = context_growth(day.values(), cfg)
    restarted = restarts(history, ctx.start, ctx.end, _abnormal(ctx), cfg)
    reporting = reports_cache(known, counted)
    wasted = _wasted(ctx)
    useful = [s for s in day.values() if s.session_id not in wasted]
    useful_groups = cache_groups(useful, cfg) if len(useful) < len(day) else groups

    lines = [
        *_cache_lines(groups, price, reporting),
        "",
        *_growth_lines(growth, price),
        "",
        *_restart_lines(restarted, price),
        "",
        OVERLAP_NOTE,
    ]
    advice = [
        *_cache_advice(useful_groups, price, cfg, reporting),
        *_growth_advice([g for g in growth if g.session.session_id not in wasted], price, cfg),
        *_restart_advice([r for r in restarted if r.session.session_id not in wasted], price, cfg),
    ]
    return Section(TITLE, tuple(lines), tuple(advice))


def _wasted(ctx: SectionContext) -> set[str]:
    """Sessions in a loop or without progress during the day (``loop_breaker`` call and
    error states, ``no_progress``): the cost of chaos already counts their tokens."""
    out = set()
    for row in ctx.inputs.states:
        st = row.state
        if st.since < ctx.end and st.last_seen >= ctx.start and (sid := session_of(st)):
            out.add(sid)
    return out


def _abnormal(ctx: SectionContext) -> set[str]:
    """Sessions whose work was left unfinished: on the board without a declared end, or
    requeued by ``exit_contract``. Sessions the board no longer shows are not judged."""
    out = {
        s.id
        for s in ctx.inputs.sessions
        if s.ended_at is not None and s.final_status not in DECLARED_ENDS
    }
    for e in ctx.inputs.actions:
        if (
            e.rule == "exit_contract"
            and e.kind is ActionKind.REQUEUE
            and e.outcome not in (Outcome.SUPPRESSED, Outcome.NOTHING_TO_DO)
            and isinstance(session := e.params.get("session_id"), str)
        ):
            out.add(session)
    return out


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


def _cell(text: str) -> str:
    """Free text (a role, an id) safe inside a Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|")


def _listed(items: Sequence[str]) -> str:
    more = f" (+{len(items) - MAX_ITEMS} more)" if len(items) > MAX_ITEMS else ""
    return ", ".join(items[:MAX_ITEMS]) + more


def _model(model: str | None) -> str:
    return f"`{_cell(model)}`" if model else "unknown model"


# --- prompt cache ------------------------------------------------------------------------


def _cache_lines(groups: Sequence[CacheGroup], price: _Prices, reporting: set[str]) -> list[str]:
    lines = [
        "**Prompt cache** per role and model: hit rate is cached over input tokens; a miss is "
        "a call that read less than half of the previous prompt from the cache (break: the "
        "prefix changed; expired: a pause past the cache lifetime; partial: only a fixed "
        "prefix is cached). Missed tokens are priced as uncached minus cached input. Step "
        "numbers count a session's calls within the day.",
        "",
        "| Role | Model | Calls | Hit rate | Breaks | Expired | Partial | Missed tokens | USD |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    rows = sorted(groups, key=lambda g: (-price.missed(g), -g.missed(), g.role, g.model or ""))
    for g in rows[:MAX_ROWS]:
        lines.append(
            f"| {_cell(g.role)} | {_model(g.model)} | {g.calls} | {g.hit_rate:.0%} | "
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
    silent = sorted({g.model for g in groups if g.model and g.model not in reporting})
    if silent:
        names = ", ".join(f"`{_cell(m)}`" for m in silent)
        lines += [
            "",
            f"No call of {names} reported a cache read: the provider or its instrumentation "
            "may not report cache tokens, so no saving from caching is estimated for it.",
        ]
    return lines


def _cache_advice(
    groups: Sequence[CacheGroup], price: _Prices, cfg: TokenSettings, reporting: set[str]
) -> list[Advice]:
    out: list[Advice] = []
    ttl = round(cfg.cache_ttl.total_seconds() / 60)
    for g in groups:
        if g.role == UNKNOWN_ROLE or g.model not in reporting:
            continue  # nobody to tell, or cache tokens may simply not be reported
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
            elif g.cached_tokens:
                title = f"Cache the growing conversation for {where}"
                detail = (
                    f"hit rate {g.hit_rate:.0%}; {len(misses)} calls read only a fixed "
                    "prefix from the cache: move the cache breakpoint to the latest message"
                )
            else:
                title = f"Turn on prompt caching for {where}"
                detail = (
                    f"no cache reads in {g.calls} calls although other calls of this model "
                    f"report them; {len(misses)} of them resent the previous prompt"
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
            f"| {_cell(role)} | {len(found)} | {statistics.median(g.steps for g in found):g} | "
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
                f"{g.first:,} → {g.last:,} tokens, +{round(g.per_step):,} per step; up to "
                f"{excess_tokens(g):,} tokens re-read above the compaction point "
                f"({_usd(price.usd(g.excess))})."
            )
    return lines


def _growth_advice(growth: Sequence[Growth], price: _Prices, cfg: TokenSettings) -> list[Advice]:
    by_role: dict[str, list[Growth]] = defaultdict(list)
    for g in growth:
        if g.runaway and g.session.role != UNKNOWN_ROLE:
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
                    f"handing off at {cfg.compact_at:,} tokens would avoid re-reading up to "
                    f"{excess:,} tokens a day, less the compaction's own calls (not additive "
                    "with cache advice for the same sessions)"
                ),
                usd=price.usd(u for g in found for u in g.excess),
                tasks=tuple(sorted({g.session.task_id for g in found if g.session.task_id})),
            )
        )
    return out


# --- restarts ----------------------------------------------------------------------------


def _restart_lines(found: Sequence[Restart], price: _Prices) -> list[str]:
    if not found:
        return [
            "**Restarts from zero**: none (no task started over after a session that left it "
            "unfinished)."
        ]
    by_role: dict[str, list[Restart]] = defaultdict(list)
    for r in found:
        by_role[r.session.role].append(r)
    lines = [
        "**Restarts from zero**: a new session of a task, after one that ended without a "
        "declared end or was requeued, that rebuilt the context the earlier session already "
        "had; burned is that context, re-read once (a lower bound).",
        "",
        "| Role | Restarts | Tokens burned | USD | Tasks |",
        "|---|---:|---:|---:|---|",
    ]
    rows = sorted(by_role.items(), key=lambda kv: (-sum(r.tokens for r in kv[1]), kv[0]))
    for role, rs in rows[:MAX_ROWS]:
        tasks = sorted({_cell(r.session.task_id) for r in rs if r.session.task_id})
        lines.append(
            f"| {_cell(role)} | {len(rs)} | {sum(r.tokens for r in rs):,} | "
            f"{_usd(price.usd(u for r in rs for u in r.usage()))} | {_listed(tasks)} |"
        )
    lines.append("")
    for r in found[:MAX_ROWS]:
        lines.append(
            f"- {r.session.session_id} restarted {r.session.task_id} after "
            f"{r.previous.session_id}: started at {r.session.steps[0].input_tokens:,} tokens "
            f"where the earlier session stopped at {r.previous.steps[-1].input_tokens:,}; "
            f"{len(r.ramp)} steps to get back, {r.tokens:,} tokens of context re-acquired."
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
                    f"{len(rs)} session(s) rebuilt the context of an earlier session that left "
                    f"the task unfinished, {tokens:,} tokens; end sessions with a handoff note "
                    "(what is done, open files, next step) and start the next one from it"
                ),
                usd=price.usd(u for r in rs for u in r.usage()),
                tasks=tuple(sorted({r.session.task_id for r in rs if r.session.task_id})),
            )
        )
    return out
