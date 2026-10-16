"""Basic token optimization from traces, no LLM: prompt caching, context growth and
restarts from zero.

Pure: no I/O and no clock. Input is what traces store (:mod:`kelpie.traces.semconv`): per
model call its input, cached and output tokens, model and time, never the prompt itself.
So every finding is about *how much* and *when*, not *what*: Kelpie can tell that the cache
broke on step 7 of a session after a ``bash`` call, not which line of the system prompt
changed.

**Steps.** The counted LLM spans of a session (nested instrumentations of the same call are
left out, :func:`kelpie.core.spans.counted_llm`), in start order, are its steps; step
numbers count the session's calls within the analysed spans (the report's day). A step's
input tokens are its context size: they include cached tokens, as in the GenAI conventions.
Ingest normalizes Claude Code, which reports Anthropic usage (input without cache reads and
writes); another instrumentation that passes Anthropic-native usage through unchanged
reports cached tokens above input tokens, which are cut to the input here, so its hit rate
reads too high.

**Prompt cache.** Agent loops resend the previous prompt plus the newest messages, so a
step can read from the cache about what the previous step sent:
``expected = min(previous input, this input)``. A step is *checked* when that is at least
``min_prefix_tokens`` (providers do not cache shorter prompts), the model did not change,
the steps did not overlap (parallel calls) and the context did not shrink below
``compaction_ratio`` of the previous step (a compaction or a new conversation). A checked
step misses when it read less than ``break_ratio`` of ``expected`` from the cache:

- ``expired``: more than ``cache_ttl`` passed since the previous step (5 minutes, the
  common default cache lifetime; providers and paid options differ);
- ``break``: the cache read dropped below ``break_ratio`` of the previous step's read, so
  the prefix changed (data that changes in the system prompt, tools in another order, an
  edited earlier message);
- ``partial``: the read did not drop but covers less than half the prompt: only a fixed
  prefix is cached (or nothing at all), not the growing conversation.

The hit rate per role and model is cached over input tokens of all its steps. The missed
tokens of a step are ``expected - cached``; they are priced as the difference between
uncached and cached input (cache-write premiums are not counted, so it is an upper bound).
A model that never reports a cache read anywhere in the traces may be one whose provider or
instrumentation does not report them: :func:`reports_cache` tells the report so, and it
does not price "turn caching on" for it.

**Context growth.** Per session: the context at the first, peak and last step and the mean
change per step (compactions left out). A session *runs away* when it has at least
``min_steps`` steps and its last context is at least ``runaway_tokens`` and
``runaway_factor`` times its first: it never compacted. The saving of compacting or handing
off at ``compact_at`` is at most the context above it, re-read on every later step; the
compaction's own call (a summary) is not counted, and the same steps' cache misses are
counted again under the prompt cache: the two are not additive.

**Restarts from zero.** A session of a task, started after an earlier session of the same
task and role ended *abnormally* (``abnormal``: it declared no real end, or Kelpie requeued
it), whose first context is below ``from_zero_ratio`` of where the earlier one stopped: the
work was not resumed but rebuilt. A session that follows a declared hand-off is a normal
next session, and sessions of an unknown role are left out. The *ramp* is its steps until
its context reaches the earlier session's last context, at most as many steps as the
earlier session took. Burned is the context re-acquired, once: ``min(earlier last context,
largest ramp context)`` input tokens, a lower bound on re-reading what was already known.
"""

from __future__ import annotations

import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Literal

from kelpie.core.model import Snapshot, Span, SpanKind
from kelpie.core.spans import Attribution, SpanKey, TokenUsage, last_at, span_key
from kelpie.traces.outputs import session_context

UNKNOWN_ROLE = "unknown"
MissKind = Literal["break", "expired", "partial"]
MISS_KINDS: tuple[MissKind, ...] = ("break", "expired", "partial")
_GroupKey = tuple[str, str | None]  # (role, model)


@dataclass(frozen=True, slots=True)
class TokenSettings:
    """Thresholds of the token analyses (defaults; see the module docs)."""

    cache_ttl: timedelta = timedelta(minutes=5)  # the providers' default cache lifetime
    break_ratio: float = 0.5
    min_prefix_tokens: int = 1024  # shorter prompts are not cached
    compaction_ratio: float = 0.5
    runaway_tokens: int = 100_000
    runaway_factor: float = 4.0
    min_steps: int = 5
    compact_at: int = 50_000
    from_zero_ratio: float = 0.5
    min_saving_tokens: int = 50_000  # a recommendation needs at least this a day


