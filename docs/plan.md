# Kelpie: what we build and the plan

As of 2026-10-15 (field evidence from other farms added). This file is the single source of truth for scope and order of work. It replaces the short plan given in the first session message.

## Summary

Kelpie is SRE and throughput optimization for AI agent farms. Requirements come from real incidents on a working agent farm, cross-checked with field evidence from other farms. The first adapter is Hermes, but the core is tied to no platform and no machine.

Priorities: **P0** — now, this week · **P1** — next step · **P2** — later, on signal · **No** — out of scope.

## Requirements from a real farm

13 incidents from three days of a running farm are a ready-made requirements list: almost all of them are about execution control, not LLM quality.

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

- **Platforms** — where tasks, statuses and sessions come from: Hermes first (`post_tool_call` and `on_session_end` hooks plus reading `kanban.db`), then Claude Code, Paperclip, OpenClaw, Codex, and a generic GitHub Issues / Linear task-queue adapter. The other platforms are a P2 backlog, one adapter each; Claude Code is the first candidate.
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
4. Stale blocks: the cause was fixed in the outside world → the block is cleared (1, 8)
5. Ping-pong: after K review rounds → escalate to the architect or a human (12)
6. Bottlenecks and idle time per role → alert with a rebalancing suggestion (9, 10)
7. Environment resources: disk, memory, processes or provider quotas running out, auth expiring → alert before the agents stop (3, 4, 6)
8. Daily "cost of chaos" report and before/after verification of every fix
9. CI economy: finds builds that burn minutes for nothing — on drafts and WIP commits, superseded by a newer push, triggered by docs-only changes, or repeated for an already verified commit. Cancels superseded builds by itself, forecasts how long the quota will last, and proposes a PR with a better CI configuration (5)
10. Loop and budget breaker, cross-platform, built on traces: N identical consecutive tool calls (same tool and same arguments hash; default 8 in 10 minutes), 5 consecutive tool errors, or a per-agent daily/nightly token budget exceeded → stop the run through the adapter and alert. Defaults reuse the AgentWatchdog thresholds (field evidence: heartbeat runaway)
11. Activity without progress: tokens spent by sessions that produce no status change, commit or artifact → alert with recommendations: a longer heartbeat interval, a cheaper model for maintenance calls, bounded sessions with a handoff file (field evidence: idle context resend)

### P1. Rules backlog — week 3 only if the budget allows

- Rule 3, extended with stop patterns: in review with nothing driving it; a successful run with no next action; a blocker chain ending in a cancelled or inaccessible task; recovery tasks recovering themselves. Plus a `kelpie why <task>` CLI that explains why a task is not moving (11)
- Rule 12. Early conflict warning: two live tasks editing the same files → warn or serialize (7)
- Rule 1, extended: "done" requires evidence (test output, green CI or a live check); track the reopened-after-done rate (2)
- Rule 13. "Needs you" inbox: everything waiting for a human across platforms, sorted by cost of delay; detects agents waiting for approval longer than N minutes (13)

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

## Budget: $250 of cloud session credit, all on Kelpie

The whole credit goes to building Kelpie in Claude Code cloud sessions: it is spent first, does not count against subscription limits, and expires on November 5 ([docs](https://code.claude.com/docs/en/claude-code-on-the-web)). Pace: about $80 per week.

| Week | What we build | Budget, approx. |
| --- | --- | --- |
| 1, Oct 15–21 | Core, traces and checkpoints, Hermes adapter, rules 1–4 | $100 |
| 2, Oct 22–28 | Rules 5–7, 9–11, resource reading via Prometheus, condition connectors | $90 |
| 3, Oct 29 – Nov 4 | "Cost of chaos" report, basic token optimization, throughput metrics, documentation | $60 |

- **Repository:** `CLAUDE.md` with the architecture (core and adapters) and coding rules; roles in `.claude/agents/`.
- **Environment:** the setup script installs dependencies in under about 5 minutes; fixtures are synthetic versions of the real farm incidents.
- **One task — one branch, one PR.** The plan lives in the repo.
- **Verification:** ruff, import-linter and pytest in CI on every PR; a clean-context reviewer agent runs before a PR is opened.
- **Economy:** routine work on a smaller model, the core and "wait for condition" on the top model; no more than 2–3 parallel sessions; no waiting inside a session; WIP pushes go to draft PRs, which do not trigger CI.
- **No:** Projects and Routines are not covered by the credit ([source](https://pasqualepillitteri.it/news/17932/claude-code-sessioni-cloud-credito-250-dollari)).

## Out of scope

So that $250 is enough, Kelpie does not build what already exists or is not needed at the start.

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

- [ ] Engine safety first: action ledger, routing to executors, stop capability, per-rule mode (A–D)
- [ ] Repository, `CLAUDE.md`, cloud environment, fixtures from the farm incidents
- [ ] Core: model of tasks, statuses, owners, conditions and resources
- [ ] Traces: OTLP intake following GenAI semantic conventions, storage, export to Langfuse
- [ ] Checkpoints: commit reminders, WIP commit on exit, resume from the last commit
- [ ] Hermes adapter: hooks and reading `kanban.db`
- [ ] Rules: exit contract, "wait for condition", orphan statuses, stale blocks

**Week 2, Oct 22–28**

- [ ] Rules: ping-pong, role bottlenecks, environment resources, CI economy
- [ ] Rules: loop and budget breaker (10), activity without progress (11)
- [ ] Resource reading via Prometheus and node_exporter for any machine: laptop, cloud VM, container
- [ ] Condition connectors: GitHub and CI, LLM provider quotas, human tasks
- [ ] Run on our own farm and record every rule trigger

**Week 3, Oct 29 – Nov 4** (the credit expires on Nov 5)

- [ ] Daily "cost of chaos" report and before/after verification of fixes
- [ ] Basic token optimization from traces and throughput metrics
- [ ] Documentation and publication of the open core
- [ ] 10 conversations with farm owners
- [ ] Only if the budget allows: P1 rules backlog (stop patterns and `kelpie why`, rules 12 and 13, evidence for "done")
