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
- at most ``max_attempts`` (2) cards per incident within ``cooldown`` (a card whose fix was
  verified more than ``same_incident`` ago closed its incident and no longer counts), and
  none after the incident was given back to the person;
- only fresh problems (reported within 15 minutes), announced in suggest or act mode,
  opened within the history the server reads (so every earlier card of the opening is
  seen and a key never repeats), on a task not held by or assigned to a person;
- no delegation about a delegation card (a task that is one of the cards) or about the
  operator's own tasks;
- at most ``max_per_hour`` and ``max_per_day`` cards carried out; the oldest problems go
  first;
- the circuit breaker: ``breaker_failures`` failed verifications within a day stop all
  delegation.

Each card is a one-off action per problem, opening and attempt (``Action.once``, key
``delegate:<problem>:<opened epoch>:<attempt>``; attempts are numbered over the incident's
history, which holds every earlier card of the opening). The circuit breaker's alert to the
person and the verdicts come in a later part of #347.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.delegation import (
    EXECUTED,
    FAILED,
    RULE,
    VERIFIED,
    Policy,
    breaker_open,
    card_for,
    given_back,
    incident_of,
    policy_from_config,
    quote,
    why_not,
)
from kelpie.core.model import DelegationEvent, Problem, Snapshot, Status, Task
from kelpie.rules import register
from kelpie.rules._params import human

