# Kelpie

**Kelpie keeps an AI agent farm moving.** It watches the task board, agent sessions, traces
and hosts of farms like Hermes and Claude Code, and applies deterministic rules (no LLM in
the loop) to unstick work and stop waste, then tells you each day what the chaos cost and
what fixed it.

```bash
pip install kelpie-sre && kelpie demo
```

`kelpie demo` runs a simulated farm on your machine and shows Kelpie finding and fixing its
problems. No farm, accounts or keys needed; Python 3.12 or newer. It replays a working day
of real farm incidents on a simulated Hermes board, offline, then prints that day's report
(`--web` also opens it as a web page, `--keep` keeps the farm and the full report). An
excerpt:

```console
$ kelpie demo
Kelpie demo: one working day on a simulated agent farm
...
── 2. A runaway loop ─────────────────────────────────────────────────────────
...
10:00  ops starts t_heartbeat. Every 30 seconds it calls the model, resending
       80k tokens of context, and runs `heartbeat_check`.
10:05  Kelpie notices (loop_breaker): identical consecutive calls, past the
       limit.
       ┌ alert, as Telegram shows it
       │ [CRITICAL] loop_breaker: alert t_heartbeat
       │ Runaway run: session hermes-run-4 (agent ops, task t_heartbeat) made
       │ 10 identical consecutive `heartbeat_check` calls (limit 8 within 10
       │ minutes). Check the task before resuming it.
       │ Stopped run hermes-run-4; task t_heartbeat is blocked for a human
       │ (hermes kanban unblock t_heartbeat); Kelpie did not end its worker
       │ (the board records no worker process for the run).
       └
10:05  Kelpie ran `hermes kanban block --kind needs_input -- t_heartbeat ...`:
       t_heartbeat is blocked.
       observe  records "stop run hermes-run-4"; the loop keeps burning tokens
                until someone looks.
       suggest  sends the alert, ending "Suggested: stop run hermes-run-4 (not
                done, rule in suggest mode)."; the loop runs until a person
                stops it.
       act      stops the run through the platform (above) and says so: one
                critical message, to every channel.

       The stop came after 10 model calls: 0.8M tokens, $2.44 ($3 per million
       input tokens, $15 per million output). Left alone for a night like on
       that farm (900 calls) it is 72M input tokens, about $216.
...
── 5. The daily report: cost of chaos ────────────────────────────────────────
Tokens burned counts the model calls of a stuck or looping session after
Kelpie reported it. Kelpie stopped the runaway in that same tick, so nothing
burned here; its calls before the stop are in the day's spend.

$ kelpie report --date 2026-03-03

Cost of chaos: 4.5 hours lost, $0.00 in 0 tokens burned + $11.25 in stuck
hours. 5 tasks completed, $0.49 per completed task.

...
## Fixed by Kelpie

- `loop_breaker`: stop x1 `t_heartbeat`

Would have done in act mode (observe, suggest or dry-run):

- `stale_blocks`: unblock x1 `t_review` (act mode is not available here:
  Hermes can't clear blocks yet)
...
```

> **Alpha (0.1.0a1).** Config and storage formats may still change. Kelpie starts safe:
> global dry-run on and every rule in `observe` mode, so it changes nothing until you let
> it.

## What it found on a real farm

Kelpie's rules come from incidents on a working agent farm and from field reports of other
farms. Almost none of them are about model quality; they are about work that silently
stops.

| What happened | What it cost | What Kelpie does |
| --- | --- | --- |
| Parallel tasks edited the same files | 46 of 63 task returns were conflicts | Warns both tasks while they are live; finds return causes from traces |
| CI minutes ran out | 11 h of idle farm | Cancels wasted builds, forecasts CI quota burn |
| LLM provider limits and expired logins | up to 200 limit hits a day | Probes quotas and auth before agents stop |
| A merge hold "for 2.5 h" was forgotten | about 5 h of idle farm, 1 task closed | Holds get an owner and an expiry; alert on idle agents with ready work |
| One human task had 28 agent tasks behind it | about 4 tasks an hour instead of 9, 3 of 5 slots busy | A "needs you" inbox per person, sorted by cost of delay |
| A parent task stayed open after its subtasks merged | 20 tasks gated; one card sat 21 h in a pre-work status | "Close it?" with the work waiting behind it |
| Agents waited for reviews inside their sessions | reviews took 40–90 min, 4 of 10 failed | The agent registers what it waits for and exits; Kelpie wakes the task |
| An agent exited without a final status | hours of idle time | The task goes back to the queue |
| A simulator cache grew about 9 GB a day | 2–3 days to a full disk | Disk alerts before it fills (a time-to-full forecast is planned) |
| A heartbeat loop repeated one tool call overnight (another farm) | about 900 calls, each resending ~80k input tokens | Loop and budget breaker on traces, any platform |

