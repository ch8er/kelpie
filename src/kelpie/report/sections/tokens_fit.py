"""Context and model fit in the daily report: repeated fragments, unused tools, model fit
per role and diff-only review (:mod:`kelpie.traces.fit`), priced by ``[prices]``.

Roles are the report's (the session owner's), else the task's; sessions of an unknown role
get no advice. A switch to a cheaper model is priced only for ``routine_roles`` the
operator named (``[report.sections.tokens_fit]`` once the report passes section settings
through; until then lines only). Unused tools need the platform to list the offered tools
in ``kelpie.tools.offered`` (see :mod:`kelpie.traces.fit`); without it the section is
silent about them. The estimates overlap with other sections and are not additive: a
fragment's tokens also count in context growth and cache misses (``tokens``), and model fit
prices the same calls as the cache advice."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from kelpie.core.spans import TokenUsage, counted_llm, last_at, span_key
from kelpie.report.sections import Advice, Section, SectionContext, register_section
from kelpie.traces.fit import (
    UNKNOWN_ROLE,
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
    tool_tokens,
    tool_use,
)

TITLE = "Context and model fit"
MAX_ROWS = 10
MAX_ITEMS = 5
OVERLAP_NOTE = (
    "These estimates overlap with each other and with the token optimization and huge tool "
    "output sections: they are not additive."
)


@register_section("tokens_fit")
def tokens_fit(ctx: SectionContext, settings: FitSettings | None = None) -> Section | None:
    cfg = _settings(ctx, settings)
    inputs = ctx.inputs
    if not ctx.spans:
        return None
    tasks = dict(inputs.attribution.session_tasks) if inputs.attribution else {}
    tasks.update({s.id: s.task_id for s in inputs.sessions})
    task_roles = {t.id: t.role for t in inputs.tasks if t.role}
    roles = {sid: task_roles[t] for sid, t in tasks.items() if t in task_roles}
    roles.update(ctx.roles)  # the owner's role first, else the task's
    known = [s for s in inputs.spans if last_at(s) < ctx.end]
    counted = counted_llm({span_key(s): s for s in known}, known)
    price = _Prices(ctx, session_models(known, counted))
    seen = {m for m in counted.values() if m} | set(cfg.allowed_models)

    day_reads = reads(ctx.spans, roles, tasks)
    found = fragments(day_reads, cfg)
    uses = tool_use(ctx.spans, roles)
    fit = _Fit(
        routine(role_models(ctx.spans, ctx.counted, roles), cfg, tool_tokens(day_reads)),
        model_switches(known, counted, roles, inputs.tasks, ctx.start, ctx.end, cfg),
        seen,
        cfg,
    )
    history = reads(known, roles, tasks)
    rounds = review_rounds(history, inputs.review_roles, ctx.start, ctx.end, cfg)

    parts = [
        _fragment_lines(found, price),
        _tool_lines(uses),
        _fit_lines(fit, price),
        _review_lines(rounds, price),
    ]
    if not any(parts):
        return None  # nothing found
    parts.append([OVERLAP_NOTE])
    lines = [line for part in parts if part for line in ("", *part)][1:]
    advice = [
        *_fragment_advice(found, price, cfg),
        *_fit_advice(fit, price),
        *_review_advice(rounds, price, cfg),
    ]
    return Section(TITLE, tuple(lines), tuple(advice))


def _settings(ctx: SectionContext, settings: FitSettings | None) -> FitSettings:
    """``settings``, else the defaults with ``routine_roles`` and ``allowed_models`` from
    the section's config when the report passes one (``ctx.config``)."""
    if settings is not None:
        return settings
    config = getattr(ctx, "config", None)
    if not isinstance(config, Mapping):
        return FitSettings()
    names = {}
    for key in ("routine_roles", "allowed_models"):
        value = config.get(key)
        if isinstance(value, list | tuple) and all(isinstance(v, str) for v in value):
            names[key] = tuple(value)
    return replace(FitSettings(), **names)


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


