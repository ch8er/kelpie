# Kelpie: what we build and the plan

As of 2026-10-15 (field evidence from other farms; budgets and spend; work continues on the subscription after the credit). This file is the single source of truth for scope and order of work. It replaces the short plan given in the first session message.

## Summary

Kelpie is SRE and throughput optimization for AI agent farms. Requirements come from real incidents on a working agent farm, cross-checked with field evidence from other farms. The first adapter is Hermes, but the core is tied to no platform and no machine.

Priorities: **P0** — now, this week · **P1** — next step · **P2** — later, on signal · **No** — out of scope.

## Requirements from a real farm

13 incidents from three days of a running farm, plus those found during the first live run, are a ready-made requirements list: almost all of them are about execution control, not LLM quality.

| # | Incident | Cost | What Kelpie does |
| --- | --- | --- | --- |
| 1 | Tasks stuck in the dispatcher guard | dozens of manual unblocks | Stale blocks: checks the block reason against external state and clears it |
| 2 | Agents exited without a status, or set a block instead of returning the task | hours of idle time | Exit contract: a task without a final status goes back to the queue |
| 3 | Disk filled to 99% | half a day | Environment resources: quotas and an alert before the disk fills |
| 4 | Host overloaded, heavy-task queue 1.5–2 h | main throughput brake | Environment resources plus a concurrency limit for heavy tasks |
| 5 | CI minutes ran out | 11 h of idle time | External conditions: CI economy and quota burn forecast (rule 9) |
| 6 | LLM provider limits and expired login | up to 200 limit hits per day | Provider quotas and auth health, concurrency tuned to the limit |
| 7 | Conflicts in shared files | 46 of 63 task returns | Root-cause analysis of returns from traces → recommendation (P1); early conflict warning (rule 12, P1) |
| 8 | Blocks stayed after the cause was fixed | 3 h | Stale blocks, same as #1 |
| 9 | Idle agents due to a sequential task graph | half a day | Role utilization and critical path |
| 10 | Agent waited for an external event inside its session; the dispatcher declared it dead | reviews took 40–90 min, 4 of 10 failed | "Wait for condition" and a "waiting" heartbeat |
| 11 | Task stuck in a status with no owner | half a day, 7 tasks blocked | Orphan statuses |
| 12 | 9 review rounds on one task | not measured | Ping-pong → escalation |
| 13 | Human tasks blocked agents | not measured | Humans in the task graph, with cost of delay (rule 13, P1) |
| 14 | A deliberate merge hold was promised to be lifted in 2.5 h and was forgotten; ready PRs waited, agents had no work | about 5 h of idle farm, 1 task closed | Idle with ready work (rule 6) and holds with an owner and an expiry |
| 15 | A reviewer blocked its own card "waiting for CI/merge" while its PR sat in the merge queue | most of the first live run's block alerts | Block reasons matched by configurable patterns become conditions (pr_merged, ci_green); rule 4 clears them when met |
| 16 | An agent task was linked to a human task (store products in the app store console); 28 agent tasks depended on it through a chain, the ready queue held only human tasks | farm at 3 of 5 slots, about 4 tasks an hour instead of 9 | Human tasks on the critical path with the number of agent tasks behind them and the idle capacity (rule 6); the dependency itself is a human decision, Kelpie only shows its cost |

Example of what Kelpie must compute by itself: after a manual fix, review time dropped from 48 to 27 minutes, and failures went from 9 of 29 to 1 of 11.

## Field evidence from other farms

The same failure modes show up outside our farm, on other platforms. Two of them are not covered by any of the 13 incidents and get their own P0 rules (10, 11):

