# Kelpie roadmap (issue list)

This file is the source of truth for the GitHub issues created by
[`scripts/create_issues.sh`](../scripts/create_issues.sh). Scope, priorities and order of
work come from [plan.md](plan.md), which is the single source of truth: if this list
disagrees with the plan, the plan wins and this file gets fixed.

**Format (machine-parsed).** Each issue starts with a level-3 heading holding the title,
immediately followed by a `labels:` line with comma-separated labels. Everything after that,
up to the next level-2 or level-3 heading, is the issue body (Markdown). Level-2 headings
(`## Week 1`, ...) are section markers and are ignored. Do not use level-2 or level-3
headings inside an issue body; use bold text instead. Titles must be unique: the script
skips issues whose title already exists in the repository.

**One task — one branch, one PR.** Each issue below is sized for a single PR. If an issue
turns out bigger, split it before starting.

Already delivered by the bootstrap PR and therefore not listed: the incident fixture
loader and harness (`tests/incidents.py`), the import-boundary check (import-linter and
`tests/test_architecture.py`), the docker compose stack with CI (the `ci-ok` aggregator job
is the only required check), `CLAUDE.md`, and the clean-context `reviewer` agent
(`.claude/agents/reviewer.md`). Delivered by the plan-field-evidence PR (#2): the optional
adapter capability `SupportsStop` with `ActionKind.STOP` and engine dispatch (alert fallback
when an adapter lacks it), `Span.args_hash`, `Session.artifacts` and the fixture-only
`repeat` key.

**Fixtures.** Fixture names follow `NN-slug.yaml`; several fixtures per incident are
allowed (same number, different slug). Foundation rules without an incident number, such as
`checkpoints`, use `incident: null` and the next free number. Scenarios taken from field
evidence on other farms (see the plan) use `field-NN-slug.yaml` with `incident: null`. The
format, including the fixture-only `repeat` key for long runs of identical spans, is
documented in [`tests/fixtures/incidents/README.md`](../tests/fixtures/incidents/README.md).
Fixtures listed as "new" below do not exist yet and are part of the issue.

**Labels.**
- Priority, as in the plan: `P0` (now, this week), `P1` (next step), `P2` (later, on signal).
- Schedule: `week-1` (Oct 15–21), `week-2` (Oct 22–28), `week-3` (Oct 29 – Nov 4). Issues
  in the P1 backlog (week 3 only if the budget allows) and P2 issues have no week. Only P0
  issues and the P1 items the plan explicitly lists in week 3 carry a week label.
- Area: `area:core|rules|adapters|traces|connectors|report|resources|deploy|docs|project`
  (`area:project` is non-code work: validation, naming, business).
- `adapter`: one issue per additional platform adapter (all P2, no week; Hermes is the
  first adapter and is scheduled in week 1).

**Rule names** (`@register` names, also used in fixtures): `exit_contract` (rule 1),
`wait_condition` (2), `orphan_status` (3), `stale_blocks` (4), `ping_pong` (5),
`bottlenecks` (6), `resources` (7), `ci_economy` (9), `loop_breaker` (10), `no_progress`
(11), `conflict_warning` (12, P1), `needs_you` (13, P1), `compaction_gate` (14, P2),
`return_causes` (incident 7), `checkpoints` (foundation). Rule 8 is the daily report in
`report/`. P1 extensions of rules 1 and 3 (evidence for "done", stop patterns) live in
`exit_contract` and `orphan_status`.

## Week 1

### core: action ledger — fingerprints, cooldown and per-task rate limit
labels: P0, week-1, area:core
Every action Kelpie emits gets a fingerprint and is recorded in a ledger. The ledger stops
Kelpie from repeating itself: the same action is not re-emitted before a cooldown, and the
number of actions per task per hour is capped, so Kelpie cannot loop on its own output. The
same records power alert deduplication and the before/after report.

**Acceptance criteria**
- [ ] Fingerprint = rule, kind, target and the params that matter; identical actions within the cooldown (config) are suppressed and counted
- [ ] Max actions per task per hour (config); excess actions are dropped and reported once as an alert
- [ ] Ledger persisted through the storage protocol (an in-memory implementation until the SQLite storage issue lands); survives a restart
- [ ] The alert pipeline and the before/after report read the ledger instead of keeping their own history
- [ ] Unit tests with a fake clock

### core: action routing — task source, executor targets, executor registry
labels: P0, week-1, area:core
The engine dispatches to one adapter today. Kelpie needs several platforms and connectors
that can act (a CI connector cancelling a build), so actions are routed to executors.

**Acceptance criteria**
- [ ] `Task.source` names the adapter the task came from
- [ ] `Action` carries an executor target: the task's platform adapter by default, or a named connector
- [ ] The engine holds a registry of executors (platform adapters and connectors) instead of a single adapter; unknown target or unsupported action → alert, never a crash
- [ ] Connectors can expose actions, e.g. `cancel_build` executed by a CI connector (used by `ci_economy`)
- [ ] Optional capabilities (`SupportsReassign`, `SupportsStop`) keep their alert fallback per executor
- [ ] `kelpie.core` still imports no adapter or connector (import-linter passes)

### core: per-rule mode — observe, suggest, act
labels: P0, week-1, area:core
Rules are enabled one by one on a real farm. Each rule runs in one of three modes, set per
rule in config: `observe` (record what it would do, nothing sent), `suggest` (send the
action as an alert or comment for a human to apply) or `act` (execute it). The default is
`observe`; the global dry-run flag overrides every rule.

**Acceptance criteria**
- [ ] Per-rule `mode: observe | suggest | act` in config, default `observe`
- [ ] Global dry-run forces every rule to `observe` regardless of its mode
- [ ] The mode is recorded with every action in the action ledger
- [ ] Unit tests cover each mode and the global override

### deploy: cloud session setup script
labels: P0, week-1, area:deploy
Cloud sessions must start working fast. A setup script installs Python 3.12, uv and all
project dependencies so that ruff, import-linter and pytest can run right away.

**Acceptance criteria**
- [ ] A fresh cloud session is ready to run the local checks from `CLAUDE.md` in under about 5 minutes
- [ ] Script is idempotent and safe to re-run
- [ ] Documented in `CONTRIBUTING.md`

### core: engine loop with configurable interval and dry-run
labels: P0, week-1, area:core
Run `Engine.tick` every N seconds (config, default 30 s) with a snapshot provider assembled
from adapters, connectors, resources and traces. Dry-run is the default global override;
per-rule behaviour comes from the per-rule mode issue, dispatch from the action routing issue.

**Acceptance criteria**
- [ ] Loop interval and the global dry-run override come from config (per-rule `mode` as in "per-rule mode")
- [ ] A failing rule is logged and skipped; it never stops the loop
- [ ] Each tick records duration and number of actions per rule
- [ ] Unit tests use a fake clock and a fake adapter; no sleeps in tests

### core: SQLite storage for snapshots, sessions and actions
labels: P0, week-1, area:core
Persist what Kelpie saw and did so that the report and before/after comparisons have data.
SQLite by default; keep the schema DuckDB-compatible for analytics.

**Acceptance criteria**
- [ ] Tables for tasks (status history), sessions and conditions (the action ledger table and versioned migrations landed with engine item A)
- [ ] Retention for raw action-ledger rows: 30 days by default plus a size cap, both configurable; the in-memory ledger store is bounded too
- [ ] Daily aggregates kept indefinitely (per rule, action kind and mode; executed / suppressed / failed counts), because rule 8's before/after verification needs history; aggregates are written before raw rows are pruned
- [ ] Pruning runs inside `kelpie serve` on a schedule and never blocks action dispatch (separate thread or short batched transactions; WAL)
- [ ] The trace store will follow the same policy shape (raw rows with age + size limits, aggregates kept); note only, not built here
- [ ] Storage lives behind a protocol in `kelpie.core`; no adapter imports
- [ ] Tests run against a temporary database file

### core: config file and `kelpie serve` CLI
labels: P0, week-1, area:core
A single YAML config (adapters, connectors, rules and thresholds, alerting, Prometheus URL)
and a `kelpie` CLI with `serve`, `check-config` and `run-once --dry-run`.

**Acceptance criteria**
- [ ] `uv run kelpie serve` starts the loop with the configured adapter
- [ ] `kelpie run-once --dry-run` prints the actions for a single snapshot and exits
- [ ] Secrets can be supplied via environment variables, never only in the file
- [ ] Invalid config fails fast with a readable error

### core: close model gaps found while writing incident fixtures
labels: P0, week-1, area:core
Writing the 13 incident fixtures forced several facts into free text or `params`. Promote
the ones rules actually need to typed fields before implementing those rules.

**Acceptance criteria**
- [ ] `Condition` can be shared by several tasks and references what it waits on (task, person, PR)
- [ ] `Return` records its cause category and conflicting files; `Task` can reference repo/PR
- [ ] `Session` and heavy tasks can be tied to a host; `Owner` exposes availability/capacity
- [ ] `Block` has a category so allowed-reason checks do not parse free text
- [ ] `Resource` has `sampled_at` and a window for event counts (e.g. hits per day)
- [ ] Task status history is available to rules (time-to-accept, bounce count)
- [ ] CI build/run records in the snapshot, supplied by the CI connector (fixture 05 today only
      has the `ci_minutes` quota resource): commit sha, branch, trigger event, draft flag,
      changed paths, duration/minutes, status, `superseded_by`. Field names stay CI-agnostic
- [ ] Fixtures in `tests/fixtures/incidents/` migrated to the new fields

### core: alert pipeline with actionable-only policy and duplicate merging
labels: P0, week-1, area:core
Real-time alerts must be worth reading. Every alert is actionable and states what Kelpie
already did by itself; duplicates are merged instead of repeated.

**Acceptance criteria**
- [ ] Alert payload carries severity, the affected tasks/hosts, the action Kelpie already took (or "dry-run: would …") and what a human should do
- [ ] Alerts with nothing for a human to do are not sent in real time (they go to the daily report)
- [ ] Identical or overlapping alerts within a configurable window are merged into one with a count, using the action ledger's fingerprints and cooldown (no separate dedup store)
- [ ] Untrusted task text is escaped before it reaches any alerter

### core: Slack alerter
labels: P0, week-1, area:core
Implement the `Alerter` protocol for Slack incoming webhooks on top of the alert pipeline.

**Acceptance criteria**
- [ ] Severity is reflected in the message (info / warning / critical)
- [ ] Message shows what Kelpie already did and what needs a human
- [ ] Tests mock HTTP; no network access

### core: condition registration API
labels: P0, week-1, area:core
HTTP endpoint (and a helper in the Hermes plugin) for an agent to register a condition for
its task before exiting. Needed by the wait-for-condition rule.

**Acceptance criteria**
- [ ] `POST /conditions` with kind, params and task id
- [ ] Authentication required
- [ ] Duplicate registrations are idempotent
- [ ] Registering a condition puts the task into a "waiting" state visible to rules

### traces: OTLP ingest with GenAI semantic conventions
labels: P0, week-1, area:traces
Accept OTLP/HTTP spans and map them onto `Span` (task → session → model/tool call) following
the OpenTelemetry GenAI semantic conventions: model, input/output/cached tokens, duration,
outcome. Hermes sends traces through its Langfuse plugin; Claude Code exports telemetry to
OpenTelemetry.

**Acceptance criteria**
- [ ] OTLP/HTTP protobuf and JSON accepted
- [ ] `gen_ai.*` attributes mapped to `Span` fields; unknown attributes kept in `attributes`
- [ ] Tool spans carry `args_hash`, a stable hash of the normalized tool arguments (never the raw arguments), for loop detection (rule 10)
- [ ] Spans linked to task and session ids
- [ ] Ingest endpoint can require a bearer token

### traces: span storage and query helpers
labels: P0, week-1, area:traces
Store spans locally (SQLite or DuckDB) and expose per-session and per-task aggregates
(tokens, duration, tool calls) for rules and the report.

**Acceptance criteria**
- [ ] Spans persisted with a retention setting
- [ ] Aggregates available in the snapshot without loading every raw span
- [ ] Tests with synthetic traces

### traces: optional forwarding to Langfuse, Phoenix or Datadog
labels: P0, week-1, area:traces
Forward ingested spans over OTLP to the tracing backend a team already uses. Traces open in
Langfuse from the daily report and dashboards.

**Acceptance criteria**
- [ ] Disabled unless configured; one or more OTLP destinations with headers from env vars
- [ ] Langfuse covered by a documented example; Phoenix and Datadog by config examples
- [ ] Forwarding failures never block or slow down ingest (bounded queue, drop with a counter)

### traces: spans from adapter hooks when a platform emits none
labels: P0, week-1, area:traces
Some platforms do not emit OpenTelemetry. Give adapters a small helper that turns hook
events (session start/end, tool call) into GenAI-convention spans fed into the same storage.

**Acceptance criteria**
- [ ] Helper in `kelpie.traces` builds task → session → tool spans from hook events, with the same `args_hash` as OTLP ingest
- [ ] Spans from hooks and from OTLP are not double-counted when both exist
- [ ] Tests with synthetic hook events

### rules: checkpoints — commit reminder and WIP commit on exit
labels: P0, week-1, area:rules
Reduce lost work when sessions die. If more than N minutes passed since the last commit, or
more than M files changed, remind the agent to commit. If the session exits without
committing, make a WIP commit to the task branch and write a progress note. WIP commits must
not trigger CI.

**Acceptance criteria**
- [ ] `comment` reminder when `last_commit_at` is older than N minutes or changed files exceed M (both in config)
- [ ] `wip_commit` with a progress note when a session ends with a dirty worktree
- [ ] WIP commits go to a draft PR or carry `[skip ci]` (configurable)
- [ ] New fixtures with `rule: checkpoints` and `incident: null`: `14-checkpoint-commit-reminder.yaml`,
      `15-checkpoint-wip-commit-on-exit.yaml`

### rules: checkpoints — resume from the last commit
labels: P0, week-1, area:rules
A restarted task continues from the last commit instead of starting from zero.

**Acceptance criteria**
- [ ] Requeued tasks carry the last commit reference, the progress note and a short git log
- [ ] The adapter passes them into the next session's prompt
- [ ] Restarts from zero are counted (input for the daily report and token optimization)
- [ ] New fixture `16-checkpoint-resume-from-commit.yaml` (`rule: checkpoints`, `incident: null`)

### adapters: Hermes adapter (read side)
labels: P0, week-1, area:adapters
Read tasks, owners, dependencies and blocks from Hermes Kanban (`~/.hermes/kanban.db`) and
map statuses onto `kelpie.core.model.Status`.

**Acceptance criteria**
- [ ] `list_tasks` and `get_task` implemented
- [ ] Status mapping documented in the adapter module
- [ ] Database opened read-only for reads
- [ ] Tests against a synthetic `kanban.db` built in the test

### adapters: Hermes adapter (actions)
labels: P0, week-1, area:adapters
Implement `requeue`, `unblock`, `comment`, `wake`, `reassign` and `wip_commit` for Hermes.

**Acceptance criteria**
- [ ] Every action leaves a comment on the task explaining what Kelpie did and why
- [ ] Actions are idempotent (re-running the same action is a no-op)
- [ ] Concurrent writes with Hermes handled safely (transactions, retry on lock)

### adapters: Hermes plugin for post_tool_call and on_session_end
labels: P0, week-1, area:adapters
A Hermes plugin that runs on the agent host and sends session events and spans to Kelpie.

**Acceptance criteria**
- [ ] `on_session_end` sends final status, exit reason and worktree state
- [ ] `post_tool_call` sends heartbeat and tool span (via the hooks-to-spans helper)
- [ ] Plugin never blocks the agent if Kelpie is unreachable (buffer and drop)
- [ ] Install instructions in docs

### rules: exit_contract (rule 1, incident 2)
labels: P0, week-1, area:rules
A session ended without a final status, or set a block whose reason is not in the allowed
list. The task goes back to the queue with an explanatory comment.

**Acceptance criteria**
- [ ] Session ended with `final_status` empty → `requeue`
- [ ] Block reason not in the configured allowed list → `requeue`
- [ ] Healthy exits (final status, allowed block) produce no action
- [ ] Fixtures: existing `02-exit-without-status.yaml` (covers both requeue cases); new `02-healthy-exit.yaml`

### rules: wait_condition — wake on condition and "waiting" heartbeat (rule 2, incident 10)
labels: P0, week-1, area:rules
Instead of waiting inside a session, an agent registers a condition (CI status, PR merged,
quota reset, human task, time) through any connector and exits. Kelpie wakes the task when
the condition holds. While the task waits, Kelpie keeps a "waiting" heartbeat so the
dispatcher does not declare the agent dead.

**Acceptance criteria**
- [ ] Satisfied condition on a waiting task → `wake`
- [ ] Waiting tasks get a "waiting" heartbeat; they are not requeued or flagged as dead
- [ ] A condition that stays unsatisfied past a configurable deadline → `alert`
- [ ] Fixtures: new `10-condition-satisfied-wake.yaml`, `10-waiting-heartbeat.yaml`

### rules: wait_condition — detect in-session polling (rule 2, incident 10)
labels: P0, week-1, area:rules
Detect agents that wait inside their session (spans are repeated checks of the same thing)
and tell them to register a condition and exit. Defaults reuse the AgentWatchdog
thresholds: 8 repeats in 10 minutes, 5 errors in a row.

**Acceptance criteria**
- [ ] Repeated identical checks (default 8 in 10 minutes) or consecutive errors (default 5) → `comment` suggesting to register a condition and exit
- [ ] A long but productive session is not flagged
- [ ] Polled commands map to condition kinds via config (`poll_patterns`), never hard-coded CI commands
- [ ] Repeated-call detection lives in one helper in `kelpie.rules`, reused later by `loop_breaker` (rule 10)
- [ ] Fixtures: existing `10-reviewer-polls-ci.yaml`; new `10-productive-long-session.yaml`

### rules: orphan_status (rule 3, incident 11)
labels: P0, week-1, area:rules
A task stays in a non-terminal status with no owner for more than N minutes. Alert and
reassign it to an owner of the task's role (the default; reassignment can be switched off
in config). Severity rises with the number of dependents.