def _cell(text: str) -> str:
    """Free text (a role, an id) safe inside a Markdown table cell."""
    return " ".join(text.split()).replace("|", "\\|")


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
            f"| {_cell(f.role)} | `{_cell(f.key)}` | `{f.args_hash}` | {len(f.sessions)} | "
            f"{f.size:,} | "
            f"{f.tokens:,} | {_usd(price.cacheable(_fragment_tokens(f)))} |"
        )
    return lines


def _fragment_advice(found: Sequence[Fragment], price: _Prices, cfg: FitSettings) -> list[Advice]:
    by_role: dict[str, list[Fragment]] = defaultdict(list)
    for f in found:
        if f.role != UNKNOWN_ROLE:
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


# --- model fit ---------------------------------------------------------------------------


class _Fit:
    """Routine roles, model switches and the candidate models, with what each implies."""

    def __init__(
        self,
        candidates: Sequence[RoleModel],
        switches: Sequence[Switch],
        seen: set[str],
        cfg: FitSettings,
    ) -> None:
        self.candidates = candidates
        self.switches = switches
        self.seen = seen
        self.cfg = cfg
        self.hold = {s.role: s for s in switches if s.hold}

    def cheaper(self, r: RoleModel, price: _Prices) -> str | None:
        return cheaper_model(price.ctx.inputs.prices, r.model, self.seen)

    def priced(self, r: RoleModel) -> bool:
        return (
            r.role in self.cfg.routine_roles and r.role != UNKNOWN_ROLE and r.role not in self.hold
        )


def _saving(r: RoleModel, cheaper: str, price: _Prices) -> float:
    return max(price.usd(r.usage) - price.usd(r.on(cheaper)), 0.0)


def _fit_lines(fit: _Fit, price: _Prices) -> list[str]:
    if not fit.candidates and not fit.switches:
        return []
    tz = price.ctx.inputs.tz
    lines = [
        "**Model fit**: roles with many calls, short outputs and small tool outputs, and the "
        "cheapest model the farm already runs that could take them; quality after a switch "
        "is checked by task returns and review returns (ping-pong rounds). Short outputs "
        "alone do not make work routine: a switch is recommended only for roles named in "
        "`routine_roles`.",
        "",
    ]
    if fit.candidates:
        lines += [
            "| Role | Model | Calls | Median output | Cheaper model | USD saved |",
            "|---|---|---:|---:|---|---:|",
        ]
        for r in fit.candidates[:MAX_ROWS]:
            cheaper = fit.cheaper(r, price)
            saved = _usd(_saving(r, cheaper, price)) if cheaper else "-"
            note = ""
            if r.role in fit.hold:
                note = " (hold: " + ("worse" if fit.hold[r.role].worse else "too early") + ")"
            lines.append(
                f"| {_cell(r.role)} | `{_cell(r.model or '')}` | {r.calls} | "
                f"{r.median_output:,.0f} | "
                f"{f'`{_cell(cheaper)}`' if cheaper else 'none in the farm'}{note} | {saved} |"
            )
        if fit.switches:
            lines.append("")
    for s in fit.switches[:MAX_ROWS]:
        before, after = s.before, s.after
        verdict = (
            "worse: check the switch" if s.worse else "too early to tell" if s.early else "no worse"
        )
        when = s.at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        lines.append(
            f"- {s.role} switched `{s.old}` → `{s.new}` at {when}: returns "
            f"{before.returns} → {after.returns}, review returns {before.review_returns} → "
            f"{after.review_returns} over {before.tasks} → {after.tasks} tasks ({verdict})."
        )
    return lines


def _fit_advice(fit: _Fit, price: _Prices) -> list[Advice]:
    out = []
    for r in fit.candidates:
        cheaper = fit.cheaper(r, price)
        if cheaper is None or not fit.priced(r):
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
                    "rounds after the switch (not additive with cache advice for the role)"
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
            f"| {_cell(r.task_id)} | {r.round} | {_cell(r.session_id)} | {r.tokens:,} | "
            f"{r.reread:,} "
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
        if r.whole and r.role != UNKNOWN_ROLE:
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
