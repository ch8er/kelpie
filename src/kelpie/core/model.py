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
    # The machine whose board names this owner (multi-host); None: single host. Owners are
    # only picked for tasks of the same machine.
    host: str | None = None

    def serves(self, task: Task) -> bool:
        """Whether this owner can be picked for ``task`` (same machine, when known)."""
        return self.host is None or task.host is None or self.host == task.host


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


class KelpieMark(StrEnum):
    """A block Kelpie set itself, as the platform adapter verified it from a signed marker
    (agents write block reasons, so the text alone proves nothing)."""

    STOP = "stop"  # Kelpie stopped the run (rule 10): it waits for a human to resume it


@dataclass(frozen=True, slots=True)
class Block:
    reason: str
    since: datetime
    condition_id: str | None = None
    kelpie: KelpieMark | None = None  # set only by an adapter that verified Kelpie's marker


@dataclass(frozen=True, slots=True)
class Return:
    """A task sent back (e.g. from review or merge) with a categorized reason."""

    at: datetime
    reason: str
    category: str | None = None


@dataclass(frozen=True, slots=True)
class Comment:
    """A comment on a task. ``human`` is set by the adapter when a person wrote it (one of
    its human owners, or a person's decision a trusted agent recorded), not an agent or
    Kelpie."""

    author: str
    text: str
    at: datetime
    human: bool = False


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    title: str
    status: Status
    status_since: datetime
    role: str | None = None
    owner_id: str | None = None  # who holds it now (a platform may route it before that)
    source: str | None = None  # name of the platform adapter the task came from
    block: Block | None = None
    review_rounds: int = 0
    estimate: timedelta | None = None
    files: tuple[str, ...] = ()
    # Which repository ``files`` are relative to (an adapter's key, e.g. a git directory);
    # files of different repositories never conflict.
    repo: str | None = None
    labels: tuple[str, ...] = ()
    returns: tuple[Return, ...] = ()
    host: str | None = None  # where the task runs, if the adapter knows (a config name)
    body: str | None = None  # the task's description, if the adapter knows it
    branch: str | None = None  # the task's working branch, if the adapter knows it
    # Whom the platform routed the task to, when that is known apart from ``owner_id``: a
    # platform that assigns work before it is claimed (Hermes) sets the owner only once a
    # run claims it. None: the owner is all there is.
    assignee: str | None = None
    # When the task, once done, was moved back out of done (rule 1's reopened-after-done
    # rate). Kept apart from ``returns``, which are sends-back before done.
    reopened_at: tuple[datetime, ...] = ()
    # Tasks this one was split into (a parent and its subtasks; Hermes: a decomposed root).
    # A parent is done when they are: rule 3 asks to close one left open after them.
    subtasks: tuple[str, ...] = ()
    # In a status before any work starts that only a person moves it out of (Hermes:
    # triage, scheduled; adapter config): rule 3 alerts when one holds up dependents.
    prework: bool = False
    # The platform's own status, before it was normalized into ``status`` (Hermes: triage,
    # todo and scheduled are all ``backlog``), for explanations, metrics and reports.
    platform_status: str | None = None
    # Comments on the task, oldest first (people's kept apart from any cap on the others),
    # the person who wrote ``body`` (None: an agent, or unknown) and when the task was made:
    # rule 1 reads the acceptance criteria people state.
    comments: tuple[Comment, ...] = ()
    body_author: str | None = None
    created_at: datetime | None = None


class ResourceKind(StrEnum):
    DISK = "disk"
    MEMORY = "memory"
    CPU = "cpu"
    LOAD = "load"
    PROCESSES = "processes"
    PATH = "path"  # the size of a directory on a host (what fills a disk)
    LLM_QUOTA = "llm_quota"
    CI_MINUTES = "ci_minutes"
    AUTH = "auth"
    # Tool slots of a pool (simulators) in use: ``name`` "<pool>:<holder>" (agent, ci),
    # ``used`` of ``limit`` slots. For the metrics; no rule reads them.
    SLOTS = "slots"


