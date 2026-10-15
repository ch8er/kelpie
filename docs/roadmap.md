# Kelpie roadmap (issue list)

This file is the source of truth for the GitHub issues created by
[`scripts/create_issues.sh`](../scripts/create_issues.sh). See [plan.md](plan.md) for goals
and exit criteria per week.

**Format (machine-parsed).** Each issue starts with a level-3 heading holding the title,
immediately followed by a `labels:` line with comma-separated labels. Everything after that,
up to the next level-2 or level-3 heading, is the issue body (Markdown). Level-2 headings
(`## Week 1`, ...) are section markers and are ignored. Do not use level-2 or level-3
headings inside an issue body; use bold text instead. Titles must be unique: the script
skips issues whose title already exists in the repository.

Labels: `P0` (needed for the milestone) / `P1` (nice to have), `week-1` / `week-2` /
`week-3`, and `area:core|rules|adapters|traces|connectors|report|resources|deploy|docs`.

## Week 1

### core: engine loop with configurable interval and dry-run
labels: P0, week-1, area:core
Run `Engine.tick` every N seconds (config, default 30 s) with a snapshot provider assembled
from adapters, connectors, resources and traces. Dry-run is the default; actions can be
enabled globally and per rule.

**Acceptance criteria**
- [ ] Loop interval and per-rule enable/dry-run flags come from config
- [ ] A failing rule is logged and skipped; it never stops the loop
- [ ] Each tick records duration and number of actions per rule
- [ ] Unit tests use a fake clock and a fake adapter; no sleeps in tests

### core: SQLite storage for snapshots, sessions and actions
labels: P0, week-1, area:core
Persist what Kelpie saw and did so that the report and before/after comparisons have data.
SQLite by default; keep the schema DuckDB-compatible for analytics.

**Acceptance criteria**
- [ ] Tables for tasks (status history), sessions, conditions, actions (with rule, reason, dry-run flag)
- [ ] Schema migrations versioned in code
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

### core: Slack alerter
labels: P0, week-1, area:core
Implement the `Alerter` protocol for Slack incoming webhooks; used by `alert` and
`escalate` actions.

**Acceptance criteria**
- [ ] Severity is reflected in the message (info / warning / critical)
- [ ] Identical alerts are deduplicated within a configurable window
- [ ] Untrusted task text is escaped before sending
- [ ] Tests mock HTTP; no network access

### core: fixture harness for incident tests
labels: P0, week-1, area:core
A shared pytest helper that loads `tests/fixtures/incidents/*.yaml` (snapshot plus expected
actions) via `kelpie.core.serde` and compares them with a rule's output.

**Acceptance criteria**
- [ ] Fixture format documented in CONTRIBUTING.md
- [ ] Mismatch output shows expected vs actual actions readably
- [ ] Order-insensitive comparison of actions
- [ ] Example fixture and test included

### core: import-boundary check
labels: P1, week-1, area:core
Enforce in CI that `kelpie.core` never imports `kelpie.adapters` or `kelpie.connectors`,
and that `kelpie.rules` performs no I/O imports.

**Acceptance criteria**
- [ ] Test or lint step fails on a forbidden import
- [ ] Runs as part of `uv run pytest`

### traces: OTLP ingest with GenAI semantic conventions
labels: P0, week-1, area:traces
Accept OTLP/HTTP spans and map them onto `Span` (task → session → model/tool call) following
the OpenTelemetry GenAI semantic conventions: model, tokens in/out/cached, duration, outcome.

**Acceptance criteria**
- [ ] OTLP/HTTP protobuf and JSON accepted
- [ ] `gen_ai.*` attributes mapped to `Span` fields; unknown attributes kept in `attributes`
- [ ] Spans linked to task and session ids
- [ ] Ingest endpoint can require a bearer token

### traces: span storage and query helpers
labels: P0, week-1, area:traces
Store spans and expose per-session and per-task aggregates (tokens, duration, tool calls)
for rules and the report.

**Acceptance criteria**
- [ ] Spans persisted with retention setting
- [ ] Aggregates available in the snapshot without loading every raw span
- [ ] Tests with synthetic traces

