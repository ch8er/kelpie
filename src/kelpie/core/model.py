"""Domain model shared by rules, adapters, connectors and traces.

All entities are immutable. Rules receive a :class:`Snapshot` and must treat it as
read-only input; they never mutate it and never perform I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """Normalized task status. Adapters map platform statuses onto these."""

    BACKLOG = "backlog"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    BLOCKED = "blocked"
    WAITING = "waiting"  # the agent registered a condition and exited; Kelpie wakes it
    ACCEPTED = "accepted"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {Status.DONE, Status.FAILED, Status.CANCELLED}


class OwnerKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


@dataclass(frozen=True, slots=True)
class Owner:
    id: str
    role: str
    kind: OwnerKind = OwnerKind.AGENT


@dataclass(frozen=True, slots=True)
class Dependency:
    """``task_id`` cannot start until ``depends_on`` reaches a terminal status."""

    task_id: str
    depends_on: str


class ConditionKind(StrEnum):
    CI_STATUS = "ci_status"
    PR_MERGED = "pr_merged"
    QUOTA_RESET = "quota_reset"
    HUMAN_TASK = "human_task"
    TASK_STATUS = "task_status"
    TIME = "time"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class Condition:
    """An external fact a task waits for. Connectors evaluate it, rules react to it."""

    id: str
    kind: ConditionKind
    task_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    satisfied: bool | None = None
    # When the condition last became true (reset when it flips back).
    satisfied_at: datetime | None = None
    checked_at: datetime | None = None
    # When the condition was registered. Connectors should set it so that a task waiting
    # again is not woken by a condition left over from its previous wait.
    registered_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Block:
    reason: str
    since: datetime
    condition_id: str | None = None


@dataclass(frozen=True, slots=True)
class Return:
    """A task sent back (e.g. from review or merge) with a categorized reason."""

    at: datetime
    reason: str
    category: str | None = None


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    title: str
    status: Status
    status_since: datetime
    role: str | None = None
    owner_id: str | None = None
    source: str | None = None  # name of the platform adapter the task came from
    block: Block | None = None
    review_rounds: int = 0
    estimate: timedelta | None = None
    files: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    returns: tuple[Return, ...] = ()


class ResourceKind(StrEnum):
    DISK = "disk"
    MEMORY = "memory"
    CPU = "cpu"
    LOAD = "load"
    PROCESSES = "processes"
    LLM_QUOTA = "llm_quota"
    CI_MINUTES = "ci_minutes"
    AUTH = "auth"


@dataclass(frozen=True, slots=True)
class Resource:
    """A point-in-time reading of a host resource or an external quota.

    ``used``/``limit`` share ``unit``. ``expires_at`` is set for credentials and
    quota windows; ``resets_at`` for quotas that refill.
    """

    name: str
    kind: ResourceKind
    host: str | None = None
    used: float | None = None
    limit: float | None = None
    unit: str = ""
    expires_at: datetime | None = None
    resets_at: datetime | None = None
    events: int = 0

    @property
    def utilization(self) -> float | None:
        if self.used is None or not self.limit:
            return None
        return self.used / self.limit


@dataclass(frozen=True, slots=True)
class Artifact:
    """Something a session produced (report, build output, file), with when it appeared."""

    path: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Session:
    """One agent run against one task, from start to exit."""

    id: str
    task_id: str
    owner_id: str
    started_at: datetime
    ended_at: datetime | None = None
    # The status the session declared at its end. Only a real end counts as declared:
    # terminal statuses or a hand-off (review, accepted, blocked, waiting). in_progress,
    # ready or backlog mean the session did not say how it ended.
    final_status: Status | None = None
    exit_reason: str | None = None
    last_heartbeat: datetime | None = None
    last_commit_at: datetime | None = None
    last_commit: str | None = None  # ref (sha) of the last commit on the task branch
    branch: str | None = None
    dirty_worktree: bool = False
    changed_files: int | None = None  # uncommitted changed files, if the adapter knows
    summary: str | None = None  # the agent's own progress note, if it left one
    artifacts: tuple[Artifact, ...] = ()


class SpanKind(StrEnum):
    TASK = "task"
    SESSION = "session"
    LLM = "llm"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class Span:
    """A trace span following OpenTelemetry GenAI semantic conventions.

    ``input_tokens`` includes cached input tokens, as in the conventions;
    ``cached_tokens`` is the part read from a provider cache.
    """

    trace_id: str
    span_id: str
    name: str
    kind: SpanKind
    start: datetime
    end: datetime | None = None
    parent_id: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    model: str | None = None
    tool: str | None = None
    args_hash: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    outcome: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> timedelta | None:
        return None if self.end is None else self.end - self.start


@dataclass(frozen=True, slots=True)
class SpanTotals:
    """Per-session aggregates over stored spans, so rules and the report can see tokens and
    call counts over a long window without loading every raw span.

    Tokens and ``llm_calls`` count root LLM spans: an LLM span with an LLM ancestor in the
    same trace (a second instrumentation of the same call) is left out. ``input_tokens``
    follows the GenAI conventions and includes cached tokens (``cached_tokens`` is the
    part served from a cache); OTLP ingest normalizes sources that report it the other
    way (Claude Code).
    """

    session_id: str
    task_id: str | None = None
    spans: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    first_start: datetime | None = None
    last_end: datetime | None = None


class BudgetScope(StrEnum):
    DAY = "day"  # the whole farm
    TASK = "task"
    AGENT = "agent"
    PROJECT = "project"


class BudgetUnit(StrEnum):
    USD = "usd"
    TOKENS = "tokens"  # input (cached included) + output


class BudgetState(StrEnum):
    OK = "ok"
    SOFT = "soft"  # soft threshold reached: alert
    HARD = "hard"  # hard threshold reached: stop (act mode)


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    """Spend of one subject against one budget at snapshot time (``kelpie.core.budgets``
    computes it). ``remaining`` and ``runs_out_at`` are measured against ``hard``, or
    ``soft`` when the budget has no hard threshold; ``remaining`` never goes below 0.
    ``burn_rate`` is ``unit`` per hour over the budget's recent ``burn_window``;
    ``runs_out_at`` is when the remaining budget runs out at that rate: ``now`` once it
    has, None when nothing is being spent. ``estimated`` is set in subscription mode,
    where there is no bill and the spend is priced from traces; ``next_limit_at`` is then
    the forecast next provider limit from observed limit hits (None without enough of
    them). ``cost_unknown_tokens`` are tokens of models with no price in ``[prices]``: a
    ``usd`` budget leaves them out of ``spent``. ``session_ids`` are the sessions that
    spent within the window."""

    budget: str
    scope: BudgetScope
    subject: str  # task id, agent (owner) id, the project's match, or "farm" for a day
    unit: BudgetUnit
    spent: float
    state: BudgetState = BudgetState.OK
    soft: float | None = None
    hard: float | None = None
    remaining: float = 0.0
    burn_rate: float = 0.0
    runs_out_at: datetime | None = None
    next_limit_at: datetime | None = None
    window: timedelta = timedelta(hours=24)
    cost_unknown_tokens: int = 0
    estimated: bool = False
    session_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class LimitHit:
    """A provider usage limit was reached (subscription mode): the run was refused until
    ``resets_at``. ``limit`` names the limit (e.g. a rolling 5-hour or a weekly window);
    forecasts are made per limit. Connectors fill these; none does yet."""

    limit: str
    at: datetime
    resets_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything a rule may look at, captured at ``now``."""

    now: datetime
    tasks: tuple[Task, ...] = ()
    owners: tuple[Owner, ...] = ()
    dependencies: tuple[Dependency, ...] = ()
    conditions: tuple[Condition, ...] = ()
    resources: tuple[Resource, ...] = ()
    sessions: tuple[Session, ...] = ()
    spans: tuple[Span, ...] = ()
    # Aggregates over a longer window than ``spans`` (see ``[traces] totals_window``).
    span_totals: tuple[SpanTotals, ...] = ()
    # Budget state per budget and subject ([[budgets]]), computed when the snapshot is
    # assembled; rules read it instead of pricing spans themselves.
    budgets: tuple[BudgetStatus, ...] = ()
    # False when spend could not be read this tick: ``budgets`` then holds the last good
    # statuses (stale), so a rule should neither resolve nor raise budget alerts on them.
    budgets_ok: bool = True
    limit_hits: tuple[LimitHit, ...] = ()  # subscription mode: observed provider limits
    _spans_by_session: dict[str, tuple[Span, ...]] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def task(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def owner(self, owner_id: str) -> Owner | None:
        return next((o for o in self.owners if o.id == owner_id), None)

    def condition(self, condition_id: str) -> Condition | None:
        return next((c for c in self.conditions if c.id == condition_id), None)

    def dependents(self, task_id: str) -> tuple[Task, ...]:
        ids = {d.task_id for d in self.dependencies if d.depends_on == task_id}
        return tuple(t for t in self.tasks if t.id in ids)

    def sessions_for(self, task_id: str) -> tuple[Session, ...]:
        return tuple(s for s in self.sessions if s.task_id == task_id)

    def spans_for_session(self, session_id: str) -> tuple[Span, ...]:
        # Indexed on first use: rules ask per session, and a snapshot can hold many spans.
        # The cache is derived data, not a change to the snapshot.
        index = self._spans_by_session
        if index is None:
            grouped: dict[str, list[Span]] = {}
            for span in self.spans:
                if span.session_id is not None:
                    grouped.setdefault(span.session_id, []).append(span)
            index = {k: tuple(v) for k, v in grouped.items()}
            object.__setattr__(self, "_spans_by_session", index)
        return index.get(session_id, ())
