"""Rule 11 — activity without progress (field evidence: idle context resend).

Idle agents keep spending tokens without moving any task: a session with nothing to do wakes
up every few minutes and resends its whole context to the model. Watchdogs that look for
dead agents never see it, because the agent is alive and busy. This rule compares, per
session and over a sliding ``window`` (default 1h), the tokens spent with the progress the
session made:

- **tokens**: input plus output tokens of the session's LLM spans that started in the window.
  Nested LLM spans are counted once (``kelpie.rules._tokens``). Only raw spans carry
  timestamps, so the rule reads ``snapshot.spans`` and not the coarser ``span_totals``;
  ``[traces] window`` (30m by default) must therefore be at least this rule's ``window``,
  otherwise the tokens are undercounted;
- **progress**: a task status change after the session started (``task.status_since``), a
  commit (``Session.last_commit_at``) or an artifact (``Session.artifacts``), inside the
  window. Claiming the task is not progress: the status change that starts a session
  happens at or before ``started_at``.

Tokens of at least ``min_tokens_without_progress`` (default 50k) with no progress → one
``alert`` with the tokens burned and recommendations. Stopping a runaway loop is rule 10's
job; this rule only warns. Sessions that ended before the window began are ignored.

One alert per session, not one per tick: the alert is a lasting state (``Action.state``) per
session, with ``since`` set to the last sign of progress (or the session start). The engine
announces it once, repeats it only at its escalation thresholds, and closes it when the rule
stops reporting it. The alert key includes ``since``, so a session that makes progress and
then stalls again is announced again.

The recommendations cannot be told apart from the data (a session never says why it is
idle), so all three are always given.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Session, Snapshot
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tokens import tokens_since

NAME = "no_progress"

RECOMMENDATIONS = (
    "longer_heartbeat_interval",
    "cheaper_maintenance_model",
    "bounded_session_handoff",
)


@dataclass(frozen=True, slots=True)
class NoProgress:
    window: timedelta = timedelta(hours=1)
    min_tokens_without_progress: int = 50_000
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        window_start = snapshot.now - self.window
        actions: list[Action] = []
        for session in snapshot.sessions:
            if session.ended_at is not None and session.ended_at < window_start:
                continue
            tokens = tokens_since(snapshot.spans_for_session(session.id), window_start)
            if tokens < self.min_tokens_without_progress:
                continue
            last = _last_progress(session, snapshot)
            if last is None or last < window_start:
                stalled_since = max(last or session.started_at, session.started_at)
                actions.append(self._alert(session, tokens, stalled_since))
        return actions

    def _alert(self, session: Session, tokens: int, stalled_since: datetime) -> Action:
        window = _fmt(self.window)
        return Action(
            ActionKind.ALERT,
            NAME,
            f"Session {session.id} of task {session.task_id} spent {tokens:,} tokens in the "
            f"last {window} with no progress: no status change, commit or artifact.",
            task_id=session.task_id,
            severity=Severity.WARNING,
            params={
                "session_id": session.id,
                "owner_id": session.owner_id,
                "tokens": tokens,
                "window": window,
                "recommendations": list(RECOMMENDATIONS),
            },
            key=f"{NAME}:{session.id}:{stalled_since.isoformat()}",
            state=f"{NAME}:{session.id}",
            since=stalled_since,
        )


def _last_progress(session: Session, snapshot: Snapshot) -> datetime | None:
    """The latest sign of progress of the session, at any time; None if it made none."""
    signals: list[datetime] = []
    task = snapshot.task(session.task_id)
    if task is not None and task.status_since > session.started_at:
        signals.append(task.status_since)
    if session.last_commit_at is not None:
        signals.append(session.last_commit_at)
    signals.extend(a.at for a in session.artifacts)
    return max(signals, default=None)


def _fmt(d: timedelta) -> str:
    """A duration in the config syntax (``1h30m``), so params round-trip through config."""
    seconds = int(d.total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "".join(f"{n}{u}" for n, u in ((hours, "h"), (minutes, "m"), (secs, "s")) if n) or "0s"


def _positive_duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return result


@register(NAME)
def build(config: dict[str, Any]) -> NoProgress:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "window" in config:
        kwargs["window"] = _positive_duration(config.pop("window"), "window")
    if "min_tokens_without_progress" in config:
        n = config.pop("min_tokens_without_progress")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("min_tokens_without_progress must be a positive integer")
        kwargs["min_tokens_without_progress"] = n
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return NoProgress(**kwargs)