### core: checkpoints (commit reminder, WIP commit on exit, resume)
labels: P0, week-1, area:core
Reduce lost work when sessions die: remind agents to commit, make a WIP commit when a
session exits with a dirty worktree, and resume the next session from the last commit.

**Acceptance criteria**
- [ ] Rule emits a `comment` reminder when `last_commit_at` is older than a threshold
- [ ] Rule emits `wip_commit` when a session ends with `dirty_worktree`
- [ ] Requeued tasks carry the last commit reference for resume
- [ ] Fixtures: `tests/fixtures/incidents/checkpoint-*.yaml`

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
Implement `requeue`, `unblock`, `comment` and `wip_commit` for Hermes.

**Acceptance criteria**
- [ ] Every action leaves a comment on the task explaining what Kelpie did and why
- [ ] Actions are idempotent (re-running the same action is a no-op)
- [ ] Concurrent writes with Hermes handled safely (transactions, retry on lock)

### adapters: Hermes plugin for post_tool_call and on_session_end
labels: P0, week-1, area:adapters
A Hermes plugin that runs on the agent host and sends session events and spans to Kelpie.

**Acceptance criteria**
- [ ] `on_session_end` sends final status, exit reason and worktree state
- [ ] `post_tool_call` sends heartbeat and tool span
- [ ] Plugin never blocks the agent if Kelpie is unreachable (buffer and drop)
- [ ] Install instructions in docs

### rules: exit contract (incident 2)
labels: P0, week-1, area:rules
A session ended without a final status, or set a block whose reason is not in the allowed
list. Requeue the task with an explanatory comment.

**Acceptance criteria**
- [ ] Session ended with `final_status` empty → `requeue`
- [ ] Block reason not in the configured allowed list → `requeue`
- [ ] Healthy exits (final status, allowed block) produce no action
- [ ] Fixtures: `tests/fixtures/incidents/02-exit-without-status.yaml`, `02-block-without-reason.yaml`, `02-healthy-exit.yaml`

### rules: wait for condition (incident 10)
labels: P0, week-1, area:rules
An agent registers a condition (CI status, PR merged, quota reset, human task, time) and
exits; Kelpie wakes the task when the condition is satisfied. Also detect in-session polling
(long sessions whose spans are repeated waits on the same check).

**Acceptance criteria**
- [ ] Satisfied condition on a waiting task → `wake`
- [ ] In-session polling detected from spans → `comment` suggesting to register a condition and exit
- [ ] A long but productive session is not flagged
- [ ] Fixtures: `tests/fixtures/incidents/10-reviewer-waits-for-ci.yaml`, `10-condition-satisfied-wake.yaml`

### rules: orphan statuses (incident 11)
labels: P0, week-1, area:rules
A task stays in a non-terminal status with no owner for more than N minutes. Alert, and
reassign when a role-based owner is configured. Severity rises with the number of dependents.

**Acceptance criteria**
- [ ] Ownerless task beyond threshold → `alert` (and `reassign` if configured)
- [ ] Severity is `critical` when dependents exceed a threshold
- [ ] Tasks below the threshold or in terminal statuses are ignored
- [ ] Fixtures: `tests/fixtures/incidents/11-accepted-without-owner.yaml`, `11-below-threshold.yaml`

### rules: stale blocks (incidents 1, 8)
labels: P0, week-1, area:rules
A task is blocked on a condition that is already satisfied in the outside world (guard
condition met, cause fixed). Unblock it.

**Acceptance criteria**
- [ ] Block linked to a satisfied condition → `unblock` with the condition in the reason
- [ ] Block with an unsatisfied or unknown condition → no action
- [ ] Grace period configurable to avoid racing the agent
- [ ] Fixtures: `tests/fixtures/incidents/01-guard-block-condition-met.yaml`, `08-block-after-fix.yaml`

### deploy: docker compose stack and CI
labels: P0, week-1, area:deploy
`docker-compose.yml` with kelpie, prometheus and grafana; GitHub Actions running ruff and
pytest.

**Acceptance criteria**
- [ ] `docker compose up` starts all three services
- [ ] Prometheus scrapes Kelpie `/metrics`
- [ ] CI runs `ruff check`, `ruff format --check` and `pytest` on every PR

