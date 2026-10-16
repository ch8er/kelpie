# Configuration reference

Every section and key Kelpie reads from `kelpie.toml`, with defaults and units. The code is
the source of truth: when this page and the code disagree, the code wins and this page is a
bug. [`kelpie.example.toml`](../kelpie.example.toml) has the same settings as a commented
file to copy.

Scope and priorities of the rules are in [plan.md](plan.md); this page only says how to
configure what exists.

## Contents

- [How the config is read](#how-the-config-is-read)
- [Environment variables](#environment-variables)
- [[engine]](#engine)
- [Modes and dry-run](#modes-and-dry-run)
- [Rules](#rules): [checkpoints](#checkpoints), [exit_contract](#1-exit_contract),
  [wait_condition](#2-wait_condition), [orphan_status](#3-orphan_status),
  [stale_blocks](#4-stale_blocks), [ping_pong](#5-ping_pong),
  [bottlenecks](#6-bottlenecks), [resources](#7-resources),
  [ci_economy](#9-ci_economy), [loop_breaker](#10-loop_breaker),
  [no_progress](#11-no_progress), [conflict_warning](#12-conflict_warning),
  [dispatch_gate](#dispatch_gate)
- [[storage]](#storage)
- [[adapters.hermes]](#adaptershermes)
- [[traces]](#traces), [[[traces.forward]]](#tracesforward),
  [[traces.outputs]](#tracesoutputs)
- [[[budgets]] and [prices]](#budgets-and-prices)
- [[resources.prometheus]](#resourcesprometheus)
- [[connectors.github]](#connectorsgithub), [[connectors.github.ci]](#connectorsgithubci),
  [[[block_conditions]]](#block_conditions)
- [[connectors.human_tasks]](#connectorshuman_tasks)
- [[connectors.llm_quotas]](#connectorsllm_quotas)
- [[alerts]](#alerts)
- [[report]](#report)
- [Checks across sections](#checks-across-sections)

## How the config is read

- **File.** `--config PATH`, else `$KELPIE_CONFIG`, else `./kelpie.toml` if it exists,
  else defaults only. Read once at startup. `--config` goes before the command:
  `kelpie --config kelpie.toml status`.
- **Top-level sections:** `engine`, `storage`, `adapters`, `traces`, `budgets`, `prices`,
  `resources`, `connectors`, `block_conditions`, `alerts`, `report`. Anything else stops
  Kelpie at startup.
- **Unknown keys** in any table stop Kelpie at startup with the key named. Rule names are
  checked against the plan's rule list (`kelpie.rules.PLANNED`).
- **Optional.** Every key is optional unless marked *required*. A section that is absent is
  off, except `[engine]`, `[storage]` and `[traces]`, which have working defaults.
- **Secrets** are never written in the file. Keys ending in `_env` name an environment
  variable that holds the secret. Kelpie never logs or shows the value.
- **Check the result** with `kelpie status`; it prints what `kelpie serve` would load.

### Units

| Kind | Syntax | Examples |
| --- | --- | --- |
| Duration | A string of `<n>d`, `<n>h`, `<n>m`, `<n>s` parts in that order, each optional; or a bare number of seconds | `"30m"`, `"1h30m"`, `"7d"`, `"90s"`, `45` |
| Ratio | A number from 0 to 1: used / limit | `0.85` |
| Load | load1 divided by CPU cores; may exceed 1 | `1.5` |
| Money | US dollars | `50` |
| Price | US dollars per 1 million tokens | `3` |
| Glob | `fnmatch` pattern, case-sensitive; `*` also matches `/` | `"docs/*"`, `"WEB-*"` |
| Pattern | Python regular expression, case-insensitive | `'waiting for merge of (?P<pr>\S+)'` |

`[traces.outputs] window` and `[report] verify_window` take the string form only.

## Environment variables

| Variable | Read by | Meaning |
| --- | --- | --- |
| `KELPIE_CONFIG` | all commands | Config file path. |
| `KELPIE_DB` | all commands | Database path; overrides `[storage] path`. |
| `KELPIE_HOST`, `KELPIE_PORT` | `kelpie serve` | Bind address and port (default `127.0.0.1`, `9464`). |
| `KELPIE_LOG_LEVEL` | all commands | Log level (default `INFO`). |
| `KELPIE_TRACES_TOKEN` | `kelpie serve` | OTLP bearer token (the default `[traces] token_env`). |
| `KELPIE_TRACES_ALLOW_UNAUTHENTICATED` | `kelpie serve` | `1`, `true`, `yes` or `on`: same as `[traces] allow_unauthenticated = true`. |
| `KELPIE_PROMETHEUS_TOKEN` | `kelpie serve` | Prometheus bearer token (the default `[resources.prometheus] token_env`). |
| `KELPIE_PROMETHEUS_URL` | `kelpie doctor` | Prometheus URL for the doctor check. |
| `GITHUB_TOKEN` | `kelpie serve` | GitHub token (the default `[connectors.github] token_env`). |
| `KELPIE_HERMES_SPOOL` | Hermes plugin | Spool path on the Hermes host. |
| `KELPIE_HERMES_SPOOL_COMMANDS` | Hermes plugin | `0`, `false`, `no` or `off`: record no command prefix. |
| `KELPIE_HERMES_SPOOL_MAX_BYTES` | Hermes plugin | Spool size that triggers rotation (default 20 MiB). |

Variables named by `token_env`, `webhook_env`, `headers_env` and `cancel_token_env` keys are
read as configured.

## [engine]

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `interval` | `"30s"` | duration, > 0 | How often the control loop ticks. |
| `dry_run` | `true` | bool | Global override. When true every rule behaves as in `observe`: nothing leaves Kelpie. |
| `default_mode` | `"observe"` | mode | Mode of rules without their own `mode`. |
| `cooldown` | `"30m"` | duration, >= 0 | The same action (same fingerprint) is not repeated within this window. |
| `retry_after` | `"5m"` | duration, >= 0 | A failed action may be retried after this, inside the cooldown. |
| `max_actions_per_task_per_hour` | `6` | count, >= 1 | Actions with an effect (suggested, alerted, executed, not executed, failed) per subject per hour: the task, else the session, else the rule. Further actions are suppressed and one warning is sent. |
| `once_window` | `"7d"` | duration, > 0 | One-off actions (WIP commit, resume note, conflict comment) are carried out once per fingerprint within this window. |
| `once_attempts` | `3` | count, >= 1 | A one-off action is given up after this many failed attempts. |
| `state_escalations` | `["4h", "12h", "24h"]` | list of durations, increasing | Alerts about a lasting condition are sent again when its age passes each of these. |
| `state_resolve_after` | `"10m"` | duration, >= 0 | A lasting condition no rule reported for this long is resolved, and a short "resolved" alert goes out. |
| `rules` | `{}` | tables | One table per rule, `[engine.rules.<name>]`; see [Rules](#rules). |

## Modes and dry-run

Each rule has a mode: `mode = "observe" | "suggest" | "act"` in its
`[engine.rules.<name>]` table, else `default_mode`. `dry_run = true` overrides every mode.
Every action goes through the action ledger first (cooldown, rate limit, once-actions), and
every outcome is recorded there and in `kelpie_actions_total`.

| Action | `dry_run = true` | `observe` | `suggest` | `act` |
| --- | --- | --- | --- | --- |
| `alert`, `escalate` | logged (`dry_run`) | logged (`observed`) | sent to alert channels (`alerted`) | sent to alert channels (`alerted`) |
| any other action | logged (`dry_run`) | logged (`observed`) | sent to alert channels as a suggestion (`suggested`) | carried out by its executor (`executed`); if no executor can, sent as an alert marked "not executed, needs a human" (`not_executed`) |

- Without `[[alerts.channels]]`, anything that should reach a human is logged and recorded
  as `dropped`.
- For rules that only alert or escalate (`ping_pong`, `bottlenecks`, `resources`,
  `no_progress`), `suggest` and `act` behave the same.
- **Routing in act mode.** An action goes to `action.executor` when the rule sets it (the
  CI connector for `cancel_build`), else to the platform adapter named in its task's
  `source`, else to the only platform adapter if there is exactly one.
- **What can act today.** The Hermes adapter carries out `stop` only. With Hermes as the only
  adapter and `dry_run = false`, Kelpie refuses to start when a rule other than
  `loop_breaker` is in `act`, except `ci_economy` when `[connectors.github.ci] allow_cancel`
  is set. See the [act checklist](act-checklist.md).

## Rules

Each rule is configured in `[engine.rules.<name>]`. Every table accepts `mode`; the other
keys are the rule's own parameters. A table for a planned rule that is not implemented yet
is accepted and ignored with a warning. Rules run in the order below.

| # | Name | Priority | Status | Actions | Act needs |
| --- | --- | --- | --- | --- | --- |
| – | `checkpoints` | P0 | implemented | `comment`, `wip_commit` | adapter `comment`, `wip_commit` (not in Hermes yet) |
| 1 | `exit_contract` | P0 | implemented | `requeue`, `alert` | adapter `requeue` (not in Hermes yet) |
| 2 | `wait_condition` | P0 | implemented | `wake`, `comment`, `alert` | adapter `requeue` (a wake puts the task back in the ready queue), `comment` (not in Hermes yet) |
| 3 | `orphan_status` | P0 | implemented | `alert`, `reassign` | adapter with `SupportsReassign` (not in Hermes yet) |
| 4 | `stale_blocks` | P0 | implemented | `unblock`, `alert` | adapter `unblock` (not in Hermes yet) |
| 5 | `ping_pong` | P0 | implemented | `escalate` | alert channel |
| 6 | `bottlenecks` | P0 | implemented | `alert`, `escalate` | alert channel |
| 7 | `resources` | P0 | implemented | `alert` | alert channel |
| 8 | daily report | P0 | `kelpie report` | — | configured in [[report]](#report), not here |
| 9 | `ci_economy` | P0 | implemented | `cancel_build`, `alert` | `[connectors.github.ci] allow_cancel` |
| 10 | `loop_breaker` | P0 | implemented | `stop`, `alert` | adapter with `SupportsStop` (Hermes has it) |
| 11 | `no_progress` | P0 | implemented | `alert` | alert channel |
| 12 | `conflict_warning` | P0 | implemented | `comment`, `wait`, `wake` | adapter `comment`; `SupportsWait` for `serialize` (not in Hermes yet) |
| – | `dispatch_gate` | P0 | implemented | `wait`, `wake` | adapter with `SupportsWait` (not in Hermes yet) |
| 13 | `needs_you` | P1 | not implemented | | |
| 14 | `compaction_gate` | P2 | not implemented | | |
| – | `return_causes` | P1 | not implemented | | |

Several rules share these terms:

- **Live session.** A session that has not ended and whose last heartbeat is younger than
  `session_stale_after` (default 30m). A session silent for longer counts as ended. Hermes
  tolerates heartbeats up to an hour old, so set `session_stale_after = "60m"` for
  `exit_contract`, `orphan_status`, `wait_condition` and `checkpoints` on Hermes.
- **Fresh condition.** A condition checked after the wait or block began and within
  `max_condition_age` (default 15m). Rules only act on fresh evidence.
- **Lasting-state alert.** An alert about a condition that lasts (an ownerless task, a
  forgotten hold). It is sent when the condition starts, again at `[engine]
  state_escalations` or when it gets worse, and once more when it is resolved.

### checkpoints

Commit reminders for live sessions, a WIP commit when a session exits with uncommitted
work, and a resume note for the next session. Needs git state from the adapter (Hermes:
`git_state = true`).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `commit_every` | `"30m"` | duration, > 0 | Remind a live session with uncommitted changes when its last commit (or its start) is older than this. |
| `max_changed_files` | `20` | count, >= 1 | ...or when it has more changed files than this. |
| `wip_ci` | `"skip_ci"` | `skip_ci` \| `draft_pr` | How the WIP commit avoids CI: `[skip ci]` in the message, or a push to a draft PR. |
| `session_stale_after` | `"30m"` | duration, > 0 | See [live session](#rules). |
| `act_within` | `"24h"` | duration, > 0 | WIP commit and resume note only this long after the exit. Keep it shorter than `[engine] once_window`. |

### 1. exit_contract

A session ended without a final status, or left a block without an allowed reason or a
condition: the task goes back to the queue. Also alerts when a declared final status was
never applied by the platform.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `allowed_block_reasons` | `["guard", "waiting_ci", "waiting_pr_merge", "waiting_human", "quota"]` | list of strings | A block is allowed when its reason equals one of these, or starts with one followed by `:` or a space; case-insensitive. Replaces the default list. A block tied to a condition is always allowed. |
| `session_exit_grace` | `"10m"` | duration | Time the platform gets to record a status after an exit before Kelpie judges it. |
| `session_stale_after` | `"30m"` | duration | See [live session](#rules). |

### 2. wait_condition

Wakes a waiting task when every condition of its current wait holds on fresh evidence;
alerts when a wait can never be met, runs past `max_wait`, or has no condition; comments on
sessions that poll a known check instead of registering a condition and exiting.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `max_wait` | `"24h"` | duration, > 0 | Alert when a task has waited this long. |
| `no_condition_grace` | `"10m"` | duration, > 0 | Alert about a waiting task without a visible condition only after this. |
| `max_condition_age` | `"15m"` | duration, > 0 | See [fresh condition](#rules). |
| `poll_patterns` | `{}` | table: condition kind -> list of substrings | Tool calls whose command (or arguments hash) contains a substring count as polling for that condition kind, e.g. `ci_status = ["gh pr checks"]`. Empty: polling detection is off. |
| `poll_min_repeats` | `8` | count, >= 1 | Comment when the same polled check ran this many times within `poll_window`. |
| `poll_window` | `"10m"` | duration, > 0 | Window for the repeat count; also how long one check may block before it counts as polling. |
| `poll_error_streak` | `5` | count, >= 1 | Comment when the same polled check failed this many times in a row. |
| `session_stale_after` | `"30m"` | duration, > 0 | See [live session](#rules). |

### 3. orphan_status

A task sits in a status that needs an owner, has none, and no live session works on it.
Alerts (critical when many open tasks depend on it) and proposes an idle agent of its role.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `owned_statuses` | `["in_progress"]` | list of statuses | Statuses judged after `orphan_after`. |
| `orphan_after` | `"30m"` | duration, > 0 | Ownerless time before an alert, for `owned_statuses`. |
| `queue_statuses` | `["review", "accepted"]` | list of statuses | Hand-off queues, judged after `queue_after`. |
| `queue_after` | `"2h"` | duration, > 0 | Ownerless time before an alert, for `queue_statuses`. |
| `critical_dependents` | `3` | count, >= 1 | The alert is critical when at least this many open tasks depend on the task. |
| `reassign` | `true` | bool | Propose a `reassign` to an idle agent of the task's role. |
| `reassign_unknown_owner` | `false` | bool | Also reassign when the owner is missing from the owner directory (it may live on another platform). |
| `session_stale_after` | `"30m"` | duration, > 0 | See [live session](#rules). |
| `reviewer_roles` | `[]` | list of roles | Roles that review. Empty: the review stop pattern is off. |
| `review_idle_after` | `"2h"` | duration, > 0, or `false` | Stop pattern: a review task owned by an agent outside `reviewer_roles`, not in a reviewer role's queue, with no live session and no open condition this long (from the later of the status change and the last session end). |
| `next_action_after` | `"30m"` | duration, > 0, or `false` | Stop pattern: the last session ended declaring `done`, `review` or `accepted`, but the task still sits in another open status (not `in_progress`, which rule 1 reports) this long after. |
| `dependents_not_woken` | `false` | bool | Stop pattern, opt-in: a `done` task whose dependents still sit in `backlog` with every parent finished, `next_action_after` after it finished. Off by default because a backlog also holds work parked on purpose (Hermes maps `triage`, `todo` and `scheduled` to it). |
| `dead_end_chains` | `true` | bool | Stop pattern: a chain of waiting tasks (dependencies, `task_status` and `human_task` conditions) that ends in a task the board does not show, with tasks stuck behind it. Conditions that can never be met or that Kelpie cannot see are rules 2 and 4's alerts, which list the stuck tasks in `params.stuck_ids`. A dependency on a cancelled or failed parent is not detected: a finished parent satisfies a dependency. |
| `max_recovery_depth` | `1` | count, >= 1, or `false` | Stop pattern: recovery tasks whose title names another recovery task as its target, chained deeper than this, or recovery tasks that cycle (through titles, dependencies or conditions). A staged plan of recovery tasks that only follow each other is not a loop. |
| `recovery_labels` | `["recovery"]` | list of labels | Labels that mark a recovery task. |
| `recovery_titles` | `[]` | list of regexes | Titles that mark a recovery task, case-insensitive; a named group `target` names the task it recovers. The only way on Hermes, which has no labels. |
| `hold_labels` | bottlenecks' | list of labels | Tasks with one of these labels are parked on purpose: no stop pattern looks at them. Unset: `bottlenecks` `hold_labels` and `holds` labels. |
| `ignore_tasks` | `[]` | list of ids or globs | Tasks rule 3 never looks at, e.g. `["t_parked*"]`: the escape hatch on boards without labels (Hermes), where holds cannot be expressed. |

Statuses: `in_progress`, `review`, `accepted`. Terminal statuses and `blocked`, `waiting`,
`backlog`, `ready` are refused. Each stop-pattern alert carries `params.pattern`
(`review-undriven`, `no-next-action`, `dependents-not-woken`, `dead-end-chain`,
`recovery-loop`).

### 4. stale_blocks

Clears a block when every linked condition holds on fresh evidence. Alerts on blocks it
cannot clear.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `unblock_grace` | `"5m"` | duration, >= 0 | Wait this long after the last condition was met before unblocking, so the agent or platform can do it first. |
| `max_condition_age` | `"15m"` | duration, > 0 | See [fresh condition](#rules). |
| `unlinked_alert_after` | `"4h"` | duration or `false` | Alert on a block with no condition Kelpie can see after this. `false`: off. |
| `max_block` | `"8h"` | duration or `false` | Alert when a linked condition is still unmet after this. `false`: off. |
| `stale_evidence_after` | `"1h"` | duration or `false` | Alert when the connector has not checked a linked condition for this long. `false`: off. |

A condition that can never be met (a PR closed without merging, a human task cancelled)
alerts at once.

### 5. ping_pong

After a number of review rounds, escalates the task with the history of return reasons.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `max_review_rounds` | `3` | count, >= 1 | Escalate at this many rounds: the larger of the adapter's round counter and the returns in `review_categories`. Announced again each further `max_review_rounds` rounds. |
| `escalate_to` | first human owner by id | owner id or role | Who settles it. Set it explicitly: `[adapters.hermes] human_owners` changes the default. |
| `review_categories` | `["review"]` | list of strings | Return categories that count as review rounds. |

### 6. bottlenecks

Finds wasted capacity: a sequential chain with idle agents, an overloaded role next to idle
ones, idle agents while ready work waits, holds past their expiry, and human tasks that
gate agent work.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `min_chain_length` | `4` | count, >= 1 | Critical path length (tasks) for the sequential-chain alert. |
| `min_idle_agents` | `2` | count, >= 1 | Idle agents of the chain's roles needed for that alert. |
| `min_queue_depth` | `3` | count, >= 1 | Queued ownerless tasks of a role for the overloaded-role alert. |
| `min_idle_time` | `"30m"` | duration, >= 0 | An agent counts as idle this long after its last session. |
| `idle_alert_after` | `"30m"` | duration, >= 0 | Alert about idle agents once ready work they could take has waited this long. |
| `slots` | unset | count, >= 1 | Farm concurrency limit. When this many agents have a live session, idle agents are not free capacity. |
| `hold_labels` | `[]` | list of labels | Labels that park tasks on purpose. Held tasks do not count as ready work. No expiry unless `hold_expiry` is set. |
| `holds` | `[]` | array of tables | Holds with an owner and an expiry, see below. |
| `hold_expiry` | unset | duration, > 0 | Expected length of a hold without its own `expires_after`. Unset: 4h for `holds` entries, no expiry for `hold_labels`. |
| `human_gate_min_tasks` | `3` | count, >= 1 | Escalate a human task with at least this many agent tasks behind it... |
| `human_gate_after` | `"1h"` | duration, >= 0 | ...once it has been ready or in progress this long. |

`[[engine.rules.bottlenecks.holds]]` entries:

| Key | Default | Meaning |
| --- | --- | --- |
| `label` | *required* | The label that marks held tasks. Unique. |
| `owner` | unset | Who owns the hold; named in the alert. |
| `expires_after` | `hold_expiry`, else `"4h"` | Expected length. A hold's age is how long its oldest queued task has not moved. |

### 7. resources

Warns before a host runs out of disk, memory, CPU, load or processes; suggests a limit on
heavy tasks when a host is under pressure; alerts on LLM provider quotas, limit hits and
credentials. Needs readings from [[resources.prometheus]](#resourcesprometheus) and
[[connectors.llm_quotas]](#connectorsllm_quotas).

Host thresholds (any can be `false` to turn it off; warning must be below critical):

| Key | Default | Unit |
| --- | --- | --- |
| `disk_warning`, `disk_critical` | `0.85`, `0.95` | ratio |
| `memory_warning`, `memory_critical` | `0.90`, `0.97` | ratio |
| `load_warning`, `load_critical` | `1.5`, `3.0` | load per core |
| `cpu_warning`, `cpu_critical` | off, off | ratio |
| `processes_warning`, `processes_critical` | `0.80`, `0.95` | ratio of the process limit |

The default process query divides by the kernel's `pid_max`, so process alerts rarely fire;
set `[resources.prometheus.queries] processes_max` to a real ceiling.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `heavy_label`, `heavy_labels` | `"heavy"` | string or list | Labels that mark heavy tasks. Both keys add up and replace the default. |
| `heavy_roles` | `[]` | string or list | Roles whose tasks are heavy. |
| `max_heavy_per_host` | unset | count, >= 1, or `false` | Alert when a host under pressure runs more heavy tasks than this. Unset: not checked. Needs `Task.host` from the adapter (Hermes does not set it yet). |
| `max_reading_age` | `"10m"` | duration, > 0 | Readings older than this are ignored. |
| `quota_warning` | `0.1` | ratio or `false` | Warn when `remaining / limit` of a provider quota is at or below this. |
| `rate_limit_events_warning` | `20` | count or `false` | Warn at this many limit hits per provider... |
| `rate_limit_window` | `"24h"` | duration, > 0 | ...within this window. |
| `ignore_resets_within` | `"10m"` | duration, > 0 | Quota windows that refill this soon are not reported as low or exhausted. |
| `auth_expiry_warning` | `"72h"` | duration, > 0 | Warn this long before a credential expires. Expired or rejected keys are critical. |
| `provider_roles` | `{}` | table: provider -> roles | Which roles use which provider. |
| `provider_owners` | `{}` | table: provider -> owner ids | Which agents use which provider. |

An exhausted quota is critical when ready or running tasks use the provider according to
`provider_roles` or `provider_owners`. Without a mapping the alert stays a warning and names
no tasks.

### 9. ci_economy

Finds CI builds that burned minutes for nothing, cancels superseded builds that still run,
and forecasts the CI minutes quota. Needs builds from
[[connectors.github.ci]](#connectorsgithubci).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `window` | `"24h"` | duration, > 0 | Builds looked at, and the burn-rate window. Must not exceed `[connectors.github.ci] window`. |
| `docs_globs` | `["docs/*", "*.md", "*.rst"]` | list of globs | A build is docs-only when every changed path matches. |
| `wip_patterns` | `['^\s*\[?wip\b', '^\s*(fixup\|squash)!']` | list of patterns | Commit messages that mark a WIP commit. |
| `waste_min_minutes` | `30` | minutes, >= 0 | Send the waste summary once this many minutes were wasted in the window. |
| `cancel_superseded` | `true` | bool | Emit `cancel_build` for superseded builds the connector marked cancellable. |
| `quota_critical` | `1.0` | ratio, > 0 and <= 1 | Used / quota at which the quota counts as exhausted. |
| `forecast_horizon` | `"7d"` | duration, > 0 | Warn when the quota runs out within this, before it resets. |
| `quota_period` | `"30d"` | duration, > 0 | Without a usage reading: warn when the burn over this period would exceed the quota. |
| `max_reading_age` | `"1h"` | duration, > 0 | Quota readings older than this are ignored. |

Waste categories, each build counted once in this order: superseded, already verified
commit, docs-only, draft, WIP commit.

### 10. loop_breaker

Stops runaway runs from traces: identical consecutive tool calls, error streaks, and hard
budgets. Info alert when a budget or a subscription limit is near.

Thresholds (also allowed in `agents` and `roles` tables):

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `identical_calls` | `8` | count, >= 1 | Consecutive tool calls with the same tool and arguments hash... |
| `identical_window` | `"10m"` | duration, > 0 | ...the last ones within this window. |
| `consecutive_errors` | `5` | count, >= 1 | Failed tool calls in a row... |
| `error_window` | `"10m"` | duration, > 0 | ...the last one at most this long ago... |
| `min_error_span` | `"60s"` | duration, > 0 | ...and the streak at least this long from first to last error. |
| `poll_comment_after` | `8` | count, >= 1 | Polling calls (matching `poll_patterns`) that rule 2 coaches at this count. Keep it equal to `wait_condition` `poll_min_repeats`. |
| `poll_grace` | `"15m"` | duration, > 0 | Stop polling only this long after that point... |
| `poll_hard_limit` | `30` | count, >= `identical_calls` | ...or at this many identical polling calls. |

Other keys:

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `poll_patterns` | `wait_condition`'s | table | Same shape as in `wait_condition`. Taken from there unless set here; a warning is logged when both are set and differ. |
| `stop_on_shared_budgets` | `false` | bool | Hard `day` and `project` budgets only alert. `true`: they also stop live sessions that spent in the window. Hard `task` and `agent` budgets always stop (in `act` mode). |
| `forecast_horizon` | `"2h"` | duration, > 0 | Info alert when a budget, or a subscription's next provider limit, is this near. |
| `session_stale_after` | `"30m"` | duration, > 0 | Only live sessions are stopped. |
| `agents.<owner id>` | `{}` | table of thresholds | Per-agent overrides. Wins over `roles`. |
| `roles.<role>` | `{}` | table of thresholds | Per-role overrides. |

```toml
[engine.rules.loop_breaker.agents.agent-ops-1]
identical_calls = 20
[engine.rules.loop_breaker.roles.maintenance]
consecutive_errors = 8
```

### 11. no_progress

Alerts when a session spends tokens without a status change, commit or artifact, with
recommendations.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `window` | `"30m"` | duration, > 0 | Tokens and progress are compared over this sliding window. `[traces] window` must be at least this long (Kelpie warns otherwise). |
| `min_tokens_without_progress` | `100000` | tokens, >= 1 | Alert at this many input plus output tokens with no progress. |
| `claim_grace` | `"2m"` | duration, >= 0 | A status change this soon after the session start is the claim, not progress. |

### 12. conflict_warning

Two live tasks edit the same files: each gets one comment naming the other and the shared
files. Needs an adapter that reports `Task.files` (Hermes does not yet).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `expected_shared` | `[]` | list of globs | Files many tasks are meant to edit, such as a changelog. Ignored when comparing. |
| `serialize` | `false` | bool | Also park the later task in `waiting` until the earlier ones are done. Needs an adapter with `SupportsWait` and `wait_condition` in `act` mode to wake it (Kelpie warns otherwise). |

### dispatch_gate

A ready task whose prerequisite is not met is parked in `waiting` on it instead of being
picked up; rule 2 wakes it when the prerequisite holds. Prerequisites are conditions linked
to the task, including PR references in its title through `[[block_conditions]]` with
`applies_to = ["ready_task"]`.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `max_condition_age` | `"15m"` | duration, > 0 | Only an unmet condition checked this recently gates a task. |
| `gate_on_parents` | `false` | bool | Also park ready tasks whose parent tasks are not done. For dispatchers that ignore dependencies; it hides rule 6's chain alert. |

In `act` mode the gate needs an adapter with `SupportsWait` and `wait_condition` in `act`
mode (Kelpie warns otherwise).

## [storage]

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | `"kelpie.db"` | SQLite database: action ledger, alert states, spans, recorded fixes. `":memory:"` keeps it in memory. `$KELPIE_DB` overrides it. |

## [adapters.hermes]

The Hermes platform adapter. Absent: no platform, and snapshots hold no tasks. Details: the
[Hermes adapter README](../src/kelpie/adapters/hermes/README.md).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `db_path` | `"~/.hermes/kanban.db"` | path | The board. Named boards: `~/.hermes/kanban/boards/<slug>/kanban.db`. Opened read-only. |
| `spool` | `"~/.hermes/kelpie/spool.jsonl"` | path or `false` | Spool written by the hooks plugin. `false`: no hook data. |
| `lookback` | `"24h"` | duration, > 0 | Runs, spans and finished tasks older than this are left out of the rules' view. The daily report looks further back on its own (see [[report]](#report)). |
| `enabled` | `true` | bool | `false` keeps the section but does not build the adapter. |
| `git_state` | `true` | bool | Read git state (commit, branch, changed files) of live task worktrees. Needs `git` and the agents' user. |
| `git_timeout` | `"5s"` | duration, > 0 | Limit for each git call. |
| `cli` | `"hermes"` | name or path | Hermes executable used for `stop`. |
| `cli_timeout` | `"10s"` | duration, > 0 and <= 60s | Limit for one `stop` command; it runs inside the tick. |
| `human_owners` | `[]` | list of names | Assignees that are people, not agents; case-insensitive. Their tasks get a human owner, which `[connectors.human_tasks]` needs. Also changes `ping_pong`'s default escalation target and who `bottlenecks` and `orphan_status` count as agents. |

## [traces]

OTLP/HTTP trace ingest on `POST /v1/traces`, on the same port as `/metrics`. On by default.
What is kept from each span: [privacy.md](privacy.md).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `enabled` | `true` | bool | `false`: no ingest, no span store, no budgets. |
| `token_env` | `"KELPIE_TRACES_TOKEN"` | variable name | Bearer token exporters must send. Used when the variable is set. Written here, the variable must be set or Kelpie refuses to start. |
| `allow_unauthenticated` | `false` | bool | Accept spans without a token on a non-loopback address. See [install.md](install.md#otlp-token-and-the-exposure-check). |
| `window` | `"30m"` | duration, > 0 | Stored spans each tick sees. |
| `totals_window` | `"24h"` | duration, > 0 | Per-session totals (calls, errors, tokens) in each tick. |
| `retention` | `"7d"` | duration, > 0, <= 3650d | Spans older than this are pruned, hourly. Must be at least `window` and `totals_window`. |
| `max_rows` | `1000000` | count, >= 1 | ...and the oldest beyond this row count. |
| `max_body_bytes` | `8388608` | bytes, >= 1 | Larger requests (also after gunzip) get 413. |
| `max_snapshot_spans` | `50000` | count, >= 1 | At most this many stored spans per tick, newest first. |
| `max_concurrent_requests` | `4` | count, >= 1 | Requests decoded at once; more get 503 and are retried by the exporter. |
| `task_attribute` | `"kelpie.task_id"` | attribute name | Span or resource attribute naming the task. |
| `session_attribute` | `"kelpie.session_id"` | attribute name | Attribute naming the session; else `gen_ai.conversation.id`, else `session.id`. |
| `commands` | `true` | bool | Keep a grouping prefix of shell commands on tool spans (`kelpie.tool.command_prefix`). `false`: drop it. |

### [[traces.forward]]

Forwards every accepted OTLP request, unchanged, to another backend. One table per
destination. Needs `[traces]` enabled. The global dry-run does not stop forwarding.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `endpoint` | *required* | http(s) URL | Destination. No credentials in the URL. |
| `name` | endpoint host | string | Metric label. Unique. |
| `headers` | `{}` | table | Static, non-secret headers. |
| `headers_env` | `{}` | table: header -> variable name | Secret headers read from the environment. A missing variable stops Kelpie at startup. |
| `timeout` | `"10s"` | duration, > 0 | Per request. |
| `queue_size` | `1000` | requests, >= 1 | Requests waiting per destination; beyond that they are dropped. |
| `queue_bytes` | `67108864` | bytes, >= 1 | Bytes waiting per destination (64 MiB). |
| `retries` | `3` | count, 0 to 10 | Retries on network errors, 429 and 5xx. |

Examples for Langfuse, Phoenix and Datadog are in the README, "Forwarding".

### [traces.outputs]

Huge tool outputs: tool calls ranked by output tokens per role and command prefix, with a
filter recommendation, read by the daily report. Only the size of an output is recorded.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `threshold_tokens` | `4000` | tokens, >= 1 | Recommend a filter when calls average at least this... |
| `min_calls` | `3` | count, >= 1 | ...over at least this many unfiltered calls. |
| `repeat_days` | `2` | days, >= 1 | Above the threshold on this many days: a repeated offender. |
| `window` | `"1d"` | duration string, > 0 | Each side of a before/after comparison. |
| `min_sample` | `5` | count, >= 1 | Fewer calls on a side: labelled a small sample. |
| `owners` | `{}` | table: role -> owner | Who owns each role's instructions; named for repeated offenders. |
| `filters` | built-in table | array of tables | Replaces the built-in filter table as a whole; first match wins. |

With `[traces.outputs]` present, `[traces] retention` must cover `repeat_days` and twice
`window`.

`[[traces.outputs.filters]]` entries (built-in: `rtk`, `xcbeautify`, `errors-only`,
`range`):

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | *required* | Filter name. |
| `how` | *required* | One line telling the operator how to apply it. |
| `commands` | *required* | Globs over the command prefix or executable; `*` is any shell command, `tool:*` any other tool. |
| `reduction` | *required* | Expected share of output tokens removed, between 0 and 1 (exclusive). |
| `platforms` | any | Agent platforms the filter works with, e.g. `["hermes", "claude-code"]`. |

## [[budgets]] and [prices]

Spend is priced from traces, so budgets need `[traces]` enabled.

`[[budgets]]` entries:

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `name` | *required* | string | Unique. |
| `scope` | *required* | `day` \| `agent` \| `task` \| `project` | `day`: the whole farm. `agent`: the session's owner. `task`: the span's task. `project`: tasks matched by `match`. |
| `unit` | *required* | `usd` \| `tokens` | `usd` needs a `[prices]` table. |
| `soft` | unset | number, > 0 | Alert at this spend. |
| `hard` | unset | number, > `soft` | Stop the run at this spend (act mode, through `loop_breaker`): `agent` and `task` budgets; `day` and `project` budgets only alert unless `loop_breaker` has `stop_on_shared_budgets = true`. At least one of `soft` and `hard` is required. |
| `match` | unset | string | `project`: `label:<name>` or a task id glob (*required*); all matching tasks share one budget. `agent`/`task`: limits the budget to the owners or tasks it names (an id or a glob); each matching agent or task still has its own budget, so `match = "agent-*"`, `hard = 50` is $50 per agent, not in total. Without it every agent or task has one. Not allowed for `day`. |
| `window` | `"24h"`; `task`: `[traces] retention` | duration, > 0 | Rolling window. Must not exceed `[traces] retention`. |
| `burn_window` | `"1h"`, or `window` if shorter | duration, <= `window` | Burn rate and "runs out at" forecast. |
| `mode` | `"api"` | `api` \| `subscription` | `subscription`: spend is an estimate, and the forecast is the time to the next provider limit. |
| `providers` | all | list of provider names | `subscription` only: the limits whose hits drive the forecast (names from `[connectors.llm_quotas]`). |
| `min_limit_window` | `"10m"` | duration, > 0 | `subscription` only: ignore limit hits whose limit refills sooner than this. |

`[prices."<model or glob>"]`, US dollars per 1 million tokens. An exact model name wins;
otherwise the first matching glob in config order. Tokens of a model without a price still
count for `tokens` budgets; their cost is unknown and left out of `usd` budgets.

| Key | Default | Meaning |
| --- | --- | --- |
| `input` | *required* | Input tokens (cached tokens excluded). |
| `output` | *required* | Output tokens. |
| `cached_input` | `input` | Cached input tokens. |

## [resources.prometheus]

Host readings from node_exporter through the Prometheus HTTP API, for rule 7. Off unless
present.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `url` | *required* | http(s) URL | Prometheus base URL. No credentials, query or fragment. |
| `hosts` | *required* | table: host name -> label value | Kelpie host name to the value of `host_label`, e.g. `{ host-a = "host-a:9100" }`. Only these hosts are kept. |
| `host_label` | `"instance"` | label name | The label that identifies a machine. |
| `timeout` | `"5s"` | duration, > 0 and <= 60s | Per query. |
| `interval` | `"60s"` | duration, >= 5s | At most one fetch this often. Keep it at most rule 7's `max_reading_age`. |
| `token_env` | `"KELPIE_PROMETHEUS_TOKEN"` | variable name | Bearer token, sent only to `url`. Written here, the variable must be set. |
| `queries` | built-in | table | PromQL overrides, one per reading, see below. |

`[resources.prometheus.queries]` keys: `disk_size`, `disk_avail`, `memory_total`,
`memory_available`, `load1`, `cores`, `cpu_busy`, `processes`, `processes_max`. Each result
must keep the `host_label` label (and `mountpoint` for disk). The defaults are in
`kelpie.example.toml`. `processes` needs node_exporter's `--collector.processes`.

## [connectors.github]

Read-only GitHub connector: evaluates `pr_merged` and `ci_status` conditions for the listed
repositories only.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `repos` | *required* | list of `owner/name` | Nothing else is ever queried. |
| `token_env` | `"GITHUB_TOKEN"` | variable name | Token with read access to PRs and checks. Unset variable: anonymous (60 requests an hour, public repos). Written here, the variable must be set. |
| `api_url` | `"https://api.github.com"` | URL | GitHub Enterprise: `https://<host>/api/v3`; `token_env` must then be set explicitly. |
| `timeout` | `"10s"` | duration, > 0 and <= 60s | Whole request. |
| `cache_ttl` | `"60s"` | duration, >= 30s | Answers are reused this long, then revalidated by ETag. |
| `max_requests_per_tick` | `50` | count, 1 to 5000 | Beyond this, cached answers or an unknown result. |
| `tick_budget` | `"10s"` | duration, > 0 and <= 60s | Wall-clock time for GitHub per tick. |
| `enabled` | `true` | bool | `false` keeps the section but does not query GitHub. |

### [connectors.github.ci]

GitHub Actions runs for rule 9, and the opt-in `cancel_build` action (the only write Kelpie
can make to GitHub).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `repos` | all `[connectors.github] repos` | list | Must be among those. |
| `enabled` | `true` | bool | |
| `window` | `"24h"` | duration, 1h to 7d | Runs created within this. |
| `poll_interval` | `"2m"` | duration, 30s to 1h | Scan this often. |
| `rate_reserve` | `1000` | requests, 0 to 4000 | Skip scans while fewer requests are left this hour (capped at a fifth of the hourly limit). |
| `max_pages` | `3` | count, 1 to 10 | Pages of 100 runs per repo per scan. |
| `max_requests_per_tick` | `30` | count, 1 to 100 | This step's own request budget per scan. |
| `tick_budget` | `"10s"` | duration, > 0 and <= 60s | |
| `included_minutes` | unset | minutes, > 0 | Monthly quota; becomes a `ci_minutes` resource for the forecast. Minutes used are not read; rule 9 projects the burn. |
| `quota_reset_day` | `1` | day of month, 1 to 28 | When the quota resets (UTC). |
| `allow_cancel` | `false` | bool | Allow `cancel_build`. |
| `cancel_repos` | `[]` | list | Required with `allow_cancel`; among `repos`. |
| `cancel_workflows` | `[]` | list of workflow paths | Only runs of these workflows, e.g. `.github/workflows/ci.yml`. Empty: any. |
| `cancel_branches` | `[]` | list of globs | Only runs on these branches. Empty: any branch but the default branch, which is never cancelled. |
| `cancel_token_env` | unset | variable name | A separate token with Actions: write on `cancel_repos`, used only for the cancel request. |

### [[block_conditions]]

Block reasons (or ready-task titles) that name a PR become conditions, so rule 4 can clear
the block and rule 1 treats it as a declared wait. Needs `[connectors.github]`.

| Key | Default | Meaning |
| --- | --- | --- |
| `pattern` | *required* | Pattern with a named group `pr`: `#123`, `123`, `owner/repo#123` or a PR link. |
| `kind` | *required* | `pr_merged` or `ci_green` (a `ci_status` condition). |
| `repo` | unset | `owner/name` for bare references such as `#123`; must be in `[connectors.github] repos`. |
| `applies_to` | `["block"]` | `block` (a blocked task's reason) and/or `ready_task` (a ready task's title, for `dispatch_gate`). |

A condition is attached only after GitHub confirms that the PR exists.

## [connectors.human_tasks]

`human_task` conditions: an agent task waits on a task owned by a person on the same board;
the condition holds once that task is `done`. Needs no service. Who is a person is adapter
config (`[adapters.hermes] human_owners`).

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | `false` keeps the section but evaluates nothing. |
| `block_patterns` | `[]` | Array of `{ pattern = '...' }` tables. Each pattern has a named group `task`; a blocked task whose reason matches gets a `human_task` condition on the named task, when that task is on the board and owned by a human. |

```toml
[[connectors.human_tasks.block_patterns]]
pattern = 'waiting[_ ]human:.*?(?P<task>T-[0-9]+)'
```

## [connectors.llm_quotas]

Probes LLM providers for quota and key health, for rule 7 and subscription budgets. Off
unless present; then at least one provider is required (at most 20). Probes send one GET
without a body to `path`; point it at an endpoint that costs nothing and requires the key.

| Key | Default | Meaning |
| --- | --- | --- |
| `tick_budget` | `"10s"` | Wall-clock time for all probes of one tick, 0.5s to 60s. |
| `providers` | *required* | Array of `[[connectors.llm_quotas.providers]]` tables. |

`[[connectors.llm_quotas.providers]]` entries:

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `name` | *required* | 1-63 of letters, digits, `.`, `_`, `-` | Unique. Used in alerts, metrics, `provider_roles` and budget `providers`. |
| `kind` | *required* | `headers` \| `status_url` | `headers`: read rate-limit headers. `status_url`: read a JSON answer. |
| `api_url` | *required* | https URL | Plain http only to loopback. No credentials, query or fragment. |
| `path` | *required* | absolute path | Zero-cost endpoint, e.g. `/v1/models`; may carry a non-secret query. |
| `token_env` | *required* | variable name | The key. The variable must be set and printable ASCII. |
| `auth_header` | `"Authorization"` | header name | Header that carries the key. |
| `auth_scheme` | `"Bearer"` | word or `""` | Sent as `<scheme> <key>`; `""` sends the bare key. |
| `headers` | `{}` | table | Static, non-secret extra headers. |
| `fields` | see below | table | Where readings come from. |
| `unit` | `""` | string, <= 32 chars | What `remaining` and `limit` count, e.g. `requests`. |
| `interval` | `"5m"` | duration, >= 30s | Probe at most this often. Keep it at most rule 7's `max_reading_age`. |
| `timeout` | `"10s"` | duration, > 0 and <= 60s | Whole request. |

`fields` keys: `remaining`, `used`, `limit`, `resets_at`, `expires_at`. For `headers` the
values are header names; the default is `remaining = "ratelimit-remaining"`,
`limit = "ratelimit-limit"`, `resets_at = "ratelimit-reset"`, and a `fields` table replaces
it as a whole. For `status_url` the values are dotted JSON paths and `fields` is required.
`remaining` is `limit - used` when only `used` is given.

## [alerts]

Real-time alerts. None are sent without a channel. Rule alerts are not sent while
`dry_run` is true or for a rule in `observe`; `kelpie report --send` sends its summary
whatever the modes. An alert goes to every channel whose `min_severity` it reaches.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `dedup_window` | `"10m"` | duration, >= 0 | Identical alerts within this window are sent once per channel. |
| `queue_size` | `50` | count, 1 to 1000 | Per channel; alerts that do not fit are dropped and counted. |
| `channels` | `[]` | array of tables | `[[alerts.channels]]`, see below. Names must be unique. |

Telegram channel (`type = "telegram"`):

| Key | Default | Meaning |
| --- | --- | --- |
| `token_env` | *required* | Variable holding the bot token; must be set at startup. |
| `chat_id` | *required* | A chat id (`-1001234567890`) or `@channelname`. |
| `min_severity` | `"warning"` | `info`, `warning` or `critical`. |
| `timeout` | `"10s"` | > 0 and <= 60s. |
| `name` | `"telegram"` | Lower-case word; needed with two Telegram channels. |
| `api_url` | `"https://api.telegram.org"` | Another Bot API server; plain http only on loopback. |

Slack channel (`type = "slack"`):

| Key | Default | Meaning |
| --- | --- | --- |
| `webhook_env` | *required* | Variable holding an incoming-webhook URL (`https://hooks.slack.com/services/...`). Checked at startup. |
| `min_severity` | `"warning"` | `info`, `warning` or `critical`. |
| `timeout` | `"10s"` | > 0 and <= 60s. |
| `name` | `"slack"` | Lower-case word; needed with two Slack channels. |
| `allow_host` | unset | Another host for the webhook (a relay); plain http only if it is loopback. |

## [report]

The daily "cost of chaos" report (rule 8), built by `kelpie report`, or by `kelpie serve`
once a day when `at` is set.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `timezone` | `"UTC"` | IANA name | Where a day starts and ends. |
| `dir` | unset | path | Writes `kelpie-report-<date>.md` there. Unset: stdout only. |
| `task_url` | unset | string with `{id}` | Links task ids in the Markdown. |
| `hour_usd` | unset | dollars, > 0 | What a stuck task-hour costs. Unset: hours stay hours. |
| `channels` | all | list of channel names | `[[alerts.channels]]` that get the summary with `--send`. |
| `severity` | `"warning"` | `info` \| `warning` \| `critical` | Severity of the summary; channels filter by `min_severity`. |
| `verify_window` | `"1d"` | duration string, > 0 | Before/after of fixes: this long on each side. |
| `review_roles` | `["reviewer"]` | list of roles | Whose sessions make "review time". |
| `min_sample` | `10` | count, >= 1 | Fewer observations on a side: a small sample. |
| `sections` | unset | `[report.sections.<name>]` tables | Settings of a report section. A key the section does not accept is noted in the report and the section does not run until the table is fixed; a table for a section that does not exist is noted too; a bad value fails that section only (also noted). |
| `at` | unset | `"HH:MM"`, local time in `timezone` | `kelpie serve` builds yesterday's report once a day after this time, writes it to `dir` and sends the summary to `channels`. A serve that was down at that time catches up the same day; a failed run is retried every 10 minutes, at most 3 times a day. Unset: only `kelpie report` (by hand or from cron). |

The report reads the board through the adapters. For the day and its before/after window
it widens `[adapters.hermes] lookback` on its own (for that run only; the rules keep the
configured value), so a short `lookback` does not cut the report's data, as long as the
Hermes spool and board still hold it.

## Checks across sections

Kelpie refuses to start when:

- `[[budgets]]` exist but `[traces] enabled = false`; a budget `window` exceeds
  `[traces] retention`; a `usd` budget exists without `[prices]`.
- `[traces] retention` is shorter than `window` or `totals_window`, or, with
  `[traces.outputs]`, shorter than `repeat_days` or twice its `window`.
- `[[traces.forward]]` exists with `[traces] enabled = false`.
- `[[block_conditions]]` exist without `[connectors.github]`, or name a `repo` not in its
  `repos`.
- `ci_economy` `window` is longer than `[connectors.github.ci] window`.
- Hermes is the only adapter, `dry_run = false`, and a rule other than `loop_breaker` (and
  `ci_economy` with `allow_cancel`) is in `act`.
- A variable named by an explicit `token_env`, `webhook_env`, `headers_env` or
  `cancel_token_env` is missing or malformed.
- The [exposure check](install.md#otlp-token-and-the-exposure-check) fails.

Kelpie logs a warning when:

- a configured rule is planned but not implemented;
- `no_progress` `window` is longer than `[traces] window`;
- `conflict_warning` with `serialize`, or `dispatch_gate`, is in `act` without
  `wait_condition` in `act`;
- `[resources.prometheus] interval` or a provider's `interval` is longer than rule 7's
  `max_reading_age`;
- a budget's `providers` names a provider that is not in `[connectors.llm_quotas]`;
- `loop_breaker` and `wait_condition` set different `poll_patterns`;
- no OTLP token is set, or credentials would go over plain http to a non-loopback host.