Kelpie also measures whether a fix worked. After one manual fix on that farm, review time
dropped from 48 to 27 minutes and review failures from 9 of 29 to 1 of 11; the daily report
now computes that before/after for every fix it knows about.

## Quickstart

Every quickstart starts read-only (dry-run, `observe`). `kelpie doctor` checks the setup and
prints a fix under each failure; `kelpie why <task>` explains why one task is not moving.

### Hermes

On the Hermes host:

```bash
pip install kelpie-sre
mkdir -p ~/.hermes/plugins/kelpie            # the hooks plugin: tool calls -> Kelpie
cp "$(python -c 'import kelpie.adapters.hermes.plugin as m; print(m.__file__)')" \
   ~/.hermes/plugins/kelpie/__init__.py
```

Add its `plugin.yaml` and enable it in Hermes as in
[step 3 of the Hermes quickstart](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-hermes.md#3-hooks-plugin-tool-calls-from-hermes), then:

```toml
# kelpie.toml
[adapters.hermes]
db_path = "~/.hermes/kanban.db"
spool = "~/.hermes/kelpie/spool.jsonl"

# Hermes heartbeats can be up to an hour old (Kelpie's default is 30 minutes)
[engine.rules.exit_contract]
session_stale_after = "60m"
[engine.rules.orphan_status]
session_stale_after = "60m"
[engine.rules.wait_condition]
session_stale_after = "60m"
```

```bash
kelpie serve      # control loop; /metrics and /healthz on 127.0.0.1:9464
kelpie doctor     # in a second shell
kelpie report     # yesterday's cost-of-chaos report
```

OTLP traces, Prometheus and Grafana: the [Hermes quickstart](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-hermes.md).

### Claude Code

On the host that runs Claude Code, with every session of the farm sharing one task list
(`export CLAUDE_CODE_TASK_LIST_ID=farm`):

```bash
pip install kelpie-sre
mkdir -p -m 700 ~/.claude/kelpie
cp "$(python -c 'import kelpie.adapters.claude_code.hook as m; print(m.__file__)')" \
   ~/.claude/kelpie/hook.py
```

Register the hook in `~/.claude/settings.json` as in
[step 4 of the Claude Code quickstart](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-claude-code.md#4-the-hook), then:

```toml
# kelpie.toml
[adapters.claude_code]
task_lists = ["farm"]
spool = "~/.claude/kelpie/spool.jsonl"
```

```bash
kelpie serve
kelpie doctor
```

Claude Code's own OpenTelemetry traces: the [Claude Code quickstart](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-claude-code.md).

### Any agent that sends OpenTelemetry traces

`kelpie serve` accepts OTLP traces (OpenTelemetry GenAI conventions) on `POST /v1/traces`.
With traces alone, the loop and budget breaker, budgets and the token sections of the
daily report work for any platform; rules about the task board need a platform adapter. See [Traces](https://github.com/ch8er/kelpie/blob/main/docs/guide.md#traces-otlp).

### OpenClaw

On the Gateway host, as the Gateway's user (the Workboard plugin gives Kelpie tasks; without
it Kelpie watches sessions, which is enough to stop runaway loops):

```bash
pip install kelpie-sre
mkdir -p ~/.openclaw/kelpie/plugin && chmod 700 ~/.openclaw/kelpie
(cd "$(python -c 'import kelpie.adapters.openclaw.plugin as m, os; print(os.path.dirname(m.__file__))')" &&
  install -m 0644 index.js kelpie_hook.py openclaw.plugin.json package.json ~/.openclaw/kelpie/plugin/)
openclaw plugins install --link ~/.openclaw/kelpie/plugin --force --accept-capabilities
openclaw plugins enable kelpie
openclaw config set plugins.entries.kelpie.hooks.allowConversationAccess true
openclaw config set plugins.entries.kelpie.config.python "$(command -v python3)"
openclaw gateway restart
```

```toml
# kelpie.toml
[adapters.openclaw]
state_dir = "~/.openclaw"
cli = "/usr/local/bin/openclaw"   # the output of `command -v openclaw`, absolute
```

```bash
kelpie serve
kelpie doctor
```

Stopping runaway loops (OpenClaw has no loop guard by default): the
[OpenClaw quickstart](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-openclaw.md).

Other platforms: [CONTRIBUTING](https://github.com/ch8er/kelpie/blob/main/CONTRIBUTING.md)
explains how to add an adapter.

### Docker

From a clone of the repository:

```bash
git clone https://github.com/ch8er/kelpie.git && cd kelpie
cp .env.example .env   # set GRAFANA_ADMIN_PASSWORD
docker compose up      # Kelpie, Prometheus and Grafana with Kelpie's dashboard
```

## How it works

- **A snapshot, then rules.** Every 30 seconds Kelpie builds one immutable snapshot of the
  farm (tasks, owners, dependencies, conditions, sessions, spans, resources) and runs every
  rule over it. Rules are pure functions: the same snapshot gives the same actions.
- **Actions go through the platform.** Requeue, unblock, wake, comment, stop, cancel a
  build, escalate: each goes to the adapter or connector that can carry it out, through a
  ledger with cooldowns and rate limits, so Kelpie never loops on its own output. What a
  platform cannot do becomes an alert.
- **Observe, suggest, act.** Each rule has a mode, and a global dry-run overrides them all.
  The [act checklist](https://github.com/ch8er/kelpie/blob/main/docs/act-checklist.md) is the gate before letting a rule act.
- **Wait for conditions, not in sessions.** An agent registers what it waits for (CI green,
  PR merged, quota reset, a human's task) and exits; Kelpie wakes the task when it holds.
- **A daily report.** Hours and tokens lost to chaos, the biggest wait by cause with a
  suggestion, what Kelpie fixed and would have fixed, what needs a human, farm throughput
  per role, the top 3 recommendations, and the before/after effect of recent fixes. An LLM
  root-cause section is optional and off by default.
- **Use what exists.** Host metrics come from node_exporter and Prometheus, dashboards from
  Grafana, and traces can be forwarded to Langfuse, Phoenix or Datadog. Kelpie exposes
  `/metrics`.

The rules:

| Rule | Catches |
| --- | --- |
| `exit_contract` | a session that ended without a final status |
| `wait_condition` | waits that can be woken, can never be met, or poll inside a session |
| `orphan_status` | tasks with no owner, parents left open, cards stuck before work |
| `stale_blocks` | blocks whose cause is gone |
| `ping_pong` | review rounds past a limit |
| `bottlenecks` | idle agents with ready work, forgotten holds, human tasks on the critical path |
| `resources` | disk, memory, load, provider quotas and logins, before they stop agents |
| `ci_economy` | wasted and superseded CI builds, CI quota burn |
| `loop_breaker` | identical tool calls, error streaks, token budgets |
| `no_progress` | tokens spent with no status change, commit or artifact |
| `conflict_warning` | two live tasks editing the same files |
| `dispatch_gate` | ready tasks whose prerequisite is not met |
| `needs_you` | everything waiting for a person, per person, by cost of delay |
| `checkpoints` | long runs without a commit, and resuming from the last one |
| `return_causes` | why tasks come back from review |
| `output_offenders` | commands whose output floods a role's context day after day |

The incidents behind each rule, the design and the daily report in full:
[How Kelpie works](https://github.com/ch8er/kelpie/blob/main/docs/guide.md).

## Documentation

- [How Kelpie works](https://github.com/ch8er/kelpie/blob/main/docs/guide.md): rules, daily report, alerts, traces, budgets
- [Install and run](https://github.com/ch8er/kelpie/blob/main/docs/install.md): uv, Docker, `kelpie doctor`, ports, tokens, data
- [Several machines](https://github.com/ch8er/kelpie/blob/main/docs/deploy.md): `kelpie agent` on each, a central `kelpie serve` (laptop + cloud VM, two VMs)
- [Configuration reference](https://github.com/ch8er/kelpie/blob/main/docs/configuration.md): every section, rule and threshold
- Quickstarts: [Hermes](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-hermes.md), [Claude Code](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-claude-code.md), [OpenClaw](https://github.com/ch8er/kelpie/blob/main/docs/quickstart-openclaw.md);
  the [observe-to-act checklist](https://github.com/ch8er/kelpie/blob/main/docs/act-checklist.md)
- [Privacy](https://github.com/ch8er/kelpie/blob/main/docs/privacy.md): what Kelpie stores from traces and hooks, and how to keep less
- [Extending Kelpie](https://github.com/ch8er/kelpie/blob/main/docs/extending.md): rules, adapters, connectors, report sections
- [Changelog](https://github.com/ch8er/kelpie/blob/main/CHANGELOG.md) · [Roadmap](https://github.com/ch8er/kelpie/blob/main/docs/roadmap.md) · [Plan](https://github.com/ch8er/kelpie/blob/main/docs/plan.md) ·
  [Releasing](https://github.com/ch8er/kelpie/blob/main/docs/release.md)

## Contributing

Incidents from your farm are the most useful contribution: each one becomes a synthetic
fixture and a rule test. [CONTRIBUTING.md](https://github.com/ch8er/kelpie/blob/main/CONTRIBUTING.md) covers incidents, rules and
adapters. Security issues: [SECURITY.md](https://github.com/ch8er/kelpie/blob/main/SECURITY.md).

## License

Apache License 2.0, see [LICENSE](https://github.com/ch8er/kelpie/blob/main/LICENSE). PyPI: `kelpie-sre` · CLI: `kelpie`.