### core: close model gaps found while writing incident fixtures
labels: P0, week-1, area:core
Writing the 13 incident fixtures forced several facts into free text or `params`. Promote
the ones rules actually need to typed fields before implementing those rules.

**Acceptance criteria**
- [ ] `Condition` can be shared by several tasks and references what it waits on (task, person, PR)
- [ ] `Return` records conflicting files; `Task` can reference repo/PR
- [ ] `Session` and heavy tasks can be tied to a host; `Owner` exposes availability/capacity
- [ ] `Block` has a category so allowed-reason checks do not parse free text
- [ ] `Resource` has `sampled_at` and a window for event counts (e.g. hits per day)
- [ ] Task status history is available to rules (time-to-accept, bounce count)
- [ ] Fixtures in `tests/fixtures/incidents/` migrated to the new fields

## Week 2

### rules: ping-pong review escalation (incident 12)
labels: P0, week-2, area:rules
After K review rounds on one task, stop the loop and escalate to a human with the history of
return reasons.

**Acceptance criteria**
- [ ] `review_rounds >= K` → `escalate` once (not every tick)
- [ ] Escalation message lists return reasons and categories
- [ ] Fixtures: `tests/fixtures/incidents/12-nine-review-rounds.yaml`, `12-below-threshold.yaml`

### rules: bottlenecks and idle time by role (incidents 9, 10, 13)
labels: P0, week-2, area:rules
Compute idle agents and queue depth per role, detect sequential dependency chains that keep
agents idle, and human tasks that block agent work.

**Acceptance criteria**
- [ ] Idle time per role computed from sessions and task statuses
- [ ] Long sequential chains with idle agents → `alert` suggesting parallelization
- [ ] Human tasks blocking agent tasks beyond a threshold → `alert` to the human owner
- [ ] Fixtures: `tests/fixtures/incidents/09-sequential-graph.yaml`, `10-dispatcher-thinks-dead.yaml`, `13-human-task-blocks.yaml`

### rules: environment resources and quotas (incidents 3, 4, 5, 6)
labels: P0, week-2, area:rules
Warn before resources run out: disk, memory, processes and load, LLM rate limits, CI minutes,
expiring logins.

**Acceptance criteria**
- [ ] Utilization thresholds per resource kind (warning / critical)
- [ ] Credentials expiring within a window → `alert`
- [ ] Repeated LLM limit hits aggregated into one alert with counts
- [ ] Fixtures: `tests/fixtures/incidents/03-disk-99.yaml`, `04-host-overload.yaml`, `05-ci-minutes-exhausted.yaml`, `06-llm-limits-expired-login.yaml`

### resources: read host metrics via Prometheus HTTP API
labels: P0, week-2, area:resources
No custom collector: query node_exporter metrics through the Prometheus HTTP API and turn
them into `Resource` readings per host.

**Acceptance criteria**
- [ ] Disk, memory, load and process count per host
- [ ] PromQL queries configurable
- [ ] Prometheus unavailability degrades gracefully (rule sees no data, alert once)

### resources: expose Kelpie metrics on /metrics
labels: P1, week-2, area:resources
Prometheus metrics for the loop and the farm: tick duration, actions per rule, tasks per
status, idle time per role.

**Acceptance criteria**
- [ ] `/metrics` in Prometheus text format
- [ ] Grafana dashboard JSON provisioned by docker compose

### connectors: GitHub PR and CI status
labels: P0, week-2, area:connectors
Evaluate `ci_status` and `pr_merged` conditions against GitHub, and expose remaining Actions
minutes as a resource.

**Acceptance criteria**
- [ ] Conditions updated with `satisfied`, `satisfied_at`, `checked_at`
- [ ] Respects rate limits with caching / conditional requests
- [ ] Works with a fine-grained read-only token
- [ ] Tests with mocked HTTP

### connectors: LLM provider quotas and auth expiry
labels: P1, week-2, area:connectors
Read rate-limit and quota state and credential expiry for configured LLM providers, from
provider APIs where available and from trace errors otherwise.

**Acceptance criteria**
- [ ] `quota_reset` conditions evaluated
- [ ] Limit hits from spans counted per provider
- [ ] No provider SDK is required by core

