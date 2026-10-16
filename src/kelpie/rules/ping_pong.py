"""Rule 5 — review ping-pong (incident 12).

A developer and a reviewer can bounce one task back and forth for hours: each fix draws a
new objection, often contradicting an earlier one, and every round costs a full session on
both sides. After ``max_review_rounds`` rounds Kelpie stops waiting for the loop to settle
and escalates the task, with the history of return reasons, to someone who can decide:
the architect role or a human (``escalate_to``).

Rounds are counted from the task itself: the larger of ``review_rounds`` (the adapter's
own counter) and the number of recorded returns whose category is in ``review_categories``.
The escalation is a lasting-state alert, so it fires once when the threshold is crossed
and again only at the engine's escalation thresholds or when the loop gets worse (every further
``max_review_rounds`` rounds), not on every tick; it resolves when
the task leaves the loop (done, failed, cancelled).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import OwnerKind, Return, Snapshot, Task
from kelpie.rules import register

NAME = "ping_pong"

_SHOWN_REASONS = 10  # the most recent return reasons quoted in the message


@dataclass(frozen=True, slots=True)
class PingPong:
    max_review_rounds: int = 3
    # Who settles the disagreement: an owner id or a role (e.g. "architect"). Empty means
    # the first human owner (by id) in the snapshot.
    escalate_to: str | None = None
    review_categories: tuple[str, ...] = ("review",)
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status.is_terminal:
                continue
            reviews = self._review_returns(task)
            rounds = max(task.review_rounds, len(reviews))
            if rounds >= self.max_review_rounds:
                actions.append(self._escalate(task, rounds, reviews, snapshot))
        return actions

    def _review_returns(self, task: Task) -> list[Return]:
        found = [r for r in task.returns if r.category in self.review_categories]
        return sorted(found, key=lambda r: r.at)

    def _escalate(
        self, task: Task, rounds: int, reviews: list[Return], snapshot: Snapshot
    ) -> Action:
        target = self._target(snapshot, task)
        history = reviews[-_SHOWN_REASONS:]
        lines = "; ".join(f"{i}. {r.reason} [{r.category}]" for i, r in enumerate(history, 1))
        earlier = len(reviews) - len(history)
        if earlier:
            lines = f"(+{earlier} earlier) {lines}"
        route = (
            f"Escalating to {target}."
            if target
            else "No escalation target configured (set escalate_to)."
        )
        reason = (
            f"Task {task.id} has been through {rounds} review rounds (limit "
            f"{self.max_review_rounds}); stop the loop and decide. {route}"
            + (f" Return reasons: {lines}." if lines else "")
        )
        # The condition began when the limit was crossed; without a history, when the
        # task last changed status.
        since = (
            reviews[self.max_review_rounds - 1].at
            if len(reviews) >= self.max_review_rounds
            else task.status_since
        )
        return Action(
            ActionKind.ESCALATE,
            NAME,
            reason,
            task_id=task.id,
            severity=Severity.WARNING,
            # The state stays one per task; the key gains a bucket each time the loop doubles
            # down another ``max_review_rounds`` rounds, so a worsening loop is announced
            # again (a "changed" transition) while an ongoing one is not.
            key=f"{NAME}:{task.id}:{rounds // self.max_review_rounds}",
            state=f"{NAME}:{task.id}",
            since=min(since, snapshot.now),
            params={
                "review_rounds": rounds,
                "max_review_rounds": self.max_review_rounds,
                "escalate_to": target,
                "returns": [
                    {"at": r.at.isoformat(), "reason": r.reason, "category": r.category}
                    for r in reviews
                ],
            },
        )

    def _target(self, snapshot: Snapshot, task: Task) -> str | None:
        if self.escalate_to is not None:
            return self.escalate_to
        humans = sorted(
            o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN and o.serves(task)
        )
        return humans[0] if humans else None


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@register(NAME)
def build(config: dict[str, Any]) -> PingPong:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "max_review_rounds" in config:
        kwargs["max_review_rounds"] = _positive_int(
            config.pop("max_review_rounds"), "max_review_rounds"
        )
    if "escalate_to" in config:
        target = config.pop("escalate_to")
        if not isinstance(target, str) or not target.strip():
            raise ValueError("escalate_to must be a non-empty owner id or role")
        kwargs["escalate_to"] = target.strip()
    if "review_categories" in config:
        cats = config.pop("review_categories")
        if (
            not isinstance(cats, list | tuple)
            or not cats
            or not all(isinstance(c, str) and c for c in cats)
        ):
            raise ValueError("review_categories must be a non-empty list of strings")
        kwargs["review_categories"] = tuple(cats)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return PingPong(**kwargs)
