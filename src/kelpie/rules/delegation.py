"""Delegation to an operator agent (#347): hand a problem to the farm's main agent.

For every open problem (:attr:`Snapshot.problems`: an alert state another rule keeps open)
whose class allows it (:func:`kelpie.core.delegation.why_not`: class ``agent`` in
``classes``, severity ``warning``, a ``delegate_to`` operator for the task's board, not the
operator's own task, no outage), the rule asks for one ``delegate`` action: a card on the
operator's queue (:func:`kelpie.core.delegation.card_for`), carried out by the task's board
adapter. Off by default: with no ``agent`` class or no ``delegate_to`` the rule emits
nothing. Like every rule it runs in observe, then suggest ("Suggested: hand the problem on
T-1 to ops"), then act behind docs/act-checklist.md.

Loop guards, read from what the rule did before (:attr:`Snapshot.delegations`, from the
ledger):

- one open card per incident (a task, or the problem when it has none): no new card while
  one carried out has no verdict yet;
- at most ``max_attempts`` (2) cards per incident within ``same_incident``, and none for
  ``cooldown`` after the rule gave the incident back to the person;
- no delegation about a delegation card (a task that is one of the cards) or about the
  operator's own tasks;
- at most ``max_per_hour`` and ``max_per_day`` cards carried out; the oldest problems go
  first;
- the circuit breaker: ``breaker_failures`` failed verifications within a day stop all
  delegation.

Each card is a one-off action per problem and attempt (``Action.once``, key
``delegate:<problem>:<attempt>``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.delegation import (
    RULE,
    Policy,
    card_for,
    incident_of,
    policy_from_config,
    why_not,
)
from kelpie.core.model import DelegationEvent, Problem, Snapshot
from kelpie.rules import register

NAME = RULE
EXECUTED = "executed"
# Verdicts that end an incident's delegation and give it back to the person.
GAVE_UP = frozenset({"not_fixed"})
# Verdicts that count as a failed verification (the circuit breaker).
FAILED = frozenset({"still_broken", "not_fixed", "not_picked_up", "not_finished"})
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class Delegation:
    policy: Policy = field(default_factory=Policy)
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        policy = self.policy
        if not policy.enabled:
            return ()
        now = snapshot.now
        events = [e for e in snapshot.delegations if e.at > now - policy.history]
        failures = [
            e for e in events if e.kind == "verdict" and e.verdict in FAILED and e.at > now - DAY
        ]
        if len(failures) >= policy.breaker_failures:
            return ()  # the circuit breaker: every problem stays with the person
        cards = [e for e in events if e.kind == "card"]
        carried = [c for c in cards if c.outcome == EXECUTED]
        card_tasks = {c.card_id for c in carried if c.card_id}
        judged = {(e.problem, e.attempt) for e in events if e.kind == "verdict"}
        left = min(
            policy.max_per_hour - sum(1 for c in carried if c.at > now - HOUR),
            policy.max_per_day - sum(1 for c in carried if c.at > now - DAY),
        )
        actions: list[Action] = []
        for problem in sorted(snapshot.problems, key=lambda p: (p.opened_at, p.id)):
            if left <= 0:
                break
            task = snapshot.task(problem.task_id) if problem.task_id else None
            if why_not(policy, problem, task) is not None or task is None:
                continue
            if task.id in card_tasks:
                continue  # a problem about a delegation card stays with the person
            attempt = self._attempt(problem, events, carried, judged, now)
            if attempt is None:
                continue
            operator = policy.operator(task.source)
            assert operator is not None  # why_not checked it
            actions.append(
                _delegate(
                    problem,
                    task.id,
                    operator,
                    attempt,
                    card_for(problem, task, operator, attempt).params(),
                )
            )
            left -= 1
        return actions

    def _attempt(
        self,
        problem: Problem,
        events: Sequence[DelegationEvent],
        carried: Sequence[DelegationEvent],
        judged: set[tuple[str, int]],
        now: Any,
    ) -> int | None:
        """The attempt a new card for ``problem`` would be, or None when a guard holds it
        back: a card still open for its incident, the incident given back to the person
        within ``cooldown``, or ``max_attempts`` cards within ``same_incident``."""
        policy = self.policy
        incident = incident_of(problem)
        mine = [c for c in carried if c.incident == incident]
        if any((c.problem, c.attempt) not in judged for c in mine):
            return None  # one open card per incident
        if any(
            e.kind == "verdict" and e.incident == incident and e.verdict in GAVE_UP for e in events
        ):
            return None  # given back to the person: no new card within the cooldown
        recent = [c for c in mine if c.at > now - policy.same_incident]
        if len(recent) >= policy.max_attempts:
            return None
        return len(recent) + 1


def _delegate(
    problem: Problem, task_id: str, operator: str, attempt: int, card: dict[str, Any]
) -> Action:
    headline = problem.summary.strip().splitlines()[0] if problem.summary.strip() else ""
    return Action(
        ActionKind.DELEGATE,
        NAME,
        f"Handed to {operator} (attempt {attempt}): {headline}".rstrip(": "),
        task_id=task_id,
        key=f"delegate:{problem.id}:{attempt}",
        severity=Severity.WARNING,
        once=True,
        once_per_key=True,
        params={
            "problem": problem.id,
            "incident": incident_of(problem),
            "attempt": attempt,
            "operator": operator,
            "problem_rule": problem.rule,
            "card": card,
        },
    )


@register(NAME)
def build(config: dict[str, Any]) -> Delegation:
    return Delegation(policy_from_config(config))
