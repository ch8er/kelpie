"""Context and model fit in the daily report: repeated fragments, unused tools, model fit
per role and diff-only review (:mod:`kelpie.traces.fit`), priced by ``[prices]``."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence

from kelpie.core.spans import TokenUsage, counted_llm, last_at, span_key
from kelpie.report.sections import Advice, Section, SectionContext, register_section
from kelpie.traces.fit import (
    OFFERED_ATTRIBUTE,
    FitSettings,
    Fragment,
    ReviewRound,
    RoleModel,
    Switch,
    ToolUse,
    cheaper_model,
    fragments,
    model_switches,
    reads,
    review_rounds,
    role_models,
    routine,
    session_models,
    tool_use,
)

TITLE = "Context and model fit"
MAX_ROWS = 10
MAX_ITEMS = 5


@register_section("tokens_fit")
def tokens_fit(ctx: SectionContext, settings: FitSettings | None = None) -> Section | None:
    cfg = settings or FitSettings()
    inputs = ctx.inputs
    if not ctx.spans:
        return None
    roles = dict(ctx.roles)
    tasks = dict(inputs.attribution.session_tasks) if inputs.attribution else {}
    tasks.update({s.id: s.task_id for s in inputs.sessions})
    known = [s for s in inputs.spans if last_at(s) < ctx.end]
    counted = counted_llm({span_key(s): s for s in known}, known)
    price = _Prices(ctx, session_models(known, counted))

    day_reads = reads(ctx.spans, roles, tasks)
    found = fragments(day_reads, cfg)
    uses = tool_use(ctx.spans, roles)
    candidates = routine(role_models(ctx.spans, ctx.counted, roles), cfg)
    switches = model_switches(known, counted, roles, inputs.tasks, ctx.start, ctx.end, cfg)
    history = reads(known, roles, tasks)
    rounds = review_rounds(history, inputs.review_roles, ctx.start, ctx.end, cfg)

    parts = [
        _fragment_lines(found, price),
        _tool_lines(uses),
        _fit_lines(candidates, switches, price),
        _review_lines(rounds, price),
    ]
    if not any(parts):
        return None  # nothing found: no section, not even the data gaps
    parts.append(_tool_gap(uses))
    lines = [line for part in parts if part for line in ("", *part)][1:]
    advice = [
        *_fragment_advice(found, price, cfg),
        *_fit_advice(candidates, switches, price),
        *_review_advice(rounds, price, cfg),
    ]
    return Section(TITLE, tuple(lines), tuple(advice))


class _Prices:
    def __init__(self, ctx: SectionContext, models: Mapping[str, str | None]) -> None:
        self.ctx = ctx
        self.models = models

    def usd(self, rows: Iterable[TokenUsage]) -> float:
        return self.ctx.price(rows).usd

    def input(self, tokens: Mapping[str, int], cached: bool = False) -> float:
        """Input tokens per session, priced by the session's model."""
        return self.usd(
            TokenUsage(sid, None, self.models.get(sid), 0, n, 0, n * cached)
            for sid, n in sorted(tokens.items())
        )

    def cacheable(self, tokens: Mapping[str, int]) -> float:
        """What reading these tokens from a cache instead would have saved."""
        return max(self.input(tokens) - self.input(tokens, cached=True), 0.0)


def _usd(value: float) -> str:
    return f"${value:,.2f}" if value > 0 else "-"


def _listed(items: Sequence[str]) -> str:
    more = f" (+{len(items) - MAX_ITEMS} more)" if len(items) > MAX_ITEMS else ""
    return ", ".join(items[:MAX_ITEMS]) + more