NAME = RULE
DAY = timedelta(days=1)
HOUR = timedelta(hours=1)
# Card statuses that mean the operator picked it up (or finished with it).
_PICKED = frozenset({Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED, Status.BLOCKED})


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
        carried = [e for e in events if e.kind == "card" and e.outcome == EXECUTED]
        verdicts = {(e.problem, e.attempt): e for e in events if e.kind == "verdict"}
        actions: list[Action] = list(self._verdicts(snapshot, events, carried, verdicts))
        if breaker_open(policy, events, now):
            # Every problem stays with the person (the engine holds nothing back), said once.
            failed = [e.at for e in events if e.kind == "verdict" and e.verdict in FAILED]
            actions.append(_breaker(policy, min(at for at in failed if at > now - DAY)))
            return actions
        card_tasks = {c.card_id for c in carried if c.card_id}
        left = min(
            policy.max_per_hour - sum(1 for c in carried if c.at > now - HOUR),
            policy.max_per_day - sum(1 for c in carried if c.at > now - DAY),
        )
        for problem in sorted(snapshot.problems, key=lambda p: (p.opened_at, p.id)):
            task = snapshot.task(problem.task_id) if problem.task_id else None
            if task is None or why_not(policy, problem, task, now, owners=snapshot.owners):
                continue
            if given_back(policy, incident_of(problem), events, now):
                continue  # the person has it, and heard so from the verdict
            operator = policy.operator_for(task)
            assert operator is not None  # why_not checked it
            if task.id in card_tasks:
                why = "it is about one of Kelpie's own cards"
            elif problem.opened_at < now - policy.history:
                why = "it is older than the history Kelpie reads back"
            elif (attempt := self._attempt(problem, events, carried, verdicts, now)) is None:
                if _open(problem, carried, verdicts):
                    continue  # with the operator: the verdict tells the person
                why = f"{policy.max_attempts} attempts were made within {human(policy.cooldown)}"
            elif left <= 0:
                why = "the hourly or daily limit of cards is reached"
            else:
                key = f"{problem.id}:{int(problem.opened_at.timestamp())}:{attempt}"
                card = card_for(problem, task, operator, attempt, f"kelpie-{key}")
                actions.append(_delegate(problem, task.id, operator, attempt, key, card.params()))
                left -= 1
                continue
            actions.append(_kept(problem, operator, why))
        return actions

    def _verdicts(
        self,
        snapshot: Snapshot,
        events: Sequence[DelegationEvent],
        carried: Sequence[DelegationEvent],
        verdicts: dict[tuple[str, int], DelegationEvent],
    ) -> list[Action]:
        """One verdict per card carried out that has none yet, by Kelpie's own check: the
        problem stayed clear for ``hold`` (verified), or the operator blocked the card, closed
        it while the problem stays (still broken; not fixed once the attempts are used), or
        let a deadline pass (not picked up, not finished). Otherwise it waits."""
        policy, now = self.policy, snapshot.now
        open_ids = {p.id: p for p in snapshot.problems}
        cleared: dict[str, datetime] = {}
        for r in snapshot.resolved_problems:
            cleared[r.id] = max(r.resolved_at, cleared.get(r.id, r.resolved_at))
        found: list[Action] = []
        for card in sorted(carried, key=lambda c: c.at):
            if (card.problem, card.attempt) in verdicts:
                continue
            ctask = snapshot.task(card.card_id) if card.card_id else None
            problem = open_ids.get(card.problem)
            if problem is None:
                clear = cleared.get(card.problem)
                if clear is not None and now >= max(clear, card.at) + policy.hold:
                    found.append(_verdict(card, VERIFIED, _said_verified(card, policy)))
                elif clear is None and now >= card.at + policy.finish_within:
                    # neither open nor closed in Kelpie's records: it cannot verify a fix
                    said = (
                        f"Not verified, yours now: Kelpie no longer sees {card.problem} but "
                        f"has no record of it clearing; check {card.task_id or 'the task'}."
                    )
                    found.append(_verdict(card, "not_finished", said))
                continue
            if ctask is not None and ctask.status is Status.BLOCKED:
                reason = ctask.block.reason if ctask.block is not None else ""
                why = f"{card.operator} blocked card {card.card_id}" + (
                    f":\n{quote(reason, 300)}" if reason else ""
                )
                found.append(_verdict(card, "not_fixed", _yours(card, problem, why)))
            elif ctask is not None and ctask.status.is_terminal:
                if now < ctask.status_since + policy.hold:
                    continue  # let Kelpie's own check catch up with the fix
                used = self._attempt(problem, events, carried, verdicts, now, closing=card)
                said = (
                    f"Agent says fixed, still broken: {card.operator} closed card "
                    f"{card.card_id}, but Kelpie still reports {problem.rule} on "
                    f"{problem.task_id or problem.id}."
                )
                if used is None:
                    found.append(_verdict(card, "not_fixed", _yours(card, problem, said)))
                else:
                    more = f"{said} Kelpie hands it over once more (attempt {used})."
                    found.append(_verdict(card, "still_broken", more))
            elif now >= card.at + policy.finish_within:
                why = f"card {card.card_id} was not finished within {human(policy.finish_within)}"
                found.append(_verdict(card, "not_finished", _yours(card, problem, why)))
            elif now >= card.at + policy.pickup_within and not _picked(ctask, snapshot):
                why = f"nobody picked up card {card.card_id} within {human(policy.pickup_within)}"
                found.append(_verdict(card, "not_picked_up", _yours(card, problem, why)))
        return found

    def _attempt(
        self,
        problem: Problem,
        events: Sequence[DelegationEvent],
        carried: Sequence[DelegationEvent],
        verdicts: dict[tuple[str, int], DelegationEvent],
        now: datetime,
        closing: DelegationEvent | None = None,
    ) -> int | None:
        """The attempt a new card for ``problem`` would be (counted over the incident's
        cards in the history, so it never repeats), or None when a guard holds it back: a
        card still open for its incident, the incident given back to the person, or
        ``max_attempts`` cards that count within ``cooldown`` (a card whose fix was
        verified more than ``same_incident`` ago closed its incident and no longer counts).
        ``closing``: the open card being judged now, counted as an attempt already made."""
        policy = self.policy
        incident = incident_of(problem)
        mine = [c for c in carried if c.incident == incident]
        if any((c.problem, c.attempt) not in verdicts and c is not closing for c in mine):
            return None  # one open card per incident
        if given_back(policy, incident, events, now):
            return None  # given back to the person: no new card within the cooldown

        def counts(card: DelegationEvent) -> bool:
            """Whether ``card`` is an attempt of the incident now: within the cooldown,
            unless its fix was verified and held for ``same_incident`` (no later card, and
            not this problem, came within that after the verdict)."""
            if card.at <= now - policy.cooldown:
                return False
            said = verdicts.get((card.problem, card.attempt))
            if said is None or said.verdict != VERIFIED:
                return True
            back = said.at + policy.same_incident
            later = [c.at for c in mine if c.at > card.at] + [problem.opened_at]
            held = back <= now and all(at > back for at in later if at >= said.at)
            return not held

        if sum(1 for c in mine if counts(c)) >= policy.max_attempts:
            return None
        return max((c.attempt for c in mine), default=0) + 1