@dataclass(frozen=True, slots=True)
class Resource:
    """A point-in-time reading of a host resource or an external quota.

    ``used``/``limit`` share ``unit``. ``expires_at`` is set for credentials and
    quota windows; ``resets_at`` for quotas that refill. ``at`` is when the reading was
    taken (None: current as of the snapshot); rules ignore readings that are too old.
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
    at: datetime | None = None
    # How fast ``used`` grows, in ``unit`` per day, over the source's window (negative when
    # it shrinks); None when unknown.
    growth_per_day: float | None = None
    # The mountpoint of the filesystem it is on (disks and directory sizes); None: unknown.
    mount: str | None = None

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
    source: str | None = None  # name of the platform adapter that reported the session
    # A long tool run in progress (a build, a simulator run), if the adapter knows: its name
    # and when it started. Only a long run: rule 9 holds commit reminders from its start
    # (an adapter must not set it for every call; open TOOL spans are every call).
    running_tool: str | None = None
    running_tool_since: datetime | None = None


class SpanKind(StrEnum):
    TASK = "task"
    SESSION = "session"
    LLM = "llm"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class Span:
    """A trace span following OpenTelemetry GenAI semantic conventions.

    ``input_tokens`` includes cached input tokens, as in the conventions;
    ``cached_tokens`` is the part read from a provider cache and ``cache_write_tokens``
    the part written to it.
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
    cache_write_tokens: int = 0

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
    them). ``cost_unknown_tokens`` are tokens of models with no price (neither in
    ``[prices]`` nor in the default table): a ``usd`` budget leaves them out of ``spent``.
    ``session_ids`` are the sessions that spent within the window."""

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
    """A provider usage limit was reached: the run was refused until ``resets_at``.
    ``limit`` names the limit (e.g. a rolling 5-hour or a weekly window; the LLM quota
    connector uses the provider name); forecasts are made per limit."""

    limit: str
    at: datetime
    resets_at: datetime | None = None


class BuildStatus(StrEnum):
    """Normalized CI build (pipeline run) status. Connectors map their CI's states."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILURE = "failure"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"  # a state the connector does not know: never treated as active

    @property
    def is_active(self) -> bool:
        return self in {BuildStatus.QUEUED, BuildStatus.RUNNING}


@dataclass(frozen=True, slots=True)
class Job:
    """One job of a finished CI build. ``conclusion`` is how it ended, mapped by the
    connector like ``Build.status``. ``step`` is the first named step that did not succeed in
    a job that did not succeed, when the CI names one. ``runner`` is the machine a job that
    did not succeed ran on (None for a CI's shared hosted pool, whose names do not identify a
    machine) and ``labels`` the runner labels it asked for (e.g. ``self-hosted``, an OS)."""

    name: str
    conclusion: BuildStatus
    step: str | None = None
    runner: str | None = None
    labels: tuple[str, ...] = ()

    @property
    def failed(self) -> bool:
        return self.conclusion is BuildStatus.FAILURE


@dataclass(frozen=True, slots=True)
class Build:
    """One CI build (a pipeline run for one commit), as a CI connector reports it.

    ``id`` is opaque and unique across connectors; ``source`` names the connector that
    reported it, which is also the executor for actions on it (``cancel_build``).
    ``workflow`` identifies the pipeline within ``repo``. ``draft`` is the current draft
    state of the change request the build belongs to. ``commit_message`` is the first line
    of the head commit's message. ``changed_paths`` are the files the change touches;
    None when unknown. ``minutes`` is what the build has burned so far (billable minutes
    where the CI reports them). ``superseded_by`` is the id of a newer build of the same
    pipeline and branch that started after this one. ``cancellable`` is set by the
    connector: the build is superseded, still active, and within what the connector is
    configured to cancel; rules only ask to cancel such builds. ``jobs`` are the build's
    jobs once it finished (jobs the connector could not read left out; empty: none
    listed). None means not read: an active, queued or skipped build, a partial job list, a
    failed request or no request budget left. None never means "nothing failed".
    """

    id: str
    source: str | None = None
    repo: str | None = None
    workflow: str | None = None
    branch: str | None = None
    sha: str | None = None
    event: str | None = None
    pr: int | None = None  # the number of the change request it builds, when the CI says
    draft: bool = False
    commit_message: str | None = None
    changed_paths: tuple[str, ...] | None = None
    status: BuildStatus = BuildStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    minutes: float = 0.0
    superseded_by: str | None = None
    cancellable: bool = False
    jobs: tuple[Job, ...] | None = None


