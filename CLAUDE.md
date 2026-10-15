# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

**`docs/plan.md` is the single source of truth for scope, priorities and order of work.**
If this file, the README or the roadmap disagree with it, the plan wins — fix the others.

## What Kelpie is

SRE and throughput optimization for AI agent farms (Hermes, OpenClaw, Paperclip, Claude
Code, Codex, ...), wherever they run: laptop, cloud VM, containers, Kubernetes. Kelpie is a
**service, not an LLM agent**: control is done by deterministic rules. An LLM is optional
and used only for root-cause analysis in the daily report. Kelpie runs on top of
platforms, not instead of them; its value is logic over the task graph and automatic
repair, not a watchdog timer.

Priorities in the plan and roadmap: **P0** now · **P1** next · **P2** later, on signal ·
**No** out of scope.

## Architecture: a core and three kinds of adapters

```
src/kelpie/
  core/         platform- and CI-agnostic: model, engine, ports, serde, storage, alerts
  rules/        control rules: pure functions Snapshot -> [Action]; REGISTRY, PLANNED
  adapters/     platforms: where tasks, statuses, sessions come from (hermes/ first)
  connectors/   external conditions: PR/CI, deploys, APIs, LLM quotas, human approval
  traces/       OTLP ingest (OpenTelemetry GenAI semconv), storage, optional export
  report/       daily "cost of chaos" report, before/after verification of fixes
  cli.py        `kelpie serve` (later `kelpie status`, `kelpie report`)
  server.py     control loop, /metrics, /healthz, OTLP endpoint
tests/fixtures/incidents/   synthetic versions of the 13 farm incidents
deploy/         Prometheus and Grafana config for docker-compose.yml
```

- **Platforms** (`adapters/`): Hermes first (`post_tool_call` and `on_session_end` hooks +
  `kanban.db`), then Claude Code, Paperclip, OpenClaw, Codex, GitHub Issues / Linear.
- **Environment resources**: node_exporter on every agent machine, scraped by Prometheus;
  Kelpie reads them through the Prometheus API. We do not write our own collector.
- **External conditions** (`connectors/`): what tasks wait for. CI is just one connector.
- **Model** (`core/model.py`): Task, Status, Owner, Dependency, Condition, Block, Resource,
  Session, Span, Snapshot — immutable dataclasses.
- **Ports** (`core/ports.py`): `PlatformAdapter` (list_tasks, get_task, requeue, unblock,
  comment, wip_commit, events), optional `SupportsReassign`, `ConditionConnector`,
  `Alerter`. Core depends on protocols; integrations implement them.
- **Engine** (`core/engine.py`): every N seconds build a Snapshot, run all rules, dispatch
  actions through the adapter. Dry-run by default; a failing rule/action never stops it.
- **Deploy**: one `kelpie serve` process or `docker compose` (kelpie, Prometheus, Grafana).
  Adapters and node_exporter run where the agents run. The watchdog must not die with what
  it watches: the server should eventually live on a separate machine.

## Rules (P0 MVP; incident numbers in brackets)

| # | Rule (`rules/` name) | Action |
|---|---|---|
| 1 | Exit contract (`exit_contract`) [2] | no final status / block without allowed reason → requeue |
| 2 | Wait for condition (`wait_condition`) [10] | agent registers a condition and exits; Kelpie wakes the task; "waiting" heartbeat |
| 3 | Orphan statuses (`orphan_status`) [11] | ownerless status > N min → alert / reassign |
| 4 | Stale blocks (`stale_blocks`) [1, 8] | cause fixed in the outside world → unblock |
| 5 | Ping-pong (`ping_pong`) [12] | > K review rounds → escalate to architect or human |
| 6 | Bottlenecks (`bottlenecks`) [9, 10] | idle time per role, critical path → alert with a rebalancing suggestion |
| 7 | Resources (`resources`) [3, 4, 6] | disk, memory, processes, provider quotas, auth expiry → alert before agents stop |
| 8 | Daily report (`report/`) | cost of chaos + before/after verification of every fix |
| 9 | CI economy (`ci_economy`) [5] | wasted builds (drafts/WIP, superseded, docs-only, already verified commit) → cancel superseded, forecast quota, propose a better CI config PR |

