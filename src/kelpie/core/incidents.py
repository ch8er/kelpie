"""Farm-wide incidents that explain many tasks' waits at once (incident 20).

A CI outage (a broken toolchain on a runner, every PR red for hours) is one incident, not
one alert per blocked task. Rule 9 (``ci_economy``) reports it as a long-lived alert state;
the daily report attributes the waits it explains to "CI outage". This module is the
contract between the two, so neither depends on the other:

- **State.** ``ci_economy:outage:<key>`` (:data:`OUTAGE_STATE_PREFIX`), where ``<key>`` is a
  short stable hash of the signature (:func:`outage_key`). ``since`` is the first correlated
  failure; ``last_seen`` advances while it is open; it resolves when a build passes the
  signature's job again. The alert key is the state id, stable for the outage's life.
- **Params**, on every handling of that alert, including the suppressed repeats while it
  stays open (so the scope can grow without alerting anyone again): see :class:`CiOutage`.
  Read them back with :meth:`CiOutage.from_params`.
- **Folded alerts.** Other rules' alerts about tasks in ``tasks`` are held back while the
  outage is open; the ledger records each as suppressed (``incident``) with
  ``params[FOLDED_UNDER]`` set to the outage's state id.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

OUTAGE_STATE_PREFIX = "ci_economy:outage:"
FOLDED_UNDER = "folded_under"  # param of a folded alert: the state id of its incident
_SEP = " > "


def outage_key(workflow: str | None, job: str, step: str | None) -> str:
    """The stable key of an outage with this signature (12 hex characters)."""
    text = "\n".join((workflow or "", job, step or ""))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class CiOutage:
    """What a CI outage alert carries in its params.

    ``signature`` is human-readable (``workflow > job > step``); ``job`` and ``step`` are its
    parts. ``prs`` are ``owner/name#N``. ``tasks`` are the tasks the outage explains: the
    blocked or waiting tasks of the PRs in scope, and tasks waiting on an unmet CI or merge
    condition of those PRs. ``runners`` are the runner names and labels of the failing jobs;
    ``builds`` the failing build ids (capped)."""

    signature: str
    job: str = ""
    step: str | None = None
    repos: tuple[str, ...] = ()
    workflows: tuple[str, ...] = ()
    prs: tuple[str, ...] = ()
    tasks: tuple[str, ...] = ()
    runners: tuple[str, ...] = ()
    builds: tuple[str, ...] = ()

    @staticmethod
    def describe(workflow: str | None, job: str, step: str | None) -> str:
        """The signature as people read it: ``ios.yml > build-ios > Select toolchain``."""
        return _SEP.join(part for part in (workflow, job, step) if part)

    def to_params(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
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
    def from_params(cls, params: Mapping[str, Any]) -> CiOutage | None:
        """The outage an alert's params describe; None without a signature. Missing or
        malformed lists read as empty, unknown keys are ignored."""
        signature = params.get("signature")
        if not isinstance(signature, str) or not signature:
            return None
        job, step = params.get("job"), params.get("step")
        return cls(
            signature=signature,
            job=job if isinstance(job, str) else "",
            step=step if isinstance(step, str) and step else None,
            repos=_strings(params.get("repos")),
            workflows=_strings(params.get("workflows")),
            prs=_strings(params.get("prs")),
            tasks=_strings(params.get("tasks")),
            runners=_strings(params.get("runners")),
            builds=_strings(params.get("builds")),
        )


def is_outage_state(state_id: str) -> bool:
    return state_id.startswith(OUTAGE_STATE_PREFIX)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, str | bytes | Mapping):
        return ()
    return tuple(v for v in value if isinstance(v, str) and v)