- **Runaway loops and overnight budget burn.** A heartbeat/maintenance loop repeated the same tool call about 900 times overnight, each time resending about 80k input tokens. Single-session guards exist only for some platforms: [AgentWatchdog](https://github.com/jusong/agentwatchdog) covers Claude Code only, and OpenClaw closed the request for such a guard as "not planned" ([openclaw/openclaw#159329](https://github.com/openclaw/openclaw/issues/159329)). A cross-platform breaker built on traces is therefore in scope.
- **Activity without progress.** Idle agents keep spending tokens without moving any task, e.g. resending a 12k-token context every 5 minutes with no status change, commit or artifact.
- **Tasks that stop silently** (P1): in review with nothing driving them, a successful run with no next action, blocker chains that end in a cancelled or inaccessible task, recovery tasks that recover themselves.
- **Conflicts discovered too late** (P1): two live tasks edit the same files and only find out at merge (incident 7).
- **"Done" without evidence** (P1): tasks marked done without test output, green CI or a live check, then reopened.
- **Humans as the hidden queue** (P1): approvals and human tasks wait unnoticed across platforms (incident 13).
- **Gate violations after context compaction** (P2): right after a compaction the agent forgets the task's gate, e.g. edits code while the task is in plan review.

## Kelpie

Kelpie is SRE and throughput optimization for AI agent farms, wherever they run: a laptop, a cloud VM, containers or Kubernetes. The core knows nothing about any specific platform or CI — everything specific lives in adapters. The first adapter is Hermes, on our own farm.

Kelpie is a **service, not an LLM agent**. Control is done by deterministic rules. An LLM is optional and only used to analyze root causes in the daily report.

### Architecture: a core and three kinds of adapters

- **Platforms** — where tasks, statuses and sessions come from: Hermes first (`post_tool_call` and `on_session_end` hooks plus reading `kanban.db`), then Claude Code, Paperclip, OpenClaw, Codex, and a generic GitHub Issues / Linear task-queue adapter. Claude Code is the second adapter (P1, after the credit); the other platforms are a P2 backlog, one adapter each.
- **Environment resources** — node_exporter on every machine that runs agents, scraped by Prometheus. Kelpie reads resources through the Prometheus API. Laptop, cloud VM or container — no difference. We do not write our own collector.
- **External conditions** — what tasks wait for: PRs, CI, deploys, API responses, LLM provider quotas, human approval. CI is just one connector.

Deployment: one `kelpie serve` process (or docker compose with Kelpie, Prometheus and Grafana). The server can run anywhere; adapters and node_exporter run where the agents run. The watchdog should not die together with what it watches, so the server should eventually live on a separate machine.

### P0. Engine safety — first PRs of week 1, before any rule acts on a real farm

- **A. Action ledger:** every action gets a fingerprint; the same action is not re-emitted within a cooldown; at most N actions per task per hour; the ledger is persisted in storage. It powers alert de-duplication and the before/after report, and keeps Kelpie itself from looping.
- **B. Routing:** each task carries its `source` (the adapter it came from) and each action an executor target (a platform adapter or a connector). The engine holds a registry of executors instead of a single adapter, so connectors can expose actions too (e.g. `cancel_build` executed by the CI connector for rule 9).
- **C. Stop capability:** `SupportsStop` (stop or pause a session) and the `stop` action, needed for rule 10.
- **D. Per-rule mode** in config: `observe | suggest | act`, default `observe`. Global dry-run overrides every rule.

### P0. MVP — core rules

The numbers in brackets are the farm incidents each rule is validated against.

1. Exit contract: a session ended without a final status, or with a block that has no allowed reason → the task goes back to the queue (2)
2. "Wait for condition" instead of waiting inside a session: the agent registers a condition through any connector and exits; Kelpie wakes the task up (10)
3. Orphan statuses: a task sits in a status with no owner for more than N minutes → alert or reassignment (11)
4. Stale blocks: the cause was fixed in the outside world → the block is cleared; block reasons that match configurable patterns ("waiting for CI/merge") get a condition through a connector and count as waiting, not dead (1, 8, 15)
5. Ping-pong: after K review rounds → escalate to the architect or a human (12)
6. Bottlenecks and idle time per role → alert with a rebalancing suggestion; the whole farm or a role idle while ready work waits (e.g. mergeable PRs) → alert naming the likely cause, such as a hold; holds (labels, flags, pauses) carry an owner and an expiry, an expired hold → alert, or lifted in act mode; free slots while the ready queue holds only human tasks → alert naming the human tasks on the critical path and how many agent tasks wait behind each (9, 10, 14, 16)
7. Environment resources: disk, memory, processes or provider quotas running out, auth expiring → alert before the agents stop (3, 4, 6). Provider usage and cost also feed budgets (see below)
8. Daily "cost of chaos" report and before/after verification of every fix
9. CI economy: finds builds that burn minutes for nothing — on drafts and WIP commits, superseded by a newer push, triggered by docs-only changes, repeated for an already verified commit, or expensive jobs queued on a head where a cheap required check already failed. Cancels superseded and doomed builds by itself and forecasts how long the quota will last (5). Proposing a PR with a better CI configuration is P1 (rules backlog)
10. Loop and budget breaker, cross-platform, built on traces: N identical consecutive tool calls (same tool and same arguments hash; default 8 in 10 minutes), 5 consecutive tool errors, or a budget's hard threshold exceeded (see "Budgets and spend") → stop the run through the adapter and alert. Defaults reuse the AgentWatchdog thresholds. Polling that rule 2 recognizes is coached by rule 2 first and stopped only if it continues (field evidence: heartbeat runaway)
11. Activity without progress: tokens spent by sessions that produce no status change, commit or artifact → alert with recommendations: a longer heartbeat interval, a cheaper model for maintenance calls, bounded sessions with a handoff file (field evidence: idle context resend)

### P0. Budgets and spend — week 2, dollars in the report in week 3

Spend becomes a metric Kelpie watches, not a number someone checks in a billing page.

- **Budget as a config object:** per day, task, agent or project. Soft threshold → alert; hard threshold → stop the run through `SupportsStop` (act mode only). Rule 10's budget check is one consumer of it.
- **Burn-rate forecast:** "at the current rate the budget runs out at <time>"; with a subscription, "time to the next limit".
- **Spend sources:** traces (tokens × a price table in config); Claude Code's own OpenTelemetry cost and token metrics, which arrive through OTLP ingest; provider usage and cost APIs where they exist (API keys), read by the provider quota connector.
- **Subscription mode:** there is no bill, so Kelpie estimates spend from traces, counts limit hits and forecasts the time to the limit. Scheduling heavy tasks for when the limit is free is P1.
- **Report:** cost per completed task and cost of chaos in dollars, next to hours.
- **Dogfooding:** Kelpie's own development in Claude Code is the first user of spend reporting.

### P1. Rules backlog — after weeks 2–3, on the subscription

- Rule 3, extended with stop patterns: in review with nothing driving it; a successful run with no next action; a blocker chain ending in a cancelled or inaccessible task; recovery tasks recovering themselves. Plus a `kelpie why <task>` CLI that explains why a task is not moving (11)
- Rule 12. Early conflict warning: two live tasks editing the same files → warn or serialize (7)
- Rule 1, extended: "done" requires evidence (test output, green CI or a live check); track the reopened-after-done rate (2)
- Rule 13. "Needs you" inbox: everything waiting for a human across platforms, sorted by cost of delay; detects agents waiting for approval longer than N minutes (13)
- Rule 9, extended: turn the wasted-build statistics into a proposed PR with a better CI configuration (5)

### P2. Rules backlog — on signal

- Rule 14. Gate violation after context compaction: the agent acts against the task's gate right after a compaction event (e.g. edits code while the task is in plan review)

### P0. Traces and checkpoints — the foundation, production-style, no LLM calls

- **Traces:** every step is an OpenTelemetry span following the standard GenAI semantic conventions: task → session → model or tool call; model, input, output and cached tokens, duration, outcome. Kelpie accepts traces over OTLP from any platform: Hermes can already send them through its Langfuse plugin, and Claude Code exports telemetry to OpenTelemetry. Kelpie stores them locally (SQLite or DuckDB) and optionally forwards them to Langfuse, Phoenix or Datadog. If a platform does not emit traces, its adapter writes spans from hooks.
- **Checkpoints:** if more than N minutes have passed since the last commit, or more than M files changed, the agent gets a reminder to commit. If it exits without committing, Kelpie makes a WIP commit to the task branch and writes a progress note. WIP commits do not trigger CI: they go to a draft PR or carry `[skip ci]`.
- **Resume instead of restart:** a restarted task receives the progress note and the git log and continues from the last commit instead of starting from zero.

### P1. Basic token optimization — from traces, no LLM

Cache hit rate and where caching breaks, context growth per step, repeated prompt fragments, unused tools, huge tool outputs (recommend processing them with a script), restarts from zero. All of this goes into the daily report as recommendations.

### P1. Farm optimization

Metrics: tasks per hour per role, task time by stage (queue, review, waiting for conditions, ownerless statuses), queue length and utilization per role, rework rate, cost per completed task. Recommendations follow the theory of constraints: find the bottleneck, relieve it, subordinate the rest, elevate it.

### Reporting

- **Real-time alerts** to Slack (P0) or Telegram (P1) — only actionable items, always stating what Kelpie already did by itself. Duplicate alerts are merged.
  Alerts about a lasting condition (an orphaned task, a block nothing will clear) go out on transitions only: when it starts, again at escalation thresholds (4h → 12h → 24h after the first alert) or when it gets worse, and a short "resolved" when it clears. Conditions still open are listed in the daily report.
- **Daily report** — cost of chaos (idle hours, tokens burned on restarts), what Kelpie fixed by itself, what needs a human decision, before/after effect of yesterday's fixes, top 3 optimization recommendations.
- **Dashboards** — no custom UI in the MVP. Kelpie exposes Prometheus metrics on `/metrics`, Grafana shows them, and traces open in Langfuse. A CLI: `kelpie status` (P0), `kelpie report`.

### Use what exists, do not build

- Visibility dashboards: Claude Code Agent View, hermes-monitor, Minions
- Single-session loop and budget thresholds: [AgentWatchdog](https://github.com/jusong/agentwatchdog) (Claude Code only); rule 10 reuses its thresholds as defaults — 8 repeats in 10 minutes and 5 errors in a row — and applies them across platforms
- Deep optimization of a single agent: HALO, Langfuse, Papaya — via trace export

### Market and money

- Segments: engineering teams running fleets of coding agents; self-hosted farms on Hermes, OpenClaw, Paperclip
- Model: open core with adapters → paid fleet dashboard and reports; a one-off farm audit as early revenue (P2)
- **P1** validation: open-source the core and talk to 10 people who run agent farms
- **P1** naming: check domains and USPTO for Kelpie; the PyPI package is `kelpie-sre` because `kelpie` is taken

### Risks

Nous may close stale task claims in Hermes itself ([issue #22926](https://github.com/NousResearch/hermes-agent/issues/22926)), and Anthropic already covers visibility for Claude Code. Kelpie's value is therefore in logic over the task graph and automatic repair, not in a basic watchdog timer.

## Budget: $250 of cloud session credit, then the subscription

The credit goes to building Kelpie in Claude Code cloud sessions: it is spent first, does not count against subscription limits, and expires on November 5 ([docs](https://code.claude.com/docs/en/claude-code-on-the-web)). Week 1 was done by Oct 15 for $112 against $100 planned; the overrun went mostly into review passes that found real security blockers. Budget is no longer the constraint — scope is. Once the credit is spent, development continues on the subscription until the open core is complete.

| Phase | What we build | Paid from |
| --- | --- | --- |
| Week 1 (done Oct 15) | Engine safety (A–D), core, traces and checkpoints, Hermes adapter, rules 1–4 | credit, $112 |
| Week 2 | Rules 5–7, 9–11, budgets and spend, resource reading via Prometheus, condition connectors, live run on our farm | credit |
| Week 3 | "Cost of chaos" report with dollars, basic token optimization, throughput metrics, documentation | credit, then subscription |
| After the credit | The rest of the open core, in this order: stop patterns and `kelpie why`, rule 12, rule 13, evidence for "done", full token optimization, Telegram alerter, Claude Code adapter, return causes, LLM root-cause section | subscription |
| Paid tier | Fleet dashboard, hosting, billing | only after the 10 conversations confirm demand |

On the subscription, development sessions share limits with any farm that runs on the same subscription: run them when the farm is idle, or use a separate account.

- **Repository:** `CLAUDE.md` with the architecture (core and adapters) and coding rules; roles in `.claude/agents/`.
- **Environment:** the setup script installs dependencies in under about 5 minutes; fixtures are synthetic versions of the real farm incidents.
- **One task — one branch, one PR.** The plan lives in the repo.
- **Verification:** ruff, import-linter and pytest in CI on every PR; a clean-context reviewer agent runs before a PR is opened.
- **Economy:** routine work on a smaller model, the core and "wait for condition" on the top model; no more than 2–3 parallel sessions; no waiting inside a session; WIP pushes go to draft PRs, which do not trigger CI.
- **No:** Projects and Routines are not covered by the credit ([source](https://pasqualepillitteri.it/news/17932/claude-code-sessioni-cloud-credito-250-dollari)).

## Out of scope

To keep the scope focused, Kelpie does not build what already exists or is not needed at the start.

| Not building | Why |
| --- | --- |
| "Who is doing what" dashboards | Claude Code Agent View, hermes-monitor and Minions already exist |
| Deep prompt and model optimization for a single agent | HALO, Langfuse and Papaya exist; Kelpie does the basic layer from traces itself |
| Our own orchestrator and queues | Kelpie runs on top of platforms, not instead of them |
| Coupling to CI, iOS or one machine | Everything specific lives in adapters and connectors |
| Kelpie as an LLM agent | Control is rules and code; LLM only for root-cause analysis |
| Hosting, SOC 2, enterprise sales | Only after demand is validated |

## Three-week plan

Three weeks, three steps: the core with traces and the first adapter, the full rule set, then the report with optimization and publication.

**Week 1, Oct 15–21**

- [x] Engine safety first: action ledger, routing to executors, stop capability, per-rule mode (A–D)
- [x] Repository, `CLAUDE.md`, cloud environment, fixtures from the farm incidents
- [x] Core: model of tasks, statuses, owners, conditions and resources
- [ ] Traces: OTLP intake following GenAI semantic conventions, storage, export to Langfuse
- [x] Checkpoints: commit reminders, WIP commit on exit, resume from the last commit
- [x] Hermes adapter: hooks and reading `kanban.db`
- [x] Rules: exit contract, "wait for condition", orphan statuses, stale blocks

**Week 2, Oct 22–28** (started early, Week 1 finished on Oct 15)

- [ ] Before the rules: span dedup by tool-call id, quickstart for Hermes and `kelpie doctor`, so the live run can start in parallel
- [ ] Rules: ping-pong, role bottlenecks, environment resources, CI economy
- [ ] Rules: loop and budget breaker (10), activity without progress (11)
- [ ] Budgets and spend: budget config, thresholds, burn-rate forecast, spend from traces, Claude Code metrics and provider cost APIs
- [ ] Resource reading via Prometheus and node_exporter for any machine: laptop, cloud VM, container
- [ ] Condition connectors: GitHub and CI, LLM provider quotas, human tasks
- [ ] Run on our own farm and record every rule trigger

**Week 3, Oct 29 – Nov 4** (the credit expires on Nov 5)

- [ ] Daily "cost of chaos" report with dollars, cost per completed task, and before/after verification of fixes
- [ ] Basic token optimization from traces and throughput metrics
- [ ] Documentation and publication of the open core
- [ ] 10 conversations with farm owners

Week plans hold P0 work plus the P1 items listed in week 3 above. Every other P1 item (the rules backlog, `kelpie why`, the Telegram alerter, the Claude Code adapter, return causes, the LLM root-cause section, the naming check) follows weeks 2–3 on the subscription, in the order given in the budget table.