### connectors: human tasks
labels: P1, week-2, area:connectors
Evaluate `human_task` conditions (task assigned to a human owner reached a terminal status),
so agent tasks can wait on humans without polling.

**Acceptance criteria**
- [ ] Works with human-owned tasks on the same board via the adapter
- [ ] Satisfied condition wakes the dependent agent task through the wait-for-condition rule

### core: condition registration API
labels: P0, week-2, area:core
HTTP endpoint (and helper in the Hermes plugin) for an agent to register a condition for its
task before exiting.

**Acceptance criteria**
- [ ] `POST /conditions` with kind, params and task id
- [ ] Authentication required
- [ ] Duplicate registrations are idempotent

### rules: file contention (incident 7)
labels: P1, week-2, area:rules
Detect tasks running in parallel that touch the same shared files (the cause of 46 of 63
returns) and serialize or warn before work starts.

**Acceptance criteria**
- [ ] Overlap computed from `Task.files` and tool spans
- [ ] Overlapping in-progress tasks → `comment` / `alert`; configurable allowlist of shared files
- [ ] Fixtures: `tests/fixtures/incidents/07-shared-file-conflict.yaml`, `07-disjoint-files.yaml`

### deploy: live run on the maintainer's farm
labels: P0, week-2, area:deploy
Run Kelpie against the real Hermes farm: dry-run first, then enable actions rule by rule.

**Acceptance criteria**
- [ ] 24 h dry-run with action log reviewed
- [ ] Rules 1, 3, 4 enabled with actions for 48 h
- [ ] False positives filed as issues with a fixture reproducing each

## Week 3

### report: daily cost-of-chaos report
labels: P0, week-3, area:report
Daily report: idle agent hours, wasted tokens, returns and blocks by cause, incidents
handled by each rule. Delivered to Slack and as Markdown/HTML.

**Acceptance criteria**
- [ ] Generated from storage only, deterministic for the same data
- [ ] Every number links back to the underlying tasks
- [ ] Scheduled daily; also available via `kelpie report --date`

### report: before/after effect of fixes
labels: P0, week-3, area:report
Compare metrics for a period before and after a rule (or other fix) was enabled.

**Acceptance criteria**
- [ ] Fix events recorded with timestamps (rule enabled, config change)
- [ ] Report shows deltas for idle time, returns and tokens per fix
- [ ] Small samples are labelled as such

### report: optional LLM root-cause section
labels: P1, week-3, area:report
Optionally ask an LLM to summarize root causes for the day's incidents. Never used in the
control loop.

**Acceptance criteria**
- [ ] Disabled by default; report is complete without it
- [ ] Prompt contains only aggregated, redacted data
- [ ] Clearly marked as generated text in the report

### traces: basic token optimization
labels: P1, week-3, area:traces
Surface token waste from traces: low cache hit ratio, repeated identical tool calls,
oversized contexts, sessions that burn tokens after the task is effectively done.

**Acceptance criteria**
- [ ] Per-role and per-model token breakdown (in / out / cached)
- [ ] Top wasteful patterns listed in the daily report with estimated savings

### report: throughput metrics
labels: P1, week-3, area:report
Lead time, cycle time, WIP and idle time per role, exported to `/metrics` and the report.

**Acceptance criteria**
- [ ] Metrics computed from task status history
- [ ] Grafana panels for each metric

### traces: optional Langfuse export
labels: P1, week-3, area:traces
Forward ingested spans to Langfuse for teams that already use it.

**Acceptance criteria**
- [ ] Disabled unless configured
- [ ] Export failures never block ingest

### docs: install, configuration and extension guides
labels: P0, week-3, area:docs
User and contributor documentation for the first public release.

**Acceptance criteria**
- [ ] Install guide (docker compose and uv) and Hermes plugin setup
- [ ] Configuration reference for every rule and threshold
- [ ] Guides for writing a rule, an adapter and a connector, with the fixture format

### deploy: publish kelpie-sre 0.1.0a1
labels: P0, week-3, area:deploy
First public pre-release on PyPI and a container image.

**Acceptance criteria**
- [ ] Release workflow builds and publishes with trusted publishing
- [ ] `pip install kelpie-sre` provides a working `kelpie` CLI
- [ ] Container image published and referenced by docker-compose.yml
- [ ] Changelog and release notes
