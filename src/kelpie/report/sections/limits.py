"""Provider limits in the daily report: limit hits counted from traces per provider, the
concurrency they suggest (:mod:`kelpie.core.limits`), and the spend Claude Code reported
in its own OpenTelemetry metrics (``POST /v1/metrics``, :mod:`kelpie.traces.metrics`).

A suggestion only: Kelpie never changes a farm's concurrency. No advice for the top 3:
the right concurrency also depends on what the farm runs, which traces do not tell."""

from __future__ import annotations

from kelpie.core.limits import provider_limits
from kelpie.core.spend import SpendConfig
from kelpie.report import md
from kelpie.report.sections import Section, SectionContext, register_section

TITLE = "Provider limits"


@register_section("limits")
def limits(ctx: SectionContext) -> Section | None:
    inputs = ctx.inputs
    spend = inputs.spend if inputs.spend is not None else SpendConfig()
    found = provider_limits(
        ctx.spans, ctx.start, ctx.end, spend.provider_for, spend.configured_provider
    )
    metered = inputs.metered
    if not found and not metered:
        return None
    lines: list[str] = []
    if found:
        lines += [
            "Model calls the provider refused for a limit (HTTP 429 or a rate-limit error "
            "type in traces), per provider, with the sessions running at once when it "
            "happened. A suggested concurrency is below the lowest that reached the limit; "
            "Kelpie does not change it.",
            "",
        ]
        lines += [f"- {md.text(p.line())}" for p in found]
    if metered:
        if lines:
            lines.append("")
        lines.append(
            "Claude Code's own metrics (`/v1/metrics`), for sessions with no model calls in "
            "traces (those count from traces): "
            + "; ".join(
                f"{md.text(model or 'unknown model')} {tokens:,} tokens, ${usd:,.2f} reported"
                for model, tokens, usd in metered
            )
            + ". Budgets count these tokens; the report's spend above is from traces only."
        )
    return Section(TITLE, tuple(lines))