**Acceptance criteria**
- [ ] Ownerless task beyond threshold → `alert` and `reassign` (unless disabled in config)
- [ ] Severity is `critical` when dependents exceed a threshold
- [ ] Tasks below the threshold or in terminal statuses are ignored
- [ ] Fixtures: existing `11-orphan-accepted-task.yaml`; new `11-below-threshold.yaml`

### rules: stale_blocks (rule 4, incidents 1, 8)
labels: P0, week-1, area:rules
A task is blocked on a cause that is already fixed in the outside world (guard condition
met, PR merged). Check the block reason against external state and clear it.

**Acceptance criteria**
- [ ] Block linked to a satisfied condition → `unblock` with the condition in the reason
- [ ] Block with an unsatisfied or unknown condition → no action
- [ ] Grace period configurable to avoid racing the agent
- [ ] Fixtures: existing `01-stale-guard-block.yaml`, `08-block-outlived-cause.yaml`

## Week 2

### rules: ping_pong (rule 5, incident 12)
labels: P0, week-2, area:rules
After K review rounds on one task, stop the loop and escalate to the architect or a human
with the history of return reasons.

**Acceptance criteria**
- [ ] `review_rounds >= K` → `escalate` once (not every tick)
- [ ] Escalation target (architect role or human) comes from config
- [ ] Escalation message lists return reasons and categories
- [ ] Fixtures: existing `12-review-ping-pong.yaml`; new `12-below-threshold.yaml`