@dataclass(frozen=True, slots=True)
class LabelEvent:
    """When ``label`` was (last) put on a pull request."""

    label: str
    at: datetime


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A pull (merge) request, as a code-host connector reports it: open
    (``Snapshot.pull_requests``) or recently closed (``Snapshot.closed_pull_requests``,
    ``closed_at`` set, and ``merged_at`` when it was merged).

    ``branch`` is its head branch. ``task_ids`` are the tasks it belongs to.
    ``mergeable`` is True when the host says it can be merged now (no conflicts, required
    checks and reviews satisfied), False when it says it cannot, None when unknown.
    ``labeled`` holds when watched labels (holds) were put on it, where the connector read
    that. ``updated_at`` is the host's last change to the PR.
    """

    repo: str
    number: int
    title: str = ""
    branch: str | None = None
    labels: tuple[str, ...] = ()
    draft: bool = False
    fork: bool = False  # from another repository (its branch names are not ours)
    mergeable: bool | None = None
    head_sha: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    labeled: tuple[LabelEvent, ...] = ()
    source: str | None = None
    # The tasks it belongs to: their code is in its title or head branch (never a mention in
    # its description or comments, incident 17).
    task_ids: tuple[str, ...] = ()
    closed_at: datetime | None = None
    merged_at: datetime | None = None
    # Whether it targets the repository's default branch (False: a stacked PR into another
    # branch); None when unknown.
    into_default: bool | None = None

    @property
    def merged(self) -> bool:
        return self.merged_at is not None

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass(frozen=True, slots=True)
class ProviderStatus:
    """The last probe of an LLM provider (``[connectors.llm_quotas]``), as of ``checked_at``.

    ``ok`` is False when the probe gave no usable reading (unreachable, timed out, an error
    status, an unexpected answer); ``error_kind`` then says why, and the quota fields are
    unknown (None). ``auth_ok`` is False when the provider rejected the credential
    (401/403), True when it accepted it, None when unknown. ``remaining`` and ``limit``
    share ``unit`` and describe the window that refills at ``resets_at``; ``expires_at`` is
    when the credential expires, if the provider reports it. A rate-limited probe (429) is
    ``ok`` with ``remaining`` 0 and ``error_kind`` ``rate_limited``.
    """

    name: str
    ok: bool = True
    auth_ok: bool | None = None
    remaining: float | None = None
    limit: float | None = None
    unit: str = ""
    resets_at: datetime | None = None
    expires_at: datetime | None = None
    checked_at: datetime | None = None
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class OutputOffender:
    """A command (or tool) whose output keeps flooding one role's context: above
    ``[traces.outputs] threshold_tokens`` per call on ``days`` days. Computed from stored
    tool spans when the snapshot is assembled (``kelpie.traces.outputs``), with the filter
    to add to the role's instructions and who owns them (``[traces.outputs] owners``)."""

    role: str | None
    command: str  # a command prefix, or tool:<name>
    filter: str  # name of the recommended filter
    how: str  # what to add to the instructions
    days: int  # days above the threshold
    calls: int  # unfiltered calls in the window
    mean_tokens: float  # per unfiltered call
    saved_tokens: int  # estimate: unfiltered tokens x the filter's expected reduction
    owner: str | None = None  # who owns the role's instructions
    tasks: tuple[str, ...] = ()
    recent_tasks: tuple[str, ...] = ()  # tasks of its unfiltered calls, the latest first


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
    limit_hits: tuple[LimitHit, ...] = ()  # observed provider limits (budgets forecast them)
    providers: tuple[ProviderStatus, ...] = ()  # LLM provider quota and auth probes
    builds: tuple[Build, ...] = ()  # recent CI builds (CI connectors)
    pull_requests: tuple[PullRequest, ...] = ()  # open PRs (code-host connectors)
    # PRs closed (merged or not) within the connector's lookback.
    closed_pull_requests: tuple[PullRequest, ...] = ()
    # True when the code-host connectors' last two scans read the open and recently closed
    # PRs of every repository they watch in full, and nothing moved while they read them:
    # only then does a task with no unmerged PR in the snapshot really have none (a parent is
    # proposed for closing only then). False otherwise; None when no connector lists PRs.
    pull_requests_complete: bool | None = None
    # When the scan that made them complete ran (the oldest, with several code hosts): PRs
    # opened after it are not in the snapshot yet.
    pull_requests_at: datetime | None = None
    # The closed PRs listed are those closed since this time (the connector's lookback; the
    # latest, with several code hosts): one that closed before is not in the snapshot.
    closed_pull_requests_since: datetime | None = None
    # Repeated huge-output offenders over [traces.outputs] repeat_days (span store).
    output_offenders: tuple[OutputOffender, ...] = ()
    # Parts of the farm that are not in this snapshot (multi-host: agents and
    # ``<agent>/<adapter>`` boards that are silent, stale or failed, whose task ids start
    # with ``<agent>:``; ``hub`` when no agent board could be read). A rule that stops
    # reporting a state on such a task, or a farm-wide state, may only be blind to it.
    unobserved: tuple[str, ...] = ()
    # Ids of the alert states open when the engine evaluates (the engine sets this, whatever
    # a source put here): a rule may keep one of its states open on a lower bar than it
    # opens it on, so a reading near the threshold does not flap (hysteresis). Observe-mode
    # states count too: observe shadows what act would do.
    open_states: tuple[str, ...] = ()
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

    def build(self, build_id: str) -> Build | None:
        return next((b for b in self.builds if b.id == build_id), None)

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
