"""Rule 11 — activity without progress (field evidence: idle context resend).

Idle agents keep spending tokens without moving any task: a session with nothing to do wakes
up every few minutes and resends its whole context to the model. Watchdogs that look for
dead agents never see it, because the agent is alive and busy. This rule compares, per
session and over a sliding ``window`` (default 30m), the tokens spent with the progress the
session made:

- **tokens**: input plus output tokens of the session's LLM spans that started in the window.
  Nested LLM spans are counted once (``kelpie.rules._tokens``). Only raw spans carry
  timestamps, so the rule reads ``snapshot.spans`` and not the coarser ``span_totals``;
  ``[traces] window`` (30m by default) must therefore be at least this rule's ``window``,
  otherwise the tokens are undercounted (Kelpie warns at startup);
- **progress**: a task status change after the session started (``task.status_since`` later
  than ``started_at`` plus ``claim_grace``, default 2m), a commit (``Session.last_commit_at``)
  or an artifact (``Session.artifacts``), inside the window. Claiming the task is not
  progress: the status change that starts a session happens at, or just around,
  ``started_at``.

Tokens of at least ``min_tokens_without_progress`` (default 100k) with no progress → one
``alert`` with the tokens burned and recommendations. Stopping a runaway loop is rule 10's
job; this rule only warns. Sessions that ended before the window began are ignored.

One alert per session, not one per tick: the alert is a lasting state (``Action.state``) per
session, with ``since`` set to the last sign of progress (or the session start). The engine
announces it once, repeats it only at its escalation thresholds, and closes it when the rule
stops reporting it. The alert key includes ``since``, so a session that makes progress and
then stalls again is announced again.

The recommendations cannot be told apart from the data (a session never says why it is
idle), so all three are always given.

**Runaway context** (issue #197). A session that keeps growing its context re-reads all of
it on every step, whether it makes progress or not: on a live farm 10 to 13 sessions a day
grew to 160k-433k tokens (+1.4k-6.9k per step), 30-80M re-read tokens a day. Per session of
the spans (its ``session_id``, whether or not the board knows the session), over the counted
LLM spans that started in ``window``: at least ``context_min_steps`` steps and a latest
step whose context (input tokens, cached ones included) is at least ``context_alert_tokens``
→ one ``info`` alert, a lasting state per session, with the context, the mean growth per
step (compactions, a step below half the one before, left out) and the tokens re-read in the
window, recommending compaction or a bounded session. Once open it stays open while the
context is above ``CONTEXT_KEEP`` of the threshold, so a session near it does not flap, and
it closes when the session compacts or goes quiet. The task is the board session's, else
the spans'. ``context_alert_tokens = 0`` turns it off.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Session, Snapshot, Span
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._alerts import alert_params
from kelpie.rules._tokens import SpanIndex, outermost_llm_spans, span_index, tokens_since

NAME = "no_progress"

RECOMMENDATIONS = (
    "longer_heartbeat_interval",
    "cheaper_maintenance_model",
    "bounded_session_handoff",
)
CONTEXT_RECOMMENDATIONS = ("compact_context", "bounded_session_handoff")
CONTEXT_KEEP = 0.8  # an open runaway-context alert stays open above this share of the bar
COMPACTION_RATIO = 0.5  # a step below this share of the one before compacted the context


@dataclass(frozen=True, slots=True)
class NoProgress:
    window: timedelta = timedelta(minutes=30)  # the default [traces] window
    min_tokens_without_progress: int = 100_000
    claim_grace: timedelta = timedelta(minutes=2)
    # Runaway context: a session's latest context at least this (0: off), after this many steps.
    context_alert_tokens: int = 150_000
    context_min_steps: int = 5
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        window_start = snapshot.now - self.window
        index = span_index(snapshot.spans)
        actions: list[Action] = []
        for session in snapshot.sessions:
            if session.ended_at is not None and session.ended_at < window_start:
                continue
            tokens = tokens_since(snapshot.spans_for_session(session.id), window_start, index)
            if tokens < self.min_tokens_without_progress:
                continue
            last = self._last_progress(session, snapshot)
            if last is None or last < window_start:
                stalled_since = max(last or session.started_at, session.started_at)
                actions.append(self._alert(session, tokens, stalled_since))
        if self.context_alert_tokens:
            actions.extend(self._runaway_context(snapshot, index, window_start))
        return actions

    def _runaway_context(
        self, snapshot: Snapshot, index: SpanIndex, window_start: datetime
    ) -> list[Action]:
        steps: dict[str, list[Span]] = defaultdict(list)
        for span in outermost_llm_spans(snapshot.spans, index):
            if span.session_id and window_start <= span.start <= snapshot.now:
                steps[span.session_id].append(span)
        board = {s.id: s for s in snapshot.sessions}
        actions: list[Action] = []
        for sid, spans in sorted(steps.items()):
            spans.sort(key=lambda s: (s.start, s.span_id))
            state = f"{NAME}:context:{sid}"
            bar = self.context_alert_tokens
            if state in snapshot.open_states:
                bar = int(bar * CONTEXT_KEEP)
            sizes = [s.input_tokens for s in spans]
            if len(sizes) < self.context_min_steps or sizes[-1] < bar:
                continue
            grown = [b - a for a, b in pairwise(sizes) if b >= COMPACTION_RATIO * a]
            per_step = round(statistics.fmean(grown)) if grown else 0
            session = board.get(sid)
            task_id = (
                session.task_id
                if session is not None
                else next((s.task_id for s in reversed(spans) if s.task_id), None)
            )
            actions.append(
                self._context_alert(sid, task_id, sizes, per_step, spans[0].start, spans[-1].model)
            )
        return actions

    def _context_alert(
        self,
        sid: str,
        task_id: str | None,
        sizes: Sequence[int],
        per_step: int,
        since: datetime,
        model: str | None,
    ) -> Action:
        window = _fmt(self.window)
        reread = sum(sizes)
        of_task = f" of task {task_id}" if task_id else ""
        return Action(
            ActionKind.ALERT,
            NAME,
            f"Session {sid}{of_task} carries a {sizes[-1]:,}-token context after "
            f"{len(sizes)} steps in the last {window} ({per_step:+,} per step), and every "
            f"step re-reads all of it: {reread:,} input tokens in {window}. Compact the "
            "context, or bound the session and hand off to a fresh one with a summary.",
            task_id=task_id,
            params={
                "session_id": sid,
                "context_tokens": sizes[-1],
                "steps": len(sizes),
                "per_step": per_step,
                "reread_tokens": reread,
                "window": window,
                "model": model,
                "recommendations": list(CONTEXT_RECOMMENDATIONS),
                **alert_params(
                    f"Session {sid}: {sizes[-1] // 1000}k-token context, {per_step:+,} per step",
                    "Compact the context or bound the session" if task_id else None,
                ),
            },
            key=f"{NAME}:context:{sid}",
            state=f"{NAME}:context:{sid}",
            since=since,
        )

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
                **alert_params(
                    f"No progress on {session.task_id}: {tokens:,} tokens in {window}",
                    "Check whether the session is stuck before it spends more",
                ),
            },
            key=f"{NAME}:{session.id}:{stalled_since.isoformat()}",
            state=f"{NAME}:{session.id}",
            since=stalled_since,
        )

    def _last_progress(self, session: Session, snapshot: Snapshot) -> datetime | None:
        """The latest sign of progress of the session, at any time; None if it made none."""
        signals: list[datetime] = []
        task = snapshot.task(session.task_id)
        if task is not None and task.status_since > session.started_at + self.claim_grace:
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


def _duration(value: Any, name: str, allow_zero: bool = False) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result < timedelta(0) or (result == timedelta(0) and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


@register(NAME)
def build(config: dict[str, Any]) -> NoProgress:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "window" in config:
        kwargs["window"] = _duration(config.pop("window"), "window")
    if "claim_grace" in config:
        kwargs["claim_grace"] = _duration(config.pop("claim_grace"), "claim_grace", True)
    if "min_tokens_without_progress" in config:
        n = config.pop("min_tokens_without_progress")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("min_tokens_without_progress must be a positive integer")
        kwargs["min_tokens_without_progress"] = n
    for key, least in (("context_alert_tokens", 0), ("context_min_steps", 2)):
        if key in config:
            n = config.pop(key)
            if not isinstance(n, int) or isinstance(n, bool) or n < least:
                raise ValueError(f"{key} must be an integer >= {least}")
            kwargs[key] = n
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return NoProgress(**kwargs)
