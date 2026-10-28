"""Farm-wide incidents that explain many tasks' waits at once (incident 20).

A CI outage (a broken toolchain on a runner, every PR red for hours), a block storm (seven
agents blocked within the hour on "waiting for CEO (Xcode first launch)") or CI jobs that
never start are one incident each, not one alert per task. The rule that detects one
reports it as a long-lived alert state; the engine folds the per-task alerts it explains
under it; the daily report attributes the waits it explains to the outage (rule 9's failing
CI jobs today; the other kinds as the report learns them, #229). This module is the
contract between the three. It lives in core because the engine reads it.

- **State.** ``<rule>:outage:<key>`` (:func:`incident_state_id`; :func:`outage_state_id` for
  rule 9's failing CI jobs), where ``<key>`` hashes what identifies the outage: for failing
  CI jobs the workflow, job and failing step, not the repository (one broken runner spans
  repositories); for a block storm the shared block reason. ``since`` is its first sign;
  ``last_seen`` advances while it is open. The same outage recurring later opens the same
  state id anew: tell occurrences apart by their time span. The alert key is the state id,
  stable for the outage's life. :func:`is_outage_state` matches every rule's outages.
- **Params**: an :class:`Outage` (:meth:`Outage.to_params`), read back with
  :meth:`Outage.from_params`. ``kind`` says which signal found it (:data:`KINDS`); params
  without one (written before kinds existed) read as failing CI jobs. The ledger stores
  them with each announcement and, while the outage stays open, with the suppressed repeat
  it writes at most once an hour (repeats in between only count on that entry). So the
  scope (``prs``, ``tasks``) of an entry is as of that entry: for CI kinds a task that joined
  since is matched by its repository (``repos``); a block storm has no repositories, so its
  tasks are those of its latest sample.
- **Folded alerts** (the engine): other rules' alerts that a task's block or wait lasts too
  long or names no condition (``Action.about_wait``) about tasks in ``tasks`` are held back
  while the outage is open: not critical ones, not ones more severe than the outage's alert,
  and only while that alert reached people (delivered now or announced live before; when it
  was only logged, only alerts that would only be logged too); dry-run folds what live mode
  would. A tick in which the outage is not reported keeps its last folds within
  ``resolve_after`` of its last sighting. The ledger records each folded alert as suppressed
  (``incident``) with ``params[FOLDED_UNDER]`` set to the outage's state id, under a
  fingerprint of its own: one entry when a fold starts, then at most one an hour (or per
  cooldown if longer; the same outage recurring within it adds to the previous
  occurrence's entry). Its own state stays open and is seen, so it neither resolves nor
  re-announces meanwhile.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

OUTAGE = "outage"  # the middle part of every outage's state id
_OUTAGE_ID = re.compile(rf"[a-z][a-z0-9_]*:{OUTAGE}:[0-9a-f]{{12}}")
# Rule 9's outages: failing CI jobs, and CI_NOT_STARTED_STATE.
OUTAGE_STATE_PREFIX = "ci_economy:outage:"
FOLDED_UNDER = "folded_under"  # param of a folded alert: the state id of its incident
MAX_BUILDS = 20  # failing build ids an outage's params list, newest first

# What found the outage (``Outage.kind``).
CI_FAILURES = "ci_failures"  # rule 9: change requests failing at the same CI job and step
CI_NOT_STARTED = "ci_not_started"  # rule 9: CI jobs that never start
BLOCK_STORM = "block_storm"  # rule 4: many tasks blocked on the same reason at once
# A closed set on purpose: params of an unknown kind (a newer Kelpie's) are not read as
# something they are not; a new signal adds its kind here.
KINDS = frozenset({CI_FAILURES, CI_NOT_STARTED, BLOCK_STORM})


def outage_signature(workflow: str | None, job: str, step: str | None) -> str:
    """The signature as people read it: ``ci.yml > build > Select toolchain``."""
    return " > ".join(part for part in (workflow, job, step) if part)


def incident_state_id(rule: str, *parts: str | None) -> str:
    """The state id of ``rule``'s outage identified by ``parts`` (a 12-hex key)."""
    text = "\n".join(part or "" for part in parts)
    return f"{rule}:{OUTAGE}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def outage_state_id(workflow: str | None, job: str, step: str | None) -> str:
    """The state id of rule 9's outage of failing CI jobs with this signature."""
    return incident_state_id("ci_economy", workflow, job, step)