@dataclass(frozen=True, slots=True)
class Step:
    """One counted model call of a session."""

    key: SpanKey
    session_id: str
    index: int  # 1-based position in the session (among the spans analysed)
    start: datetime
    end: datetime
    model: str | None  # the pricing model
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    after_tool: str | None = None  # the last tool called since the previous step

    def usage(self, task_id: str | None = None) -> TokenUsage:
        return TokenUsage(
            self.session_id,
            task_id,
            self.model,
            1,
            self.input_tokens,
            self.output_tokens,
            min(self.cached_tokens, self.input_tokens),
        )


@dataclass(frozen=True, slots=True)
class SessionSteps:
    session_id: str
    role: str
    task_id: str | None
    steps: tuple[Step, ...]

    @property
    def start(self) -> datetime:
        return self.steps[0].start

    @property
    def end(self) -> datetime:
        return max(s.end for s in self.steps)


@dataclass(frozen=True, slots=True)
class CacheMiss:
    step: Step
    kind: MissKind
    expected: int  # tokens the step could have read from the cache
    task_id: str | None = None

    @property
    def missed(self) -> int:
        return max(self.expected - self.step.cached_tokens, 0)

    def usage(self, cached: bool) -> TokenUsage:
        """The missed tokens as input, read from the cache or not."""
        n = self.missed
        return TokenUsage(self.step.session_id, self.task_id, self.step.model, 0, n, 0, n * cached)


@dataclass(frozen=True, slots=True)
class CacheGroup:
    """Cache use of one role on one model."""

    role: str
    model: str | None
    calls: int
    input_tokens: int
    cached_tokens: int
    checked: int  # steps that could read the previous prompt from the cache
    misses: tuple[CacheMiss, ...]

    @property
    def hit_rate(self) -> float:
        return self.cached_tokens / self.input_tokens if self.input_tokens else 0.0

    def of(self, kind: MissKind) -> tuple[CacheMiss, ...]:
        return tuple(m for m in self.misses if m.kind == kind)

    def missed(self, kind: MissKind | None = None) -> int:
        return sum(m.missed for m in self.misses if kind is None or m.kind == kind)


@dataclass(frozen=True, slots=True)
class Growth:
    """Context size over one session's steps."""

    session: SessionSteps
    first: int
    peak: int
    last: int
    per_step: float  # mean change per step, compactions left out
    runaway: bool
    excess: tuple[TokenUsage, ...] = ()  # runaway: the context above ``compact_at``

    @property
    def steps(self) -> int:
        return len(self.session.steps)


@dataclass(frozen=True, slots=True)
class Restart:
    """A session that rebuilt from zero what an earlier session of its task had."""

    session: SessionSteps
    previous: SessionSteps
    ramp: tuple[Step, ...]  # the steps spent getting back to where ``previous`` stopped

    @property
    def tokens(self) -> int:
        """The context re-acquired, counted once."""
        reached = self.previous.steps[-1].input_tokens
        return min(reached, max(s.input_tokens for s in self.ramp))

    def usage(self) -> tuple[TokenUsage, ...]:
        """The re-acquired context as uncached input of the session's first model."""
        s = self.session
        return (TokenUsage(s.session_id, s.task_id, s.steps[0].model, 0, self.tokens, 0, 0),)


# --- steps -------------------------------------------------------------------------------