### rules: bottlenecks — role utilization and critical path (rule 6, incidents 9, 10)
labels: P0, week-2, area:rules
Compute idle time and queue depth per role and the critical path of the task graph. When a
sequential graph keeps agents idle, or one role (e.g. reviewers) becomes the bottleneck,
alert with a rebalancing suggestion.

**Acceptance criteria**
- [ ] Idle time and queue depth per role computed from sessions and task statuses
- [ ] Long sequential chains with idle agents → `alert` suggesting parallelization, with `critical_path_length`
- [ ] Overloaded role with idle roles elsewhere → `alert` with a rebalancing suggestion
- [ ] Fixtures: existing `09-sequential-chain-idle.yaml`; new `10-review-queue-bottleneck.yaml`

### rules: loop_breaker — identical calls, error streaks and token budget (rule 10, field evidence)
labels: P0, week-2, area:rules
Field evidence: a heartbeat/maintenance loop repeated the same tool call about 900 times
overnight, each time resending about 80k input tokens. Single-session guards exist only for
some platforms (AgentWatchdog covers Claude Code only; OpenClaw closed the request as "not
planned"). Kelpie's breaker is built on traces, so it works for any platform that sends
them (OTLP or spans written by an adapter from hooks). It stops the run and tells a human.

**Acceptance criteria**
- [ ] N identical consecutive tool calls in a session (same `tool` and same `Span.args_hash`; default 8 within 10 minutes) → `stop` (with `session_id`) + critical `alert`
- [ ] Precedence with rule 2: calls matching `wait_condition`'s `poll_patterns` are left to rule 2 (a coaching `comment`); the run is stopped only if polling continues after that comment for a grace period or passes a higher hard limit
- [ ] 5 consecutive tool errors in a session → `stop` + critical `alert`
- [ ] Per-agent daily and nightly token budget exceeded (budgets and window boundaries in config) → `stop` + critical `alert` with the tokens spent
- [ ] Defaults are the AgentWatchdog thresholds (8 repeats in 10 minutes, 5 errors in a row); every threshold can be overridden per agent or role
- [ ] Reuses the repeated-call helper from the `wait_condition` polling check; reads only spans and sessions, no platform-specific code
- [ ] The same tool with different arguments, a varied long session and an agent under budget are not flagged
- [ ] Fixtures: existing `field-01-heartbeat-runaway.yaml` (about 900 identical heartbeat calls with about 80k input tokens each, plus 5 consecutive tool errors; a healthy agent and a CI-polling reviewer left to rule 2 as distractors)

### rules: no_progress — tokens spent without progress (rule 11, field evidence)
labels: P0, week-2, area:rules
Field evidence: idle agents keep spending tokens without moving any task, e.g. resending a
12k-token context every 5 minutes for hours with no status change, commit or artifact.
Alert with concrete recommendations; stopping runaway loops is rule 10's job.

**Acceptance criteria**
- [ ] Per session over a configurable window: tokens spent vs. progress signals (task status change, `last_commit_at`, `Session.artifacts`); tokens above a threshold with no progress → `alert`
- [ ] Alert carries the tokens burned and `params.recommendations`: `longer_heartbeat_interval`, `cheaper_maintenance_model`, `bounded_session_handoff` (bounded sessions with a handoff file)
- [ ] One alert per session per window, not one per tick
- [ ] A productive session (status change, commit or artifact in the window) is not flagged
- [ ] Fixtures: existing `field-02-idle-context-resend.yaml` (a productive session as the distractor)

### adapters: Hermes adapter — stop a run (`SupportsStop`)
labels: P0, week-2, area:adapters
`loop_breaker` stops runaway runs with the `stop` action. Stopping is an optional adapter
capability (`SupportsStop` in `kelpie.core.ports`); the capability, `ActionKind.STOP` and the
engine dispatch with an alert fallback for adapters without it were delivered in #2. This
issue implements it for Hermes.

**Acceptance criteria**
- [ ] Hermes adapter implements `SupportsStop`: the running session stops and the task is not picked up again until a human resumes it; the mechanism is documented in the adapter module
- [ ] Stopping leaves a comment on the task with the reason and how to resume
- [ ] Idempotent: stopping an already stopped run is a no-op
- [ ] Engine dispatch for `stop` reaches the Hermes adapter (test with a synthetic `kanban.db`)

### rules: resources — disk, memory, processes and heavy-task concurrency (rule 7, incidents 3, 4)
labels: P0, week-2, area:rules
Warn before host resources run out and the agents stop, and limit concurrency of heavy
tasks on an overloaded host.

**Acceptance criteria**
- [ ] Utilization thresholds per resource kind (warning / critical) for disk, memory, load, processes
- [ ] Overloaded host with a heavy-task queue → `alert` with a suggested heavy-task concurrency limit (`max_heavy`)
- [ ] Healthy hosts and light tasks produce no action
- [ ] Fixtures: existing `03-disk-full.yaml`, `04-host-overload-heavy-queue.yaml`; new `03-disk-below-threshold.yaml`

### rules: resources — LLM provider quotas and auth health (rule 7, incident 6)
labels: P0, week-2, area:rules
Provider limits and expired logins stopped agents up to 200 times a day. Alert before
quotas run out or logins expire, and suggest a concurrency level tuned to the provider limit.

**Acceptance criteria**
- [ ] Repeated limit hits aggregated into one alert with counts per provider
- [ ] Suggested concurrency per provider derived from the observed limit
- [ ] Credentials expired or expiring within a window → `alert` (critical when expired)
- [ ] Fixtures: existing `06-llm-quota-and-expired-auth.yaml`

### rules: ci_economy — wasted builds, cancel superseded, quota forecast (rule 9, incident 5)
labels: P0, week-2, area:rules
CI minutes ran out and tasks waited about 11 hours. Find builds that burn minutes for
nothing: on drafts and WIP commits, superseded by a newer push, triggered by docs-only
changes, or repeated for an already verified commit. Cancel superseded builds and forecast
how long the quota will last. The rule stays CI-agnostic; CI specifics live in a connector.

**Acceptance criteria**
- [ ] Depends on CI build/run records in the snapshot (see "close model gaps"): commit sha, branch, trigger event, draft flag, changed paths, duration/minutes, status, `superseded_by`
- [ ] Each wasted build is classified (draft/WIP, superseded, docs-only, already verified) with the minutes it burned
- [ ] Superseded running builds → `cancel_build`, executed by the CI connector (not the platform adapter) through action routing (see "action routing")
- [ ] Quota burn forecast (time until exhaustion at the current rate) → `alert` before it runs out; exhausted quota → `critical` with `blocked_tasks`
- [ ] Fixtures: existing `05-ci-minutes-exhausted.yaml`; new `05-superseded-builds.yaml`, `05-docs-only-build.yaml`

### rules: ci_economy — propose a better CI configuration (rule 9, incident 5)
labels: P0, week-2, area:rules
Turn the wasted-build statistics into a concrete fix: a proposed pull request with a
better CI configuration (skip drafts, path filters for docs-only changes, cancel in
progress on new pushes, skip already verified commits).

**Acceptance criteria**
- [ ] Recommendation lists each suggested change with the minutes it would have saved
- [ ] The CI connector renders the change for its CI system and opens the PR (dry-run: prints the diff)
- [ ] No CI-specific syntax in `kelpie.rules`

### resources: read host metrics via Prometheus HTTP API
labels: P0, week-2, area:resources
No custom collector: query node_exporter metrics through the Prometheus HTTP API and turn
them into `Resource` readings per host. Works the same for a laptop, a cloud VM or a container.

**Acceptance criteria**
- [ ] Disk, memory, load and process count per host
- [ ] PromQL queries configurable
- [ ] Prometheus unavailability degrades gracefully (rule sees no data, alert once)

### connectors: GitHub PR and CI
labels: P0, week-2, area:connectors
Evaluate `ci_status` and `pr_merged` conditions against GitHub, and supply CI data for
`ci_economy`: build/run records, remaining Actions minutes as a resource, and cancelling a
superseded run.

**Acceptance criteria**
- [ ] Conditions updated with `satisfied`, `satisfied_at`, `checked_at`
- [ ] Build/run records mapped to the CI-agnostic model fields
- [ ] Exposes the `cancel_build` action as an executor (see "action routing"); only with a token that allows it, read-only otherwise
- [ ] Respects rate limits with caching / conditional requests; tests with mocked HTTP

### connectors: LLM provider quotas and auth health
labels: P0, week-2, area:connectors
Read rate-limit and quota state and credential expiry for configured LLM providers, from
provider APIs where available and from trace errors otherwise.

**Acceptance criteria**
- [ ] `quota_reset` conditions evaluated
- [ ] Limit hits from spans counted per provider
- [ ] Credential expiry exposed as a resource
- [ ] No provider SDK is required by core

### connectors: human tasks
labels: P0, week-2, area:connectors
Evaluate `human_task` conditions (task assigned to a human owner reached a terminal status),
so agent tasks can wait on humans without polling.

**Acceptance criteria**
- [ ] Works with human-owned tasks on the same board via the adapter
- [ ] Satisfied condition wakes the dependent agent task through the `wait_condition` rule

### report: Kelpie metrics on /metrics and a Grafana dashboard
labels: P0, week-2, area:report
No custom UI in the MVP. Kelpie exposes Prometheus metrics, Grafana shows them, and traces
open in Langfuse.

**Acceptance criteria**
- [ ] `/metrics` in Prometheus text format: tick duration, actions per rule, tasks per status, idle time per role
- [ ] Grafana dashboard JSON provisioned by docker compose
- [ ] Dashboard links to traces in Langfuse when configured

### deploy: run on our own farm and record every rule trigger
labels: P0, week-2, area:deploy
Run Kelpie against the real Hermes farm: every rule in `observe` mode first, then `suggest`
and `act` rule by rule. Record every rule trigger so each one can be checked by hand.

**Acceptance criteria**
- [ ] 24 h in `observe` mode with every rule trigger logged and reviewed
- [ ] Rules 1–4 in `act` mode for 48 h; triggers recorded as true or false positive
- [ ] False positives filed as issues with a fixture reproducing each

## Week 3

### report: daily cost-of-chaos report
labels: P0, week-3, area:report
Rule 8. A daily report with: cost of chaos (idle hours, tokens burned on restarts), what
Kelpie fixed by itself, what needs a human decision, and the top 3 optimization
recommendations. Delivered to Slack or Telegram and as Markdown/HTML.

**Acceptance criteria**
- [ ] Generated from storage only, deterministic for the same data
- [ ] Sections: cost of chaos, fixed by Kelpie, needs a human, top 3 recommendations
- [ ] Every number links back to the underlying tasks
- [ ] Scheduled daily; also available via `kelpie report --date`

### report: before/after verification of fixes
labels: P0, week-3, area:report
Kelpie computes the effect of every fix by itself. Example from the farm: after a manual
fix, review time dropped from 48 to 27 minutes, and failures from 9 of 29 to 1 of 11.

**Acceptance criteria**
- [ ] Fix events recorded with timestamps (rule enabled, config change, manual fix noted via CLI)
- [ ] Daily report shows the before/after effect of yesterday's fixes on idle time, review time, failures, returns and tokens
- [ ] Small samples are labelled as such

### core: `kelpie status` CLI
labels: P0, week-3, area:core
A quick terminal view of the farm from Kelpie's storage, without a custom UI.

**Acceptance criteria**
- [ ] Shows tasks per status, waiting conditions, open alerts and the last actions per rule
- [ ] Works against a running server or directly against the storage file
- [ ] Plain text by default, `--json` for scripts

### traces: token optimization — caching, context growth, restarts
labels: P1, week-3, area:traces
Basic token optimization from traces, no LLM: cache hit rate and where caching breaks,
context growth per step, restarts from zero.

**Acceptance criteria**
- [ ] Cache hit rate per role and model, with the steps where the cache breaks
- [ ] Context growth per step; sessions with runaway context flagged
- [ ] Tokens burned on restarts from zero (feeds the cost of chaos)
- [ ] Recommendations with estimated savings in the daily report

### traces: token optimization — repeated fragments, unused tools, huge outputs
labels: P1, week-3, area:traces
Find repeated prompt fragments, tools that are offered but never used, and huge tool
outputs (recommend processing them with a script instead of reading them into context).

**Acceptance criteria**
- [ ] Repeated prompt fragments detected across sessions of a role
- [ ] Unused tools per role listed
- [ ] Tool outputs above a size threshold listed with a script-processing recommendation
- [ ] Recommendations with estimated savings in the daily report

### report: farm throughput metrics
labels: P1, week-3, area:report
Farm optimization metrics: tasks per hour per role, task time by stage (queue, review,
waiting for conditions, ownerless statuses), queue length and utilization per role, rework
rate, cost per completed task.

**Acceptance criteria**
- [ ] Metrics computed from task status history and traces
- [ ] Exported on `/metrics` with Grafana panels, and summarized in the daily report

### report: theory-of-constraints recommendations
labels: P1, week-3, area:report
Turn throughput metrics into recommendations following the theory of constraints: find the
bottleneck, relieve it, subordinate the rest, elevate it.

**Acceptance criteria**
- [ ] The current bottleneck role or stage identified each day
- [ ] Recommendations ranked and fed into the top 3 of the daily report
- [ ] Before/after verification applies to accepted recommendations

### docs: install, configuration and extension guides
labels: P0, week-3, area:docs
User and contributor documentation for the first public release.

**Acceptance criteria**
- [ ] Install guide (docker compose and uv) and Hermes plugin setup
- [ ] Configuration reference for every rule and threshold
- [ ] Guides for writing a rule, an adapter and a connector, with the fixture format

### deploy: publish kelpie-sre 0.1.0a1
labels: P0, week-3, area:deploy
Publish the open core: first public pre-release on PyPI and a container image.

**Acceptance criteria**
- [ ] Release workflow builds and publishes with trusted publishing
- [ ] `pip install kelpie-sre` provides a working `kelpie` CLI
- [ ] Container image published and referenced by docker-compose.yml
- [ ] Changelog and release notes

### project: 10 conversations with farm owners
labels: P1, week-3, area:project
Validate demand: talk to 10 people who run agent farms (engineering teams with fleets of
coding agents; self-hosted farms on Hermes, OpenClaw, Paperclip).

**Acceptance criteria**
- [ ] 10 conversations held; notes kept without personal data in the repo
- [ ] Top incidents and requested features summarized and compared with the 13 farm incidents
- [ ] Follow-up issues filed for confirmed needs

## Backlog (P1, week 3 only if the budget allows)

P1 work that the plan does not schedule in a week. Week 3 holds only the P1 items the
plan lists there (token optimization, throughput metrics, 10 conversations).

### rules: return_causes — root causes of task returns (incident 7)
labels: P1, area:rules
46 of 63 task returns were merge conflicts in a few shared files. Analyze why tasks come
back, from returns and traces, and turn the dominant causes into a recommendation (for
shared-file conflicts: the hot files, and a warning to in-progress tasks touching them).

**Acceptance criteria**
- [ ] Returns grouped by cause category; share of each cause computed over a window
- [ ] Dominant shared-file conflicts → `alert` with `hot_files` and a recommendation; in-progress tasks touching them → `comment`
- [ ] Configurable allowlist of files that are expected to be shared
- [ ] Fixtures: existing `07-shared-file-conflicts.yaml`; new `07-disjoint-files.yaml`

### core: Telegram alerter
labels: P1, area:core
Implement the `Alerter` protocol for Telegram (bot token and chat id) on top of the alert
pipeline, with the same content as the Slack alerter.

**Acceptance criteria**
- [ ] Severity, what Kelpie already did and what needs a human are shown
- [ ] Message length and formatting limits handled; untrusted text escaped
- [ ] Tests mock HTTP; no network access

### project: check the Kelpie name (domains and USPTO)
labels: P1, area:project
Before publishing, check that the name is usable. The PyPI package is `kelpie-sre` because
`kelpie` is taken.

**Acceptance criteria**
- [ ] Domain availability checked and the result recorded
- [ ] USPTO search for conflicting marks in software classes recorded
- [ ] Decision: keep the name or pick an alternative before the release

### report: optional LLM root-cause section
labels: P1, area:report
Optionally ask an LLM to analyze root causes for the day's incidents. Never used in the
control loop.

**Acceptance criteria**
- [ ] Disabled by default; report is complete without it
- [ ] Prompt contains only aggregated, redacted data
- [ ] Clearly marked as generated text in the report

### rules: orphan_status — stop patterns (rule 3, incident 11)
labels: P1, area:rules
Field evidence: tasks stop silently in ways that are not "no owner". Extend rule 3 with
stop patterns, so a task that nothing will move again is flagged.

**Acceptance criteria**
- [ ] In review with nothing driving it (no reviewer session, no open condition) for more than N minutes → `alert`
- [ ] A successful run with no next action (session ended with success, task not moved on, no follow-up task) → `alert`
- [ ] A blocker chain that ends in a cancelled or inaccessible task → `alert` naming the dead end
- [ ] Recovery tasks recovering themselves (a recovery task whose target is itself a recovery task) → `alert`
- [ ] Tasks with an active session or an open condition are not flagged
- [ ] Fixtures: new `11-review-nobody-driving.yaml`, `11-blocker-chain-dead-end.yaml`, `11-recovery-loop.yaml`

### core: `kelpie why <task>` CLI
labels: P1, area:core
Explain in one screen why a task is not moving: status and how long it has been there,
owner, blocks, open conditions, the dependency chain up to the first dead end, the last
session and the rule findings for the task (stop patterns included).

**Acceptance criteria**
- [ ] `kelpie why T-101` prints the explanation from storage or a running server; `--json` for scripts
- [ ] Uses the same rule results as the engine (no separate logic)
- [ ] Tests on the stop-pattern fixtures

### rules: conflict_warning — early conflict warning (rule 12, incident 7)
labels: P1, area:rules
Field evidence and incident 7: two live tasks edit the same files and only find out at
merge. `return_causes` explains conflicts after the fact; this rule warns while both tasks
are still in progress.

**Acceptance criteria**
- [ ] Two live tasks whose changed files overlap → `comment` on both naming the files and the other task
- [ ] Optional serialization (config): the later task is held until the earlier one merges
- [ ] Files on the expected-shared allowlist (same config as `return_causes`) are ignored
- [ ] Fixtures: new `07-live-tasks-same-files.yaml` (a live task with disjoint files as the distractor)

### rules: exit_contract — "done" requires evidence, reopened-after-done rate (rule 1, incident 2)
labels: P1, area:rules
Field evidence: tasks are marked done without test output, green CI or a live check, and
then reopened. Extend rule 1 so "done" needs evidence, and track how often done tasks come
back.

**Acceptance criteria**
- [ ] Task moved to done without configured evidence (test-output artifact, satisfied `ci_status` condition, live-check artifact) → `requeue` with the missing evidence in the reason (`alert` only, if configured)
- [ ] Evidence kinds come from config and the snapshot; no CI-specific code
- [ ] Reopened-after-done rate per role exported on `/metrics` and in the daily report
- [ ] Done tasks with evidence produce no action
- [ ] Fixtures: new `02-done-without-evidence.yaml`

### rules: needs_you — "needs you" inbox with cost of delay (rule 13, incident 13)
labels: P1, area:rules
Everything waiting for a human, across platforms, in one list sorted by cost of delay:
human tasks that block agent work and agents waiting for approval. Replaces the earlier
`human_tasks` rule.

**Acceptance criteria**
- [ ] Human tasks blocking agent tasks beyond a threshold → `escalate` to the human owner with the cost of delay (blocked agent tasks, idle agent hours)
- [ ] Agents waiting for approval longer than N minutes → included in the inbox and escalated
- [ ] The inbox is sorted by cost of delay and shown in the daily report's "needs a human" section
- [ ] Human tasks with nothing blocked behind them are not escalated
- [ ] Fixtures: existing `13-human-tasks-block-agents.yaml`; new `13-approval-wait.yaml`

## Later (P2, on signal)

### rules: compaction_gate — gate violation after context compaction (rule 14, field evidence)
labels: P2, area:rules
Field evidence: right after a context compaction the agent forgets the task's gate, e.g.
edits code while the task is in plan review. Start only when the signal shows up on our
farm or in validation conversations.

**Acceptance criteria**
- [ ] Compaction events available to rules, from traces or platform events (model change if needed)
- [ ] Gates are data: task status → tools or actions not allowed in it (e.g. no edits in plan review)
- [ ] A disallowed tool call within N minutes after a compaction → `stop` + `alert` (or `comment`, per config)
- [ ] Fixtures: new `field-03-edit-during-plan-review.yaml` (`incident: null`)

### project: one-off farm audit offer
labels: P2, area:project
Early revenue: a one-off audit of a team's agent farm using Kelpie's report (cost of chaos,
bottlenecks, top recommendations). Start only after the validation conversations show demand.

**Acceptance criteria**
- [ ] Audit scope and deliverable described
- [ ] Report template based on the daily report

### adapters: Claude Code adapter
labels: P2, adapter, area:adapters
The first candidate after Hermes. Claude Code already exports telemetry to OpenTelemetry,
so traces come through OTLP ingest; the adapter maps sessions and tasks onto the core model
and implements the actions the platform allows.

**Acceptance criteria**
- [ ] Sessions and tasks mapped onto `kelpie.core.model`; status mapping documented
- [ ] Supported actions implemented, unsupported ones fall back to alerts through action routing
- [ ] Tests against synthetic data, no network access

### adapters: Paperclip adapter
labels: P2, adapter, area:adapters
Platform adapter for Paperclip farms. Start on signal (a user or a validation conversation).

**Acceptance criteria**
- [ ] Tasks, statuses, owners and sessions mapped onto `kelpie.core.model`; status mapping documented
- [ ] Supported actions implemented, unsupported ones fall back to alerts through action routing
- [ ] Spans from OTLP or from hooks via the hooks-to-spans helper

### adapters: OpenClaw adapter
labels: P2, adapter, area:adapters
Platform adapter for OpenClaw farms. OpenClaw has no built-in loop guard, so `stop` support
makes `loop_breaker` useful there. Start on signal.

**Acceptance criteria**
- [ ] Tasks, statuses, owners and sessions mapped onto `kelpie.core.model`; status mapping documented
- [ ] `SupportsStop` implemented if the platform allows it; other unsupported actions fall back to alerts
- [ ] Spans from OTLP or from hooks via the hooks-to-spans helper

### adapters: Codex adapter
labels: P2, adapter, area:adapters
Platform adapter for Codex agents. Start on signal.

**Acceptance criteria**
- [ ] Sessions and tasks mapped onto `kelpie.core.model`; status mapping documented
- [ ] Supported actions implemented, unsupported ones fall back to alerts through action routing
- [ ] Spans from OTLP or from hooks via the hooks-to-spans helper

### adapters: GitHub Issues / Linear task-queue adapter
labels: P2, adapter, area:adapters
A generic adapter for farms whose task queue is GitHub Issues or Linear rather than a
platform's own board. Start on signal.

**Acceptance criteria**
- [ ] Issues mapped to tasks; statuses, assignees and blockers configurable per board (labels, states)
- [ ] `requeue`, `unblock`, `comment` and `reassign` implemented through the tracker API
- [ ] Respects API rate limits; tests with mocked HTTP