# Rule 9's one farm-wide outage of CI jobs that never start.
CI_NOT_STARTED_STATE = incident_state_id("ci_economy", "not_started")


def is_outage_state(state_id: str) -> bool:
    """Whether a state id (an alert state, or a folded alert's ``folded_under``) is an
    outage's, whichever rule found it: exactly what :func:`incident_state_id` makes, so a
    task, host or session named "outage" in another rule's state id is no outage."""
    return _OUTAGE_ID.fullmatch(state_id) is not None


@dataclass(frozen=True, slots=True)
class Outage:
    """What an outage alert carries in its params.

    ``state`` is its state id and ``kind`` the signal that found it (:data:`KINDS`).
    ``signature`` is how people read what is shared (a CI job, a block reason, a tool);
    ``cause`` says in one line what happened; ``person`` is who can end it, when its cause
    names someone (a block storm's "waiting for CEO"). For failing CI jobs, ``workflow``,
    ``job`` and ``step`` are the signature's parts. ``prs`` are ``owner/name#N``. ``tasks``
    are the tasks the outage explains (for CI kinds: the blocked or waiting tasks of the
    PRs in scope, and tasks waiting on an unmet CI or merge condition of those PRs; for a
    block storm: the blocked tasks). ``runners`` are runner names and labels; ``builds``
    build ids (at most :data:`MAX_BUILDS`)."""

    state: str
    signature: str
    job: str = ""
    workflow: str | None = None
    step: str | None = None
    repos: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    runners: tuple[str, ...] = ()
    builds: tuple[str, ...] = ()
    kind: str = CI_FAILURES
    cause: str | None = None
    person: str | None = None

    def to_params(self) -> dict[str, Any]:
        """The alert's params (JSON-ready)."""
        return {
            "state": self.state,
            "kind": self.kind,
            "signature": self.signature,
            "cause": self.cause,
            "person": self.person,
            "workflow": self.workflow,
            "job": self.job,
            "step": self.step,
            "repos": list(self.repos),
            "workflows": list(self.workflows),
            "prs": list(self.prs),
            "tasks": list(self.tasks),
            "runners": list(self.runners),
            "builds": list(self.builds),
        }

    @classmethod
    def from_params(cls, params: object) -> Outage | None:
        """The outage an alert's params describe; None unless they name an outage state, a
        known kind (none: failing CI jobs) and a signature, and for failing CI jobs a job.
        Missing or malformed lists read as empty, unknown keys are ignored."""
        if not isinstance(params, Mapping):
            return None
        state, signature, job = (_text(params.get(k)) for k in ("state", "signature", "job"))
        kind = params.get("kind", CI_FAILURES)
        if state is None or not is_outage_state(state) or signature is None:
            return None
        if not isinstance(kind, str) or kind not in KINDS or (kind == CI_FAILURES and not job):
            return None
        return cls(
            state=state,
            signature=signature,
            job=job or "",
            workflow=_text(params.get("workflow")),
            step=_text(params.get("step")),
            repos=_texts(params.get("repos")),
            workflows=_texts(params.get("workflows")),
            prs=_texts(params.get("prs")),
            tasks=_texts(params.get("tasks")),
            runners=_texts(params.get("runners")),
            builds=_texts(params.get("builds"))[:MAX_BUILDS],
            kind=kind,
            cause=_text(params.get("cause")),
            person=_text(params.get("person")),
        )


CiOutage = Outage  # the name before outages had kinds


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | Mapping):
        return ()
    return tuple(v for v in value if _text(v) is not None)
