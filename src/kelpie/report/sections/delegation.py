"""Delegation to an operator agent in the daily report (#347).

What the ``delegation`` rule did within the day, read back from the ledger as the rule reads
it (:func:`kelpie.core.delegation.event_of`): the cards handed to operator agents, the alerts
the engine held back for them, the verdicts by Kelpie's own check (fixed and verified, still
broken, given back to the person) and the problems kept with the person; and the operator's
tokens on Kelpie's cards: the counted model calls whose task (their own, else their
session's) is one of the cards Kelpie made within the ledger rows the report reads. Nothing
when the rule did nothing within the day. No advice: the verdicts already went to the
person."""

from __future__ import annotations

from collections import Counter

from kelpie.core.delegation import KEPT, VERIFIED, event_of
from kelpie.core.model import DelegationEvent
from kelpie.core.spans import usage
from kelpie.report import md
from kelpie.report.sections import Section, SectionContext, register_section

TITLE = "Delegation to operator agents"
# How each verdict reads in the report, in the order it is listed.
VERDICTS = (
    (VERIFIED, "fixed, verified"),
    ("still_broken", "agent says fixed, still broken"),
    ("not_fixed", "not fixed, given back"),
    ("not_picked_up", "not picked up, given back"),
    ("not_finished", "not finished, given back"),
    (KEPT, "held, then kept with the person"),
)


@register_section("delegation")
def delegation(ctx: SectionContext) -> Section | None:
    events = [e for e in map(event_of, ctx.inputs.actions) if e is not None]
    cards = {
        e.card_id for e in events if e.kind == "card" and e.outcome == "executed" and e.card_id
    }
    today = [e for e in events if ctx.start <= e.at < ctx.end]
    made = [e for e in today if e.kind == "card" and e.outcome == "executed"]
    suggested = [e for e in today if e.kind == "card" and e.outcome == "suggested"]
    held = [e for e in today if e.kind == "held"]
    said = Counter(
        e.verdict for e in today if e.kind == "verdict" and e.outcome != "failed" and e.verdict
    )
    owners = ctx.owners
    on_cards = [
        u
        for u in usage(ctx.spans, ctx.counted)
        if (owners.task_of(u.task_id, u.session_id) if owners else u.task_id) in cards
    ]
    if not (made or suggested or held or said or on_cards):
        return None
    lines = [_summary(made, suggested, held)]
    if said:
        lines += ["", "| Verdict | Cards |", "| --- | ---: |"]
        lines += [f"| {text} | {said[v]} |" for v, text in VERDICTS if said[v]]
    if on_cards:
        spend = ctx.price(on_cards)
        money = f" (${spend.usd:,.2f})" if spend.usd else ""
        lines += [
            "",
            f"Operator agents spent {spend.tokens:,} tokens{money} on Kelpie's cards "
            f"({len({u.task_id or u.session_id for u in on_cards})} card(s) or session(s)).",
        ]
    if made:
        who = Counter(e.operator or "?" for e in made)
        listed = ", ".join(f"{md.text(o)} {n}" for o, n in sorted(who.items()))
        lines += ["", f"Cards by operator: {listed}."]
    return Section(TITLE, tuple(lines))


def _summary(
    made: list[DelegationEvent], suggested: list[DelegationEvent], held: list[DelegationEvent]
) -> str:
    parts = [f"Kelpie handed {len(made)} problem(s) to operator agents as cards"]
    if held:
        parts.append(f"held back {len(held)} alert(s) for them")
    if suggested:
        parts.append(f"would have handed {len(suggested)} more (suggest mode)")
    return "; ".join(parts) + " within the day."
