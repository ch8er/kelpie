"""Action ledger: what Kelpie decided and did, with de-duplication and rate limits.

Every action has a fingerprint: rule, kind, task, executor and either ``Action.key``
(when the rule sets one) or all params. Put volatile measurements in params *and* set
``key`` so that the same alert with a new reading is still recognised as a repeat.

Before an action is handled the ledger may suppress it:

- **cooldown**: the same fingerprint was handled in the same effective mode within
  ``cooldown`` (``retry_after`` if that attempt failed). Matching on mode means that
  switching a rule from observe to act, or turning dry-run off, takes effect at once.
- **rate limit**: its subject (task, else session, else executor and rule) already had
  ``max_per_subject_per_hour`` actions with an effect in the last hour. The first such
  suppression per subject and hour is flagged so the engine can tell a human once.

Suppressed actions are counted on the latest entry with the same fingerprint instead of
being stored one by one. If the store fails, entries are kept in memory and retried, and
``check`` keeps seeing them, so a write failure cannot make Kelpie repeat itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import Mode


class Outcome(StrEnum):
    DRY_RUN = "dry_run"  # global override: logged only
    OBSERVED = "observed"  # rule in observe mode: logged only
    SUGGESTED = "suggested"  # sent to a human as a suggestion
    ALERTED = "alerted"  # alert/escalation delivered
    EXECUTED = "executed"  # carried out by an executor
    NOT_EXECUTED = "not_executed"  # no executor could do it; a human was alerted
    FAILED = "failed"  # the executor raised
    DROPPED = "dropped"  # should have reached a human, but no alerter is configured
    SUPPRESSED = "suppressed"  # held back by the ledger


# Outcomes that touched the outside world or a human; they count against the rate limit.
EFFECTS = frozenset(
    {
        Outcome.SUGGESTED,
        Outcome.ALERTED,
        Outcome.EXECUTED,
        Outcome.NOT_EXECUTED,
        Outcome.FAILED,
    }
)


class Suppression(StrEnum):
    COOLDOWN = "cooldown"
    RATE_LIMIT = "rate_limit"
    LEDGER_DOWN = "ledger_down"


@dataclass(frozen=True, slots=True)
class Dispatched:
    """What happened to an action. ``mode`` is the effective mode (observe under dry-run)."""

    action: Action
    mode: Mode
    outcome: Outcome
    suppression: Suppression | None = None
    first_suppression: bool = False  # first rate-limit hit for this subject this hour


def fingerprint(action: Action) -> str:
    detail: Any = action.key if action.key is not None else action.params
    key = [action.rule, action.kind, action.task_id, action.executor, detail]
    raw = json.dumps(key, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def subject(action: Action) -> str:
    """What the rate limit counts against."""
    if action.task_id:
        return f"task:{action.task_id}"
    if session_id := action.params.get("session_id"):
        return f"session:{session_id}"
    return f"rule:{action.executor or ''}:{action.rule}"


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    at: datetime
    fingerprint: str
    subject: str
    rule: str
    kind: ActionKind
    mode: Mode
    outcome: Outcome
    reason: str
    task_id: str | None = None
    executor: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    suppressed: int = 0  # later repeats held back by the ledger

    @classmethod
    def of(cls, dispatched: Dispatched, at: datetime) -> LedgerEntry:
        a = dispatched.action
        return cls(
            at=at,
            fingerprint=fingerprint(a),
            subject=subject(a),
            rule=a.rule,
            kind=a.kind,
            mode=dispatched.mode,
            outcome=dispatched.outcome,
            reason=a.reason,
            task_id=a.task_id,
            executor=a.executor,
            params=dict(a.params),
        )


class LedgerStore(Protocol):
    """Persistence for ledger entries. Times are compared as instants (``>``)."""

    def append(self, entry: LedgerEntry) -> None: ...

    def by_fingerprint(self, fingerprint: str, after: datetime) -> Sequence[LedgerEntry]: ...

    def by_subject(self, subject: str, after: datetime) -> Sequence[LedgerEntry]: ...

    def bump_suppressed(self, fingerprint: str) -> bool:
        """Count one more suppression on the latest entry; False if there is none."""
        ...

    def since(self, after: datetime) -> Sequence[LedgerEntry]: ...


@dataclass(slots=True)
class MemoryLedgerStore:
    entries: list[LedgerEntry] = field(default_factory=list)

    def append(self, entry: LedgerEntry) -> None:
        self.entries.append(entry)

    def by_fingerprint(self, fingerprint: str, after: datetime) -> Sequence[LedgerEntry]:
        return [e for e in self.entries if e.fingerprint == fingerprint and e.at > after]

    def by_subject(self, subject: str, after: datetime) -> Sequence[LedgerEntry]:
        return [e for e in self.entries if e.subject == subject and e.at > after]

    def bump_suppressed(self, fingerprint: str) -> bool:
        for i in range(len(self.entries) - 1, -1, -1):
            if self.entries[i].fingerprint == fingerprint:
                e = self.entries[i]
                self.entries[i] = replace(e, suppressed=e.suppressed + 1)
                return True
        return False

    def since(self, after: datetime) -> Sequence[LedgerEntry]:
        return [e for e in self.entries if e.at > after]


@dataclass(slots=True)
class ActionLedger:
    store: LedgerStore
    cooldown: timedelta = timedelta(minutes=30)
    retry_after: timedelta = timedelta(minutes=5)
    max_per_subject_per_hour: int = 6
    _unsaved: list[LedgerEntry] = field(default_factory=list)
    _limited: set[tuple[str, datetime]] = field(default_factory=set)

    def check(self, action: Action, mode: Mode, now: datetime) -> tuple[Suppression, bool] | None:
        """Return ``(why, first)`` if ``action`` must be suppressed, else None."""
        fp = fingerprint(action)
        horizon = now - max(self.cooldown, self.retry_after)
        for e in self._with_unsaved(self.store.by_fingerprint(fp, horizon), fp=fp):
            if e.mode is not mode or e.outcome is Outcome.SUPPRESSED:
                continue
            window = self.retry_after if e.outcome is Outcome.FAILED else self.cooldown
            if e.at > now - window:
                return Suppression.COOLDOWN, False
        subj = subject(action)
        hour_ago = now - timedelta(hours=1)
        recent = self._with_unsaved(self.store.by_subject(subj, hour_ago), subj=subj)
        if sum(1 for e in recent if e.outcome in EFFECTS) >= self.max_per_subject_per_hour:
            # Remember the hit per subject and clock hour to flag only the first one.
            hour = now.replace(minute=0, second=0, microsecond=0)
            self._limited = {s for s in self._limited if s[1] >= hour - timedelta(hours=1)}
            first = (subj, hour) not in self._limited
            self._limited.add((subj, hour))
            return Suppression.RATE_LIMIT, first
        return None

    def record(self, dispatched: Dispatched, now: datetime) -> None:
        """Persist a decision. Raises if the store fails; the entry stays in memory."""
        if dispatched.outcome is Outcome.SUPPRESSED:
            fp = fingerprint(dispatched.action)
            for i in range(len(self._unsaved) - 1, -1, -1):
                if self._unsaved[i].fingerprint == fp:
                    e = self._unsaved[i]
                    self._unsaved[i] = replace(e, suppressed=e.suppressed + 1)
                    self.flush()
                    return
            if self.store.bump_suppressed(fp):
                return
        self._unsaved.append(LedgerEntry.of(dispatched, now))
        self.flush()

    def flush(self) -> None:
        while self._unsaved:
            self.store.append(self._unsaved[0])
            self._unsaved.pop(0)

    def _with_unsaved(
        self, entries: Sequence[LedgerEntry], fp: str | None = None, subj: str | None = None
    ) -> list[LedgerEntry]:
        extra = [
            e
            for e in self._unsaved
            if (fp is not None and e.fingerprint == fp) or (subj is not None and e.subject == subj)
        ]
        return [*entries, *extra]