Also derived from incidents, not numbered in the plan (priority set in the roadmap):
`return_causes` [7] — root causes of returns from traces → recommendation (P1);
`human_tasks` [13] — humans in the task graph, with cost of delay (P0).

Foundation (P0, no LLM): **traces** (OTLP, GenAI semconv, local SQLite/DuckDB, optional
Langfuse/Phoenix/Datadog export; adapters write spans from hooks if a platform emits none)
and **checkpoints** (`checkpoints`: commit reminder after N minutes / M files, WIP commit to
the task branch on exit — draft PR or `[skip ci]` — and resume from the last commit with a
progress note). P1: basic token optimization and farm throughput metrics from traces.

## Reporting

- **Real-time alerts** (Slack, Telegram): only actionable items, always say what Kelpie
  already did by itself, merge duplicates.
- **Daily report**: cost of chaos (idle hours, tokens burned on restarts), what Kelpie
  fixed, what needs a human decision, before/after of yesterday's fixes, top-3
  recommendations.
- **Dashboards**: no custom UI in the MVP. `/metrics` → Prometheus → Grafana; traces in
  Langfuse. CLI: `kelpie status`, `kelpie report`.

## Invariants (do not break)

1. `kelpie.core` (and `kelpie.rules`) never import `kelpie.adapters` or
   `kelpie.connectors`. Enforced by import-linter (`uv run lint-imports`) and
   `tests/test_architecture.py`.
2. No LLM in the control loop. `core/`, `rules/` and `server.py` never import an LLM client.
3. A rule is a pure function over a Snapshot: no I/O, no clock reads (use `snapshot.now`),
   no mutation. Side effects happen only via actions executed by the engine through an
   adapter (or connector). Every rule has fixture tests in `tests/fixtures/incidents/`
   with a negative case; `kelpie.rules` coverage must stay >= 90%.
4. No coupling to a specific CI, iOS, or a single machine. Hosts, repos, providers and
   CI commands are data (config), not code paths.
5. Python 3.12, uv, ruff, pytest, type hints everywhere.

## Out of scope (see the plan for reasons)

"Who is doing what" dashboards, single-session loop/overspend guards (reuse AgentWatchdog
thresholds: 8 repeats in 10 minutes, 5 errors in a row), deep single-agent prompt/model
optimization, our own orchestrator or queues, Kelpie as an LLM agent, hosting/SOC 2.

## Workflow

- **One task — one branch, one PR.** Small PRs. Commit after every meaningful step.
- Push work in progress to a **draft** PR and keep pushing after every completed step.
  Draft PRs do not run CI. Mark the PR ready for review only when the step is done and
  ruff and pytest pass locally; that triggers CI (later pushes rebuild, cancelling the
  previous run for the branch). Changes only to `docs/` or `*.md` skip CI.
- Never wait for CI inside a session — push and move on (rule 2 applies to us too).
- Before a PR is marked ready, run the `reviewer` subagent (`.claude/agents/reviewer.md`)
  on the diff against `main` and fix its findings.
- Economy: routine work on a smaller model; the core and "wait for condition" on the top
  model; no more than 2–3 parallel sessions.
- Local checks:
  ```
  uv sync
  uv run ruff check . && uv run ruff format --check .
  uv run lint-imports
  uv run pytest --cov=kelpie
  uv run coverage report --include='src/kelpie/rules/*' --fail-under=90
  ```
- Adding a rule: add a module in `src/kelpie/rules/`, register it with
  `@register("<name>")` (modules are imported automatically), make the fixtures with
  `rule: <name>` pass (they stop being skipped), add unit tests for edge cases.
- Adding an incident: see `tests/fixtures/incidents/README.md`. Synthetic data only —
  never personal data, hostnames or repo names from real farms.
- Code, comments and docs in English.
- Backlog: `docs/roadmap.md` (`scripts/create_issues.sh` turns it into GitHub issues).