def session_roles(
    snapshot: Snapshot, remembered: Attribution | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """``(roles, tasks)`` per session id: the role of the session's owner, else of its task;
    and the task it worked on, as the board and the span store remember them."""
    roles, _ = session_context(snapshot, remembered)
    tasks = dict(remembered.session_tasks) if remembered else {}
    for session in snapshot.sessions:
        tasks[session.id] = session.task_id
    task_roles = {t.id: t.role for t in snapshot.tasks if t.role}
    for session, task in tasks.items():
        if session not in roles and task in task_roles:
            roles[session] = task_roles[task]
    return roles, tasks


def session_steps(
    spans: Iterable[Span],
    counted: Mapping[SpanKey, str | None],
    roles: Mapping[str, str] | None = None,
    tasks: Mapping[str, str] | None = None,
) -> dict[str, SessionSteps]:
    """The counted model calls among ``spans`` per session, in start order, with the last
    tool called before each one. Spans without a session are left out."""
    roles = roles or {}
    tasks = tasks or {}
    llm: dict[str, list[Span]] = defaultdict(list)
    tools: dict[str, list[Span]] = defaultdict(list)
    span_tasks: dict[str, str] = {}
    for span in spans:
        if not span.session_id:
            continue
        if span.task_id:
            span_tasks.setdefault(span.session_id, span.task_id)
        if span.kind is SpanKind.LLM and span_key(span) in counted:
            llm[span.session_id].append(span)
        elif span.kind is SpanKind.TOOL and span.tool:
            tools[span.session_id].append(span)
    out: dict[str, SessionSteps] = {}
    for session in sorted(llm):
        calls = sorted(llm[session], key=lambda s: (s.start, s.span_id))
        used = sorted(tools.get(session, ()), key=lambda s: (s.start, s.span_id))
        starts = [t.start for t in used]
        steps: list[Step] = []
        for i, span in enumerate(calls):
            after = None
            if i:
                # the last tool started since the previous call and before this one
                j = bisect_left(starts, span.start) - 1
                if j >= 0 and starts[j] >= calls[i - 1].start:
                    after = used[j].tool
            steps.append(
                Step(
                    key=span_key(span),
                    session_id=session,
                    index=i + 1,
                    start=span.start,
                    end=last_at(span),
                    model=counted[span_key(span)],
                    input_tokens=span.input_tokens,
                    output_tokens=span.output_tokens,
                    cached_tokens=min(span.cached_tokens, span.input_tokens),
                    after_tool=after,
                )
            )
        out[session] = SessionSteps(
            session_id=session,
            role=roles.get(session, UNKNOWN_ROLE),
            task_id=span_tasks.get(session) or tasks.get(session),
            steps=tuple(steps),
        )
    return out


# --- prompt cache ------------------------------------------------------------------------


def cache_misses(
    session: SessionSteps, settings: TokenSettings | None = None
) -> tuple[Counter[str | None], tuple[CacheMiss, ...]]:
    """Checked steps per model and the misses of one session (see the module docs)."""
    cfg = settings or TokenSettings()
    checked: Counter[str | None] = Counter()
    misses: list[CacheMiss] = []
    steps = session.steps
    for prev, step in pairwise(steps):
        expected = min(prev.input_tokens, step.input_tokens)
        if (
            expected < cfg.min_prefix_tokens
            or step.model != prev.model
            or step.start < prev.end  # overlapping: a parallel call, not the next step
            or step.input_tokens < cfg.compaction_ratio * prev.input_tokens
        ):
            continue
        checked[step.model] += 1
        if step.cached_tokens >= cfg.break_ratio * expected:
            continue
        kind: MissKind
        if step.start - prev.end > cfg.cache_ttl:
            kind = "expired"
        elif step.cached_tokens < cfg.break_ratio * prev.cached_tokens:
            kind = "break"
        else:
            kind = "partial"
        misses.append(CacheMiss(step, kind, expected, session.task_id))
    return checked, tuple(misses)


def cache_groups(
    sessions: Iterable[SessionSteps], settings: TokenSettings | None = None
) -> list[CacheGroup]:
    """Cache use per role and model, the most missed tokens first."""
    calls: Counter[_GroupKey] = Counter()
    inputs: Counter[_GroupKey] = Counter()
    cached: Counter[_GroupKey] = Counter()
    checked: Counter[_GroupKey] = Counter()
    misses: dict[_GroupKey, list[CacheMiss]] = defaultdict(list)
    for session in sessions:
        for step in session.steps:
            key = (session.role, step.model)
            calls[key] += 1
            inputs[key] += step.input_tokens
            cached[key] += step.cached_tokens
        counts, found = cache_misses(session, settings)
        for model, n in counts.items():
            checked[(session.role, model)] += n
        for miss in found:
            misses[(session.role, miss.step.model)].append(miss)
    groups = [
        CacheGroup(
            role=role,
            model=model,
            calls=calls[(role, model)],
            input_tokens=inputs[(role, model)],
            cached_tokens=cached[(role, model)],
            checked=checked[(role, model)],
            misses=tuple(misses[(role, model)]),
        )
        for role, model in calls
    ]
    groups.sort(key=lambda g: (-g.missed(), g.role, g.model or ""))
    return groups


def break_hint(group: CacheGroup, quote: Callable[[str], str] = "`{}`".format) -> str:
    """What the breaks of a group point at, from when they happen. ``quote`` renders a tool
    name (a Markdown report passes one that escapes it)."""
    breaks = group.of("break")
    if not breaks:
        return ""
    if group.checked and len(breaks) * 2 >= group.checked:
        return (
            "the prefix changes on most calls: look for data that changes (time, ids, "
            "counters) in the system prompt and for tools sent in a different order"
        )
    after = Counter(m.step.after_tool for m in breaks if m.step.after_tool)
    if after:
        tool, count = max(sorted(after.items()), key=lambda kv: kv[1])
        if count * 2 > len(breaks):
            return (
                f"most breaks follow {quote(tool)} ({count} of {len(breaks)}): check whether it "
                "changes the instructions or the tool list, or edits earlier messages"
            )
    return (
        "keep the system prompt and the tool list stable within a session and put "
        "changing data after the cached prefix"
    )


# --- context growth ----------------------------------------------------------------------


def context_growth(
    sessions: Iterable[SessionSteps], settings: TokenSettings | None = None
) -> list[Growth]:
    """Context growth per session, runaway sessions first, then by peak context."""
    cfg = settings or TokenSettings()
    out: list[Growth] = []
    for session in sessions:
        sizes = [s.input_tokens for s in session.steps]
        deltas = [b - a for a, b in pairwise(sizes) if b >= cfg.compaction_ratio * a]
        first, last = sizes[0], sizes[-1]
        runaway = (
            len(sizes) >= cfg.min_steps
            and last >= cfg.runaway_tokens
            and last >= cfg.runaway_factor * max(first, 1)
        )
        excess: list[TokenUsage] = []
        if runaway:
            for step in session.steps:
                over = step.input_tokens - cfg.compact_at
                if over <= 0:
                    continue
                share = over / step.input_tokens
                excess.append(
                    TokenUsage(
                        session.session_id,
                        session.task_id,
                        step.model,
                        0,
                        over,
                        0,
                        round(step.cached_tokens * share),
                    )
                )
        out.append(
            Growth(
                session=session,
                first=first,
                peak=max(sizes),
                last=last,
                per_step=statistics.fmean(deltas) if deltas else 0.0,
                runaway=runaway,
                excess=tuple(excess),
            )
        )
    out.sort(key=lambda g: (not g.runaway, -g.peak, g.session.session_id))
    return out


def reports_cache(spans: Iterable[Span], counted: Mapping[SpanKey, str | None]) -> set[str]:
    """Models with at least one counted call that reported a cache read: their provider and
    instrumentation report cache tokens, so zero reads elsewhere are real misses."""
    return {
        model for span in spans if span.cached_tokens > 0 and (model := counted.get(span_key(span)))
    }


def excess_tokens(growth: Growth) -> int:
    return sum(u.input_tokens for u in growth.excess)


# --- restarts from zero ------------------------------------------------------------------


def restarts(
    sessions: Mapping[str, SessionSteps],
    start: datetime,
    end: datetime,
    abnormal: Collection[str],
    settings: TokenSettings | None = None,
) -> list[Restart]:
    """Sessions that started within ``[start, end)`` and rebuilt from zero what an earlier
    session of the same task and role had, after it ended abnormally (session ids in
    ``abnormal``; see the module docs). ``sessions`` should reach back far enough to hold
    the earlier ones."""
    cfg = settings or TokenSettings()
    by_task: dict[tuple[str, str], list[SessionSteps]] = defaultdict(list)
    for s in sessions.values():
        if s.task_id and s.steps and s.role != UNKNOWN_ROLE:
            by_task[(s.task_id, s.role)].append(s)
    out: list[Restart] = []
    for group in by_task.values():
        group.sort(key=lambda s: (s.start, s.session_id))
        for i, current in enumerate(group):
            if not start <= current.start < end:
                continue
            earlier = [p for p in group[:i] if p.end <= current.start]
            if not earlier:
                continue
            previous = max(earlier, key=lambda p: (p.end, p.session_id))
            if previous.session_id not in abnormal:
                continue  # a declared hand-off: a normal next session
            reached = previous.steps[-1].input_tokens
            if current.steps[0].input_tokens >= cfg.from_zero_ratio * reached:
                continue  # resumed with its context
            ramp: list[Step] = []
            for step in current.steps[: len(previous.steps)]:
                if step.input_tokens >= reached:
                    break
                ramp.append(step)
            if ramp:
                out.append(Restart(current, previous, tuple(ramp)))
    out.sort(key=lambda r: (-r.tokens, r.session.session_id))
    return out
