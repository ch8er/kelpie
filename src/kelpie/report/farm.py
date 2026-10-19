"""What the farm already has, so the report's suggestions never advise it.

Advice that would turn something on is given only when the farm is known not to have it.
A report built without the farm's config (``None`` facts) leaves such advice out. Merge
automation is never advised: the report cannot see whether PRs merge automatically (a
live farm was told to enable auto-merge it already used), so a merge wait gets a diagnosis
instead. Facts come from config, read by ``kelpie report`` (:mod:`kelpie.report.cli`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FarmFacts:
    """None: not known."""

    approvers: bool | None = None  # rule 13 routes agents' questions to named people
    hold_expiry: bool | None = None  # holds have an owner and an expiry (rule 6)
    block_conditions: bool | None = None  # [[block_conditions]] tie block reasons to PRs
    budgets: bool | None = None  # [[budgets]] cap spend


UNKNOWN_FARM = FarmFacts()  # a report built without the farm's config


def people(facts: FarmFacts) -> str:
    out = "answer the inboxes under Needs a human first and keep human tasks off the critical path"
    if facts.approvers is False:
        out += "; name approvers for agent roles ([engine.rules.needs_you] approvers)"
    return out


def holds(facts: FarmFacts) -> str:
    if facts.hold_expiry is False:
        return (
            "give every hold an owner and an expiry ([engine.rules.bottlenecks] holds), "
            "and lift a hold when its reason is gone"
        )
    return "lift a hold when its reason is gone; its owner is alerted when it expires"


def merges() -> str:
    return (
        "find what keeps the PRs from merging: red or queued checks, missing reviews, "
        "conflicts or the merge step itself"
    )


def unregistered_waits(facts: FarmFacts) -> str:
    out = (
        "have agents register a condition when they wait (ci_status, human_task, ...), so "
        "Kelpie can tell what they wait for and wake them when it holds"
    )
    if facts.block_conditions is False:
        out += ", or match the block reasons they write with [[block_conditions]]"
    elif facts.block_conditions:
        out += ", or extend [[block_conditions]] to the block reasons it does not match yet"
    return out


def stale_blocks(facts: FarmFacts, can_act: bool) -> str:
    then = "clear blocks" if can_act else "alert"
    if facts.block_conditions is False:
        return (
            "attach conditions to block reasons ([[block_conditions]]) so Kelpie can "
            f"{then} when their cause is fixed"
        )
    if facts.block_conditions:
        return (
            "extend [[block_conditions]] to the block reasons it does not match yet, so "
            f"Kelpie can {then} when their cause is fixed"
        )
    return "check which block reasons Kelpie could not tie to a condition"


def runaway_by_hand(facts: FarmFacts) -> str:
    out = "stop runaway sessions by hand when Kelpie alerts on them"
    if facts.budgets is False:
        out += ", and cap spend with [[budgets]]"
    return out
