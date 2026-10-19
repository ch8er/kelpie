"""Token optimization from traces, no LLM: repeated fragments, unused tools, model fit per
role and diff-only review.

Pure: no I/O and no clock. Traces keep no prompt or tool content
(:mod:`kelpie.traces.semconv`), only sizes, tool names, argument hashes and command
prefixes, so each analysis works on those:

**Repeated fragments.** The same content loaded into many sessions of a role: calls of a
read-like tool (not a shell command: those are runs, and huge outputs have their own
analysis) with the same ``args_hash`` (the same file, the same query) whose output size
stays within ``fragment_tolerance`` of the group's median (the content did not change),
each at least ``fragment_min_tokens``, in at least ``fragment_min_sessions`` sessions of the
day. Text repeated inside prompts themselves cannot be seen: prompts are not stored.

**Unused tools.** Tools offered to a role that it never calls. Traces record the tools
*called*; which tools were *offered* (their definitions) is not stored. A platform or hook
can name them in ``kelpie.tools.offered``: a comma-separated *span* attribute (resource
attributes other than ``service.name`` are not kept) on any span of the session, cut at 256
characters like every kept value. Without it unused tools are unknown, and the report
shows nothing for the role.

**Model fit.** A role on one model with at least ``routine_min_calls`` calls a day whose
median output is at most ``routine_output_tokens`` and whose tool calls return at most
``routine_max_tool_tokens`` on average (triage, polling, status updates; a coding loop
reads files and runs tests) is a candidate for a cheaper model: the cheapest model of
``[prices]`` (exact names) cheaper on both input and output that the farm already runs
(seen in its traces) or that ``allowed_models`` names. Short outputs alone do not make work
routine, so the report prices a switch only for ``routine_roles`` that the operator named.
**After a switch** (the role's most used model differs from the day
before) quality is compared before and after the first call on the new model (from it to
the end of the day, and over as long but at least a day before it): task returns and review
returns (ping-pong rounds) per task of the role active in each window. Worse by at
least ``worse_by`` returns and per task: the switch is flagged and no further downgrade is
suggested. With less than ``min_after`` since the switch, or fewer than two tasks after
it, it is too early to tell, and no further downgrade is suggested either.

**Diff-only review.** Sessions of review roles on a task that made measured reads, in
start order, are its review rounds. A read is a call matching ``review_reads`` (file reads,
``git diff``, ``gh pr diff``, ...) with an ``args_hash``; a hash also read for another task
is a shared file (instructions such as an agents file), not the PR, and is left out. In
round 2 and later, a read with the same key and hash as in an earlier round re-reads what
was already reviewed:
``min(earlier size, size)`` tokens (all of an unchanged file, the old part of a grown diff).
A round whose re-read tokens are at least ``rereview_share`` of its reads re-read the whole
PR instead of the changes since the last round. Reads without an ``args_hash`` (platforms
that report no arguments) cannot be compared and are counted apart.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from fnmatch import fnmatchcase

from kelpie.core.budgets import PriceTable
from kelpie.core.model import Span, SpanKind, Task
from kelpie.core.spans import SpanKey, TokenUsage, last_at, span_key
from kelpie.traces.outputs import FILE_TOOLS, TOOL_KEY, call_key, output_tokens
from kelpie.traces.semconv import MAX_VALUE_CHARS

UNKNOWN_ROLE = "unknown"
OFFERED_ATTRIBUTE = "kelpie.tools.offered"
REVIEW_CATEGORIES = ("review",)
REVIEW_READS = (
    *FILE_TOOLS,
    "git diff*",
    "git show*",
    "gh pr diff*",
    "gh pr view*",
    "glab mr diff*",
    "cat*",
)


@dataclass(frozen=True, slots=True)
class FitSettings:
    """Thresholds of these analyses (defaults; see the module docs)."""

    fragment_min_sessions: int = 3
    fragment_min_tokens: int = 500
    fragment_tolerance: float = 0.05
    routine_output_tokens: int = 400
    routine_min_calls: int = 20
    routine_max_tool_tokens: int = 1000  # mean tool output per call
    routine_roles: tuple[str, ...] = ()  # roles whose switch to a cheaper model is priced
    allowed_models: tuple[str, ...] = ()  # candidates besides the models the farm runs
    min_after: timedelta = timedelta(hours=12)  # before that, a switch is too early to tell
    worse_by: int = 2
    review_reads: tuple[str, ...] = REVIEW_READS
    rereview_share: float = 0.5
    min_saving_tokens: int = 50_000  # a recommendation needs at least this a day


@dataclass(frozen=True, slots=True)
class Read:
    """One measured tool call."""

    session_id: str
    task_id: str | None
    role: str
    at: datetime
    key: str  # command prefix or ``tool:<name>``
    args_hash: str | None
    tokens: int


def reads(
    spans: Iterable[Span], roles: Mapping[str, str], tasks: Mapping[str, str] | None = None
) -> list[Read]:
    """Tool calls with a session and a known output size, in time order."""
    tasks = tasks or {}
    out = []
    for span in spans:
        if span.kind is not SpanKind.TOOL or not span.session_id:
            continue
        size = output_tokens(span)
        if size is None:
            continue
        sid = span.session_id
        out.append(
            Read(
                session_id=sid,
                task_id=span.task_id or tasks.get(sid),
                role=roles.get(sid, UNKNOWN_ROLE),
                at=span.start,
                key=call_key(span),
                args_hash=span.args_hash,
                tokens=size[0],
            )
        )
    out.sort(key=lambda r: (r.at, r.session_id, r.key))
    return out


def session_models(
    spans: Iterable[Span], counted: Mapping[SpanKey, str | None]
) -> dict[str, str | None]:
    """The model that read the most input tokens in each session."""
    tally: dict[str, Counter[str]] = defaultdict(Counter)
    for span in spans:
        model = counted.get(span_key(span))
        if span.session_id and model:
            tally[span.session_id][model] += span.input_tokens
    return {s: max(sorted(c), key=lambda m: c[m]) for s, c in tally.items() if c}


# --- repeated fragments ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fragment:
    role: str
    key: str
    args_hash: str
    size: int  # median tokens per read
    reads: tuple[Read, ...]

    @property
    def sessions(self) -> tuple[str, ...]:
        return tuple(sorted({r.session_id for r in self.reads}))

    @property
    def tasks(self) -> tuple[str, ...]:
        return tuple(sorted({r.task_id for r in self.reads if r.task_id}))

    @property
    def tokens(self) -> int:
        return sum(r.tokens for r in self.reads)


def fragments(found: Iterable[Read], settings: FitSettings | None = None) -> list[Fragment]:
    """Content loaded into many sessions of a role, the most tokens first."""
    cfg = settings or FitSettings()
    groups: dict[tuple[str, str, str], list[Read]] = defaultdict(list)
    for r in found:
        if r.args_hash and r.key.startswith(TOOL_KEY) and r.tokens >= cfg.fragment_min_tokens:
            groups[(r.role, r.key, r.args_hash)].append(r)
    out = []
    for (role, key, digest), mine in groups.items():
        size = round(statistics.median(r.tokens for r in mine))
        same = tuple(r for r in mine if abs(r.tokens - size) <= cfg.fragment_tolerance * size)
        if len({r.session_id for r in same}) >= cfg.fragment_min_sessions:
            out.append(Fragment(role, key, digest, size, same))
    out.sort(key=lambda f: (-f.tokens, f.role, f.key, f.args_hash))
    return out


# --- unused tools ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolUse:
    role: str
    used: Mapping[str, int]  # tool -> calls
    offered: tuple[str, ...] | None  # None: not recorded

    @property
    def unused(self) -> tuple[str, ...]:
        return tuple(t for t in self.offered or () if t not in self.used)


def offered_tools(value: object) -> tuple[str, ...]:
    """Tool names in a ``kelpie.tools.offered`` value; a value cut at the attribute limit
    loses its last, possibly partial, name."""
    if not isinstance(value, str):
        return ()
    names = [n.strip() for n in value.split(",")]
    if len(value) >= MAX_VALUE_CHARS and names:
        names = names[:-1]
    return tuple(n for n in names if n)


def tool_use(spans: Iterable[Span], roles: Mapping[str, str]) -> list[ToolUse]:
    """Tools called per role, and the offered tools it never called where they are known."""
    used: dict[str, Counter[str]] = defaultdict(Counter)
    offered: dict[str, set[str]] = defaultdict(set)
    for span in spans:
        if not span.session_id:
            continue
        role = roles.get(span.session_id, UNKNOWN_ROLE)
        if span.kind is SpanKind.TOOL and span.tool:
            used[role][span.tool] += 1
        elif span.kind is SpanKind.LLM:
            used.setdefault(role, Counter())
        names = offered_tools(span.attributes.get(OFFERED_ATTRIBUTE))
        offered[role].update(names)
    return [
        ToolUse(role, dict(used[role]), tuple(sorted(offered[role])) if offered[role] else None)
        for role in sorted(set(used) | set(offered))
    ]


# --- model fit ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoleModel:
    """Counted calls of one role on one model."""

    role: str
    model: str | None
    usage: tuple[TokenUsage, ...]  # per call
    median_output: float

    @property
    def calls(self) -> int:
        return len(self.usage)

    def on(self, model: str) -> tuple[TokenUsage, ...]:
        """The same calls priced as ``model``."""
        return tuple(replace(u, model=model) for u in self.usage)

    @property
    def tasks(self) -> tuple[str, ...]:
        return tuple(sorted({u.task_id for u in self.usage if u.task_id}))


def role_models(
    spans: Iterable[Span], counted: Mapping[SpanKey, str | None], roles: Mapping[str, str]
) -> list[RoleModel]:
    calls: dict[tuple[str, str | None], list[TokenUsage]] = defaultdict(list)
    for span in spans:
        key = span_key(span)
        if key not in counted or not span.session_id:
            continue
        role = roles.get(span.session_id, UNKNOWN_ROLE)
        model = counted[key]
        calls[(role, model)].append(
            TokenUsage(
                span.session_id,
                span.task_id,
                model,
                1,
                span.input_tokens,
                span.output_tokens,
                min(span.cached_tokens, span.input_tokens),
                min(
                    span.cache_write_tokens,
                    span.input_tokens - min(span.cached_tokens, span.input_tokens),
                ),
            )
        )
    out = [
        RoleModel(role, model, tuple(us), statistics.median(u.output_tokens for u in us))
        for (role, model), us in calls.items()
    ]
    out.sort(key=lambda r: (r.role, r.model or ""))
    return out


def routine(
    found: Iterable[RoleModel],
    settings: FitSettings | None = None,
    tool_tokens: Mapping[str, float] | None = None,
) -> list[RoleModel]:
    """Roles doing routine work on a model: many calls, short outputs, small tool outputs
    (``tool_tokens``: mean tool output per call of each role, :func:`tool_tokens`)."""
    cfg = settings or FitSettings()
    tool_tokens = tool_tokens or {}
    return [
        r
        for r in found
        if r.model
        and r.calls >= cfg.routine_min_calls
        and r.median_output <= cfg.routine_output_tokens
        and tool_tokens.get(r.role, 0.0) <= cfg.routine_max_tool_tokens
    ]


def tool_tokens(found: Iterable[Read]) -> dict[str, float]:
    """Mean output tokens per tool call of each role."""
    sizes: dict[str, list[int]] = defaultdict(list)
    for r in found:
        sizes[r.role].append(r.tokens)
    return {role: statistics.fmean(v) for role, v in sizes.items()}


def cheaper_model(prices: PriceTable, model: str | None, candidates: Collection[str]) -> str | None:
    """The cheapest of ``candidates`` (models the farm runs, or allowed ones) with a price
    of their own (a ``[prices]`` entry naming them or the default table, not a glob) that
    is cheaper than ``model`` on both input and output; None when there is none or
    ``model`` has no price."""
    current = prices.price_for(model)
    if current is None:
        return None
    found = [
        (p.input + p.output, name)
        for name in candidates
        if name != model
        and (p := prices.named_price(name)) is not None
        and p.input < current.input
        and p.output <= current.output
    ]
    return min(found)[1] if found else None


@dataclass(frozen=True, slots=True)
class Quality:
    tasks: int  # tasks of the role active in the window
    returns: int
    review_returns: int  # ping-pong rounds

    def per_task(self, n: int) -> float:
        return n / self.tasks if self.tasks else 0.0


@dataclass(frozen=True, slots=True)
class Switch:
    role: str
    old: str
    new: str
    at: datetime  # the first call on the new model after the last one on the old
    before: Quality
    after: Quality
    worse: bool
    early: bool = False  # too little time or too few tasks since the switch to tell

    @property
    def hold(self) -> bool:
        """No further downgrade for the role: worse, or too early to tell."""
        return self.worse or self.early


def model_switches(
    spans: Sequence[Span],
    counted: Mapping[SpanKey, str | None],
    roles: Mapping[str, str],
    tasks: Sequence[Task],
    start: datetime,
    end: datetime,
    settings: FitSettings | None = None,
) -> list[Switch]:
    """Roles whose most used model on the day ``[start, end)`` differs from the day before,
    with returns before and after the switch (see the module docs)."""
    cfg = settings or FitSettings()
    day_before = start - timedelta(days=1)
    earliest = start - timedelta(days=2)  # the window before a switch reaches this far
    calls: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    active: dict[str, list[tuple[str, datetime, datetime]]] = defaultdict(list)
    for span in spans:
        if not span.session_id or not (earliest <= last_at(span) < end):
            continue
        role = roles.get(span.session_id)
        if role is None:
            continue
        if span.task_id:
            active[role].append((span.task_id, span.start, last_at(span)))
        model = counted.get(span_key(span))
        if span.start >= day_before and model:
            calls[role].append((span.start, model))
    by_id = {t.id: t for t in tasks}
    out = []
    for role in sorted(calls):
        mine = sorted(calls[role])
        old = _most_used(m for at, m in mine if at < start)
        new = _most_used(m for at, m in mine if at >= start)
        if old is None or new is None or old == new:
            continue
        last_old = max(at for at, m in mine if m == old)
        later = [at for at, m in mine if m == new and at > last_old]
        at = min(later) if later else min(at for at, m in mine if m == new)
        before = _quality(active[role], by_id, at - max(end - at, timedelta(days=1)), at)
        after = _quality(active[role], by_id, at, end)
        worse = (
            after.returns + after.review_returns
            >= before.returns + before.review_returns + cfg.worse_by
            and after.per_task(after.returns + after.review_returns)
            > before.per_task(before.returns + before.review_returns)
        )
        early = end - at < cfg.min_after or after.tasks < 2
        out.append(Switch(role, old, new, at, before, after, worse, early and not worse))
    return out


def _most_used(models: Iterable[str]) -> str | None:
    tally = Counter(models)
    return max(sorted(tally), key=lambda m: tally[m]) if tally else None


def _quality(
    active: Sequence[tuple[str, datetime, datetime]],
    tasks: Mapping[str, Task],
    a: datetime,
    b: datetime,
) -> Quality:
    ids = {tid for tid, s, e in active if s < b and e >= a}
    returns = review = 0
    for tid in ids:
        task = tasks.get(tid)
        for r in task.returns if task else ():
            if a <= r.at < b:
                if r.category in REVIEW_CATEGORIES:
                    review += 1
                else:
                    returns += 1
    return Quality(len(ids), returns, review)


# --- diff-only review --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewRound:
    task_id: str
    session_id: str
    role: str
    round: int
    at: datetime
    tokens: int  # read tokens of the round (hashed reads)
    reread: int  # tokens already read in an earlier round
    unhashed: int  # reads without an args hash: not comparable
    whole: bool  # re-read the whole PR instead of the changes

    @property
    def share(self) -> float:
        return self.reread / self.tokens if self.tokens else 0.0


def review_rounds(
    found: Sequence[Read],
    review_roles: Sequence[str],
    start: datetime,
    end: datetime,
    settings: FitSettings | None = None,
) -> list[ReviewRound]:
    """Review rounds 2+ that started within ``[start, end)``. ``found`` should reach back
    to the earlier rounds."""
    cfg = settings or FitSettings()
    sessions: dict[str, list[Read]] = defaultdict(list)
    tasks_of: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in found:
        if r.args_hash and r.task_id:
            tasks_of[(r.key, r.args_hash)].add(r.task_id)
        if r.role in review_roles and r.task_id:
            sessions[r.session_id].append(r)
    shared = {k for k, ids in tasks_of.items() if len(ids) > 1}  # not the PR's own files
    by_task: dict[str, list[list[Read]]] = defaultdict(list)
    for mine in sessions.values():
        by_task[mine[0].task_id or ""].append(mine)
    out = []
    for task_id, rounds in sorted(by_task.items()):
        rounds.sort(key=lambda rs: (rs[0].at, rs[0].session_id))
        seen: dict[tuple[str, str], int] = {}
        for n, mine in enumerate(rounds, 1):
            wanted = [r for r in mine if any(fnmatchcase(r.key, g) for g in cfg.review_reads)]
            first: dict[tuple[str, str], int] = {}
            unhashed = 0
            for r in wanted:
                if r.args_hash is None:
                    unhashed += 1
                elif (r.key, r.args_hash) not in shared:
                    first.setdefault((r.key, r.args_hash), r.tokens)
            tokens = sum(first.values())
            reread = sum(min(seen[k], v) for k, v in first.items() if k in seen)
            if n > 1 and start <= mine[0].at < end and (tokens or unhashed):
                out.append(
                    ReviewRound(
                        task_id=task_id,
                        session_id=mine[0].session_id,
                        role=mine[0].role,
                        round=n,
                        at=mine[0].at,
                        tokens=tokens,
                        reread=reread,
                        unhashed=unhashed,
                        whole=reread > 0 and reread >= cfg.rereview_share * tokens,
                    )
                )
            for k, v in first.items():
                seen[k] = max(seen.get(k, 0), v)
    out.sort(key=lambda r: (-r.reread, r.task_id, r.round))
    return out
