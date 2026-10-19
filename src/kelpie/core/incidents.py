"""Farm-wide incidents that explain many tasks' waits at once (incident 20).

A CI outage (a broken toolchain on a runner, every PR red for hours) is one incident, not
one alert per blocked task. Rule 9 (``ci_economy``) reports it as a long-lived alert state;
the engine folds the per-task alerts it explains under it; the daily report attributes the
waits it explains to "CI outage". This module is the contract between the three. It lives
in core because the engine reads it; it names rule 9's state ids because a state id starts
with its rule's name.

- **State.** :func:`outage_state_id` (``ci_economy:outage:<key>``), where ``<key>`` hashes
  the signature's parts: workflow, job and failing step, not the repository (one broken
  runner spans repositories). ``since`` is the first correlated failure; ``last_seen``
  advances while it is open; it resolves when a build passes the signature's job again.
  The same signature failing again later opens the same state id anew: tell occurrences
  apart by their time span. The alert key is the state id, stable for the outage's life.
- **Params**: a :class:`CiOutage` (:meth:`CiOutage.to_params`), read back with
  :meth:`CiOutage.from_params`. The ledger stores them with each announcement and, while
  the outage stays open, with the suppressed repeat it writes at most once an hour
  (repeats in between only count on that entry). So the scope (``prs``, ``tasks``) of an
  entry is as of that entry: a task that joined since is matched by its repository
  (``repos``).
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

OUTAGE_STATE_PREFIX = "ci_economy:outage:"
FOLDED_UNDER = "folded_under"  # param of a folded alert: the state id of its incident
MAX_BUILDS = 20  # failing build ids an outage's params list, newest first


def outage_signature(workflow: str | None, job: str, step: str | None) -> str:
    """The signature as people read it: ``ci.yml > build > Select toolchain``."""
    return " > ".join(part for part in (workflow, job, step) if part)


def outage_state_id(workflow: str | None, job: str, step: str | None) -> str:
    """The state id of the outage with this signature (a 12-hex key after the prefix)."""
    text = "\n".join((workflow or "", job, step or ""))
    return OUTAGE_STATE_PREFIX + hashlib.sha256(text.encode()).hexdigest()[:12]


def is_outage_state(state_id: str) -> bool:
    """Whether a state id (an alert state, or a folded alert's ``folded_under``) is an
    outage's."""
    return state_id.startswith(OUTAGE_STATE_PREFIX)


@dataclass(frozen=True, slots=True)
class CiOutage:
    """What a CI outage alert carries in its params.

    ``state`` is its state id; ``workflow``, ``job`` and ``step`` the signature's parts, and
    ``signature`` how people read it. ``prs`` are ``owner/name#N``. ``tasks`` are the tasks
    the outage explains: the blocked or waiting tasks of the PRs in scope, and tasks waiting
    on an unmet CI or merge condition of those PRs. ``runners`` are the runner names and
    labels of the failing jobs; ``builds`` the failing build ids (at most
    :data:`MAX_BUILDS`)."""

    state: str
    signature: str
    job: str
    workflow: str | None = None
    step: str | None = None
    repos: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    runners: tuple[str, ...] = ()
    builds: tuple[str, ...] = ()

    def to_params(self) -> dict[str, Any]:
        """The alert's params (JSON-ready)."""
        return {
            "state": self.state,
            "signature": self.signature,
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
    def from_params(cls, params: object) -> CiOutage | None:
        """The outage an alert's params describe; None unless they name an outage state, a
        signature and a job. Missing or malformed lists read as empty, unknown keys are
        ignored."""
        if not isinstance(params, Mapping):
            return None
        state, signature, job = (_text(params.get(k)) for k in ("state", "signature", "job"))
        if state is None or not is_outage_state(state) or signature is None or job is None:
            return None
        return cls(
            state=state,
            signature=signature,
            job=job,
            workflow=_text(params.get("workflow")),
            step=_text(params.get("step")),
            repos=_texts(params.get("repos")),
            workflows=_texts(params.get("workflows")),
            prs=_texts(params.get("prs")),
            tasks=_texts(params.get("tasks")),
            runners=_texts(params.get("runners")),
            builds=_texts(params.get("builds"))[:MAX_BUILDS],
        )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | Mapping):
        return ()
    return tuple(v for v in value if _text(v) is not None)