def _delegate(
    problem: Problem, task_id: str, operator: str, attempt: int, key: str, card: dict[str, Any]
) -> Action:
    headline = problem.summary.strip().splitlines()[0] if problem.summary.strip() else ""
    return Action(
        ActionKind.DELEGATE,
        NAME,
        f"Handed to {operator} (attempt {attempt}): {headline}".rstrip(": "),
        task_id=task_id,
        key=f"delegate:{key}",
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


def _open(
    problem: Problem,
    carried: Sequence[DelegationEvent],
    verdicts: dict[tuple[str, int], DelegationEvent],
) -> bool:
    incident = incident_of(problem)
    return any(c.incident == incident and (c.problem, c.attempt) not in verdicts for c in carried)


def _picked(card: Task | None, snapshot: Snapshot) -> bool:
    """Whether the operator picked the card up: it moved on, someone holds it, or a session
    ran on it."""
    if card is None:
        return False
    if card.status in _PICKED or card.status.is_terminal or card.owner_id:
        return True
    return bool(snapshot.sessions_for(card.id))


def _headline(problem: Problem) -> str:
    return problem.summary.strip().splitlines()[0] if problem.summary.strip() else problem.id


def _said_verified(card: DelegationEvent, policy: Policy) -> str:
    return (
        f"Fixed, verified: the problem {card.operator} had on card {card.card_id} "
        f"({card.problem}) has stayed clear for {human(policy.hold)} by Kelpie's own check."
    )


def _yours(card: DelegationEvent, problem: Problem, why: str) -> str:
    return (
        f"Not fixed, yours now: {why}. Kelpie still reports: {_headline(problem)} "
        "No new card for this task until the cooldown ends."
    )


def _verdict(card: DelegationEvent, verdict: str, reason: str) -> Action:
    return Action(
        ActionKind.ALERT,
        NAME,
        reason,
        task_id=card.task_id,
        key=f"verdict:{card.problem}:{card.attempt}:{int(card.at.timestamp())}",
        severity=Severity.WARNING,
        once=True,
        once_per_key=True,
        params={
            "problem": card.problem,
            "incident": card.incident,
            "attempt": card.attempt,
            "operator": card.operator,
            "card_id": card.card_id,
            "verdict": verdict,
        },
    )


def _kept(problem: Problem, operator: str, why: str) -> Action:
    """The person's message about a problem the engine held back for the operator that the
    rule does not hand over: it is theirs."""
    return Action(
        ActionKind.ALERT,
        NAME,
        f"Kelpie did not hand this to {operator} ({why}); it is yours: {_headline(problem)}",
        task_id=problem.task_id,
        key=f"kept:{problem.id}:{int(problem.opened_at.timestamp())}",
        severity=Severity.WARNING,
        once=True,
        once_per_key=True,
        params={"problem": problem.id, "kept": why},
    )


def _breaker(policy: Policy, since: datetime) -> Action:
    return Action(
        ActionKind.ALERT,
        NAME,
        f"Delegation stopped: {policy.breaker_failures} or more fixes failed Kelpie's check "
        "within a day. Kelpie hands nothing to the operator for now; every problem comes to "
        "you as before.",
        key="breaker",
        severity=Severity.WARNING,
        state=f"{NAME}:breaker",
        since=since,
    )


@register(NAME)
def build(config: dict[str, Any]) -> Delegation:
    from kelpie.rules import PLANNED  # the planned rules: a class must name one of them

    policy = policy_from_config(config)
    known = {p.name for p in PLANNED}
    if unknown := sorted(k for k in policy.classes if k.split(".", 1)[0] not in known):
        raise ValueError(f"classes: unknown rules {unknown}")
    return Delegation(policy)