def _per_session(found: Iterable[tuple[str, int]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for sid, n in found:
        out[sid] += n
    return out


# --- repeated fragments ------------------------------------------------------------------


def _fragment_tokens(f: Fragment) -> dict[str, int]:
    return _per_session((r.session_id, r.tokens) for r in f.reads)


def _fragment_lines(found: Sequence[Fragment], price: _Prices) -> list[str]:
    if not found:
        return []
    lines = [
        "**Repeated fragments**: the same content (same tool, arguments and size) loaded into "
        "many sessions of a role; USD is what reading it from a cached prefix would save. "
        "Text repeated inside prompts is not visible: traces keep no prompt content.",
        "",
        "| Role | Tool or command | Arguments | Sessions | Tokens per read | Tokens | USD |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for f in found[:MAX_ROWS]:
        lines.append(
            f"| {f.role} | `{f.key}` | `{f.args_hash}` | {len(f.sessions)} | {f.size:,} | "
            f"{f.tokens:,} | {_usd(price.cacheable(_fragment_tokens(f)))} |"
        )
    return lines


def _fragment_advice(found: Sequence[Fragment], price: _Prices, cfg: FitSettings) -> list[Advice]:
    by_role: dict[str, list[Fragment]] = defaultdict(list)
    for f in found:
        by_role[f.role].append(f)
    out = []
    for role, fs in sorted(by_role.items()):
        tokens = sum(f.tokens for f in fs)
        if tokens < cfg.min_saving_tokens:
            continue
        keys = [f"`{f.key}`" for f in fs]
        sessions = {s for f in fs for s in f.sessions}
        per = _per_session((r.session_id, r.tokens) for f in fs for r in f.reads)
        out.append(
            Advice(
                f"Put what every {role} session loads into its cached instructions",
                (
                    f"{len(fs)} fragment(s) ({_listed(keys)}) were loaded unchanged into "
                    f"{len(sessions)} sessions, {tokens:,} tokens; a summary in the role's "
                    "instructions is read from the cache instead"
                ),
                usd=price.cacheable(per),
                tasks=tuple(sorted({t for f in fs for t in f.tasks})),
            )
        )
    return out


# --- unused tools ------------------------------------------------------------------------


def _tool_lines(uses: Sequence[ToolUse]) -> list[str]:
    known = [u for u in uses if u.offered is not None]
    if not known:
        return []
    lines = ["**Unused tools** per role (offered but never called this day):", ""]
    for u in known[:MAX_ROWS]:
        unused = [f"`{t}`" for t in u.unused]
        lines.append(
            f"- {u.role}: {len(u.offered or ())} offered, {len(u.used)} called"
            + (f"; never called: {_listed(unused)}" if unused else "; all called")
            + "."
        )
    return lines


def _tool_gap(uses: Sequence[ToolUse]) -> list[str]:
    missing = sorted(u.role for u in uses if u.offered is None)
    if not missing:
        return []
    return [
        f"Unused tools are unknown for {_listed(missing)}: traces keep the tools called, not "
        f"the ones offered. A platform or hook can list them in `{OFFERED_ATTRIBUTE}` "
        "(comma-separated) on the session's spans."
    ]


# --- model fit ---------------------------------------------------------------------------


def _saving(r: RoleModel, cheaper: str, price: _Prices) -> float:
    return max(price.usd(r.usage) - price.usd(r.on(cheaper)), 0.0)


def _fit_lines(
    candidates: Sequence[RoleModel], switches: Sequence[Switch], price: _Prices
) -> list[str]:
    if not candidates and not switches:
        return []
    prices = price.ctx.inputs.prices
    worse = {s.role for s in switches if s.worse}
    tz = price.ctx.inputs.tz
    lines = [
        "**Model fit**: roles doing routine work (many calls, short outputs) and the cheapest "
        "priced model that could do it; quality after a switch is checked by task returns "
        "and review returns (ping-pong rounds).",
        "",
    ]
    if candidates:
        lines += [
            "| Role | Model | Calls | Median output | Cheaper model | USD saved |",
            "|---|---|---:|---:|---|---:|",
        ]
        for r in candidates[:MAX_ROWS]:
            cheaper = cheaper_model(prices, r.model)
            saved = _usd(_saving(r, cheaper, price)) if cheaper else "-"
            hold = " (hold: the last switch made it worse)" if r.role in worse else ""
            lines.append(
                f"| {r.role} | `{r.model}` | {r.calls} | {r.median_output:,.0f} | "
                f"{f'`{cheaper}`' if cheaper else 'none priced'}{hold} | {saved} |"
            )
        if switches:
            lines.append("")
    for s in switches[:MAX_ROWS]:
        before, after = s.before, s.after
        verdict = "worse: check the switch" if s.worse else "no worse"
        when = s.at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        lines.append(
            f"- {s.role} switched `{s.old}` → `{s.new}` at {when}: returns "
            f"{before.returns} → {after.returns}, review returns {before.review_returns} → "
            f"{after.review_returns} over {before.tasks} → {after.tasks} tasks ({verdict})."
        )
    return lines


def _fit_advice(
    candidates: Sequence[RoleModel], switches: Sequence[Switch], price: _Prices
) -> list[Advice]:
    worse = {s.role for s in switches if s.worse}
    out = []
    for r in candidates:
        cheaper = cheaper_model(price.ctx.inputs.prices, r.model)
        if cheaper is None or r.role in worse:
            continue
        saved = _saving(r, cheaper, price)
        if saved <= 0:
            continue
        out.append(
            Advice(
                f"Try `{cheaper}` for {r.role}",
                (
                    f"routine work on `{r.model}`: {r.calls} calls with a median output of "
                    f"{r.median_output:,.0f} tokens; Kelpie checks returns and ping-pong "
                    "rounds after the switch"
                ),
                usd=saved,
                tasks=r.tasks,
            )
        )
    return out


# --- diff-only review --------------------------------------------------------------------


def _review_tokens(rounds: Iterable[ReviewRound]) -> dict[str, int]:
    return _per_session((r.session_id, r.reread) for r in rounds)


def _review_lines(rounds: Sequence[ReviewRound], price: _Prices) -> list[str]:
    if not rounds:
        return []
    lines = [
        "**Diff-only review**: review rounds 2+ and how much of what they read (files, "
        "diffs) was already read in an earlier round.",
        "",
        "| Task | Round | Session | Read tokens | Already read | Whole PR re-read | USD |",
        "|---|---:|---|---:|---:|---|---:|",
    ]
    for r in rounds[:MAX_ROWS]:
        lines.append(
            f"| {r.task_id} | {r.round} | {r.session_id} | {r.tokens:,} | {r.reread:,} "
            f"({r.share:.0%}) | {'yes' if r.whole else 'no'} | "
            f"{_usd(price.input(_review_tokens([r])))} |"
        )
    unhashed = sum(r.unhashed for r in rounds)
    if unhashed:
        lines += [
            "",
            f"{unhashed} read(s) had no arguments hash (the platform reported no arguments) and "
            "could not be compared.",
        ]
    return lines


def _review_advice(rounds: Sequence[ReviewRound], price: _Prices, cfg: FitSettings) -> list[Advice]:
    by_role: dict[str, list[ReviewRound]] = defaultdict(list)
    for r in rounds:
        if r.whole:
            by_role[r.role].append(r)
    out = []
    for role, rs in sorted(by_role.items()):
        tokens = sum(r.reread for r in rs)
        if tokens < cfg.min_saving_tokens:
            continue
        out.append(
            Advice(
                f"Review only the changes since the last round ({role})",
                (
                    f"{len(rs)} review round(s) 2+ re-read {tokens:,} tokens already read in "
                    "an earlier round; give the reviewer the diff since the last reviewed "
                    "commit and the previous round's notes"
                ),
                usd=price.input(_review_tokens(rs)),
                tasks=tuple(sorted({r.task_id for r in rs})),
            )
        )
    return out
