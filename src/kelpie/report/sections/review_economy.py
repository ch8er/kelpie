"""Review economy in the daily report (#298, :mod:`kelpie.report.review_economy`): review
rounds, tokens and findings per repository and kind of change, and a lighter review path
for the kinds where full review almost never finds anything. Kelpie only recommends."""

from __future__ import annotations

from kelpie.report import md
from kelpie.report.review_economy import (
    ReviewEconomySettings,
    Verdict,
    classes,
    judge,
    reviews,
)
from kelpie.report.sections import Advice, Section, SectionContext, register_section
from kelpie.report.sections._config import keys_of, settings_from

TITLE = "Review economy"
MAX_ROWS = 15


@register_section("review_economy", keys=keys_of(ReviewEconomySettings()))
def review_economy(
    ctx: SectionContext, settings: ReviewEconomySettings | None = None
) -> Section | None:
    cfg = settings or settings_from(ReviewEconomySettings(), ctx.config)
    inputs = ctx.inputs
    found = reviews(
        inputs.tasks,
        inputs.sessions,
        inputs.spans,
        ctx.roles,
        inputs.review_roles,
        ctx.end,
        cfg,
    )
    if not found:
        return None
    verdicts = [judge(c, ctx.end, cfg) for c in classes(found)]
    lines = [
        "Reviews the board shows, per repository and kind of change. A review found "
        "something when the task was sent back after it or another round followed. Kelpie "
        "only recommends; it never changes how tasks are reviewed.",
        "",
        "| Repository | Kind | Reviews | Rounds per task | Review tokens | With findings |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for v in verdicts[:MAX_ROWS]:
        c = v.cls
        lines.append(
            f"| {md.code(c.repo, cell=True)} | {c.kind} | {c.n} | {c.rounds:.1f} | {c.tokens:,} "
            f"| {c.k} of {c.n} ({c.share:.0%}) |"
        )
    if len(verdicts) > MAX_ROWS:
        lines.append(f"\n*…and {len(verdicts) - MAX_ROWS} more classes.*")
    advice: list[Advice] = []
    notes: list[str] = []
    for v in verdicts:
        if v.recommend:
            advice.append(_advice(ctx, v, cfg))
            notes.append(f"- **{_name(v)}**: {_detail(v, cfg)}.")
        elif v.direction:
            notes.append(
                f"- {_name(v)}: direction only, {v.cls.k} of {v.cls.n} reviews found "
                f"something; {cfg.min_sample} reviews are needed before Kelpie recommends a "
                "lighter path."
            )
    if notes:
        lines += ["", *notes]
    return Section(TITLE, tuple(lines), tuple(advice))


def _name(v: Verdict) -> str:
    return f"{v.cls.kind} in {md.short_code(v.cls.repo)}"


def _detail(v: Verdict, cfg: ReviewEconomySettings) -> str:
    c = v.cls
    return (
        f"{c.k} of {c.n} full reviews found something ({c.share:.0%}, threshold "
        f"{cfg.max_findings_share:.0%}); a lighter path ({v.path}) would save about "
        f"{v.saved_tokens_per_day:,.0f} tokens a day, at {cfg.light_share:.0%} of a full "
        f"review's cost"
    )


def _advice(ctx: SectionContext, v: Verdict, cfg: ReviewEconomySettings) -> Advice:
    spend = ctx.price(row for r in v.cls.reviews for row in r.usage)
    usd = 0.0 if spend.plan_tokens else spend.usd * (1 - cfg.light_share) / v.days
    return Advice(
        title=f"Lighter review for {_name(v)}",
        detail=_detail(v, cfg),
        usd=usd,
        tasks=v.cls.tasks[:10],
    )
