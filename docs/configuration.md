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
- [[storage]](#storage), [[snooze]](#snooze)
- [[adapters.hermes]](#adaptershermes), [[adapters.claude_code]](#adaptersclaude_code),
  [[adapters.openclaw]](#adaptersopenclaw), [[adapters.github_issues]](#adaptersgithub_issues),
  [[adapters.linear]](#adapterslinear)
- [[traces]](#traces), [[[traces.forward]]](#tracesforward),
  [[traces.outputs]](#tracesoutputs)
- [[[budgets]] and [prices]](#budgets-and-prices), [[spend]](#spend)
- [[resources.prometheus]](#resourcesprometheus)
- [[connectors.github]](#connectorsgithub), [[connectors.github.ci]](#connectorsgithubci),
  [[connectors.github.pulls]](#connectorsgithubpulls), [[[block_conditions]]](#block_conditions)
- [[connectors.human_tasks]](#connectorshuman_tasks)
- [[connectors.llm_quotas]](#connectorsllm_quotas)
- [[connectors.cost_reports]](#connectorscost_reports)
- [[alerts]](#alerts), [snoozing from the alert](#snoozing-from-the-alert)
- [[report]](#report), [report sections](#report-sections)
- [[agent]](#agent) and [[hub]](#hub): multi-host
- [Checks across sections](#checks-across-sections)

## How the config is read

- **File.** `--config PATH`, else `$KELPIE_CONFIG`, else `./kelpie.toml` if it exists,
  else defaults only. Read once at startup. `--config` goes before the command:
  `kelpie --config kelpie.toml status`.
- **Top-level sections:** `engine`, `storage`, `adapters`, `traces`, `budgets`, `prices`,
  `spend`, `resources`, `connectors`, `block_conditions`, `alerts`, `report`. Anything else stops
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
| `KELPIE_CLAUDE_SPOOL` | Claude Code hook | Spool path on the agent host (default `~/.claude/kelpie/spool.jsonl`). |
| `KELPIE_CLAUDE_SPOOL_COMMANDS` | Claude Code hook | `0`, `false`, `no` or `off`: record no command prefix. |
| `KELPIE_CLAUDE_SPOOL_MAX_BYTES` | Claude Code hook | Spool size that triggers rotation (default 20 MiB). |
| `KELPIE_CLAUDE_STOP_DIR` | Claude Code hook | Where the hook looks for stop markers; must match `[adapters.claude_code] stop_dir`. |
| `KELPIE_CLAUDE_STOP_TTL` | Claude Code hook | Seconds a stop marker holds (default 86400). |
| `CLAUDE_CODE_TASK_LIST_ID`, `KELPIE_TASK_ID`, `KELPIE_AGENT` | Claude Code sessions | The shared task list; a task id from a launcher; the agent's name. Read by the hook. |
| `KELPIE_OPENCLAW_SPOOL`, `KELPIE_OPENCLAW_SPOOL_COMMANDS`, `KELPIE_OPENCLAW_SPOOL_MAX_BYTES` | OpenClaw hook | Spool path, `0` for no command prefix, rotation size. Not read from the Gateway's environment: the plugin sets them from its own settings `spool`, `commands` and `maxBytes` (`plugins.entries.kelpie.config`). |

Variables named by `token_env`, `webhook_env`, `headers_env`, `cancel_token_env`,
`billing_token_env` and `admin_key_env` keys are read as configured.

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
| `state_resolve_after` | `"10m"` | duration, >= 0 | A lasting condition no rule reported for this long is resolved, and a short "resolved" alert goes out: what is true now, from the rule (rule 7: the current reading, "disk h:/ is 80% full, ..."), or, quoted as the past, what the rule no longer reports ('task T-1 (...): no longer reported: "..."'). |
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
  CI connector for `cancel_build`); else a `stop` goes to the adapter that reported the
  session; else to the platform adapter named in its task's `source`;
  else to the only platform adapter if there is exactly one.
- **What can act today.** The Claude Code adapter carries out `stop` only; Hermes carries out
  `stop`, `wait`, waking a parked task and `comment`. With a Hermes adapter enabled and
  `dry_run = false`, Kelpie refuses to start when a rule outside `HERMES_ACT_RULES` in
  `src/kelpie/app.py` (`loop_breaker`, `wait_condition`, `conflict_warning`,
  `dispatch_gate`) is in `act`, except `ci_economy` when `[connectors.github.ci]
  allow_cancel` is set. The Claude Code adapter refuses the other actions with
  `UnsupportedAction`, so on Claude Code they become "not executed" alerts. See the
  [act checklist](act-checklist.md).

## Rules

Each rule is configured in `[engine.rules.<name>]`. Every table accepts `mode`; the other
keys are the rule's own parameters. A table for a planned rule that is not implemented yet
is accepted and ignored with a warning. Rules run in the order below.

| # | Name | Priority | Status | Actions | Act needs |
| --- | --- | --- | --- | --- | --- |
| – | `checkpoints` | P0 | implemented | `comment`, `wip_commit` | adapter `comment`, `wip_commit` (not in Hermes yet) |
| 1 | `exit_contract` | P0 | implemented | `requeue`, `alert`, `comment` | adapter `requeue` (not in Hermes yet), `comment` (acceptance criteria) |
| 2 | `wait_condition` | P0 | implemented | `wake`, `comment`, `alert` | adapter `requeue` (a wake puts the task back in the ready queue), `comment` (not in Hermes yet) |
| 3 | `orphan_status` | P0 | implemented | `alert`, `reassign` | adapter with `SupportsReassign` (not in Hermes yet) |
| 4 | `stale_blocks` | P0 | implemented | `unblock`, `alert` | adapter `unblock` (not in Hermes yet) |
| 5 | `ping_pong` | P0 | implemented | `escalate` | alert channel |
| 6 | `bottlenecks` | P0 | implemented | `alert`, `escalate` | alert channel |
| 7 | `resources` | P0 | implemented | `alert` | alert channel |
| 8 | daily report | P0 | `kelpie report` | — | configured in [[report]](#report), not here |
| 9 | `ci_economy` | P0 | implemented | `cancel_build`, `alert` | `[connectors.github.ci] allow_cancel` |
| 10 | `loop_breaker` | P0 | implemented | `stop`, `alert` | adapter with `SupportsStop` (Hermes and Claude Code have it) |
| 11 | `no_progress` | P0 | implemented | `alert` | alert channel |
| 12 | `conflict_warning` | P0 | implemented | `comment`, `wait`, `wake` | adapter `comment`; `SupportsWait` for `serialize` (not in Hermes yet) |
| – | `dispatch_gate` | P0 | implemented | `wait`, `wake` | adapter with `SupportsWait` (not in Hermes yet) |
| 13 | `needs_you` | P1 | implemented | `escalate` | alert channel |
| 14 | `compaction_gate` | P2 | not implemented | | |
| – | `return_causes` | P1 | implemented | `alert`, `comment` | adapter `comment` |
| – | `output_offenders` | P1 | implemented | `comment`, `alert` | adapter `comment` (Hermes: `hermes kanban comment`) |
| – | `instruction_bloat` | P1 | implemented | `alert` | alert channel |

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
work, and a resume note for the next session (not after a session that declared a block
or a wait). Needs git state from the adapter (Hermes: `git_state = true`).

The WIP commit and the alert about changes left uncommitted share one once-key per session,
so the first decision stands (per mode: switching to act decides again). No commit once the
task was claimed again since the exit (a requeue alone is no claim; the adapter refuses
while a run is active); none onto a branch with an open PR or a PR of the task that closed
after the session started; and none for a session older than the closed PRs listed
(`closed_lookback`).
The resume note goes out with the commit and says where the changes are if it failed.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `commit_every` | `"30m"` | duration, > 0 | Remind a live session with uncommitted changes when its last commit (or its start) is older than this. |
| `commit_every_by_role` | `{}` | table of role = duration | `commit_every` by role, e.g. `{ reviewer = "2h" }`: the task's role, or its owner's when the task has none. |
| `max_changed_files` | `20` | count, >= 1 | ...or when it has more changed files than this. |
| `tool_grace` | `"2h"` | duration, > 0 | No reminder while a long tool run is in progress, up to this long after it started: `Session.running_tool` with its start (Hermes: a build or simulator process in the task's workspace, [adapters.hermes.tools](#adaptershermestools)), or an open TOOL span. |
| `tool_long_after` | `"5m"` | duration, > 0 | An open TOOL span holds reminders once it has run this long: short calls do not. An adapter's `running_tool` holds from its start (it is long by what it is). |
| `wip_ci` | `"skip_ci"` | `skip_ci` \| `draft_pr` | How the WIP commit avoids CI: `[skip ci]` in the message, or a push to a draft PR. Never onto an open PR (drafts included, so with `draft_pr` the draft of a first WIP commit refuses the next ones), nor onto a branch whose PR closed after the session started: none whose head is the session's or task's branch or that belongs to the task, and not without a known branch. A person gets an alert about the uncommitted changes instead. |
| `wip_pr_margin` | `"1m"` | duration, > 0, < `act_within` | A WIP commit waits until `[connectors.github.pulls]` read the open PRs in full at least this long after the exit (an agent's last act may be opening a PR); until then nothing is said, not even the resume note. A PR someone else opens between that scan and the commit (up to `poll_interval`) is not seen. |
| `wip_without_pr_check` | `false` | bool | WIP commits without that wait, for a farm whose task branches never carry PRs. Without it and without `[connectors.github.pulls]`, there are no WIP commits (warned at startup). A fork's PR, or one in a repository not in `[connectors.github] repos`, is not seen. |
| `session_stale_after` | `"30m"` | duration, > 0 | See [live session](#rules). |
| `act_within` | `"24h"` | duration, > 0 | WIP commit and resume note only this long after the exit; a decision never made by then gets one alert. Keep it plus 1h shorter than `[engine] once_window`, and no longer than `[connectors.github.pulls] closed_lookback` (Kelpie warns at startup). |

### 1. exit_contract

A session ended without a final status, or left a block without an allowed reason or a
condition: the task goes back to the queue. Also alerts when a declared final status was
never applied by the platform. With `done_evidence`, a task an agent moved to done without
the configured evidence goes back to the queue too; the reopened-after-done rate per role is
on `/metrics` (`kelpie_role_reopened_after_done_ratio`) and in the daily report.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `allowed_block_reasons` | `["guard", "waiting_ci", "waiting_pr_merge", "waiting_human", "quota"]` | list of strings | A block is allowed when its reason equals one of these, or starts with one followed by `:` or a space; case-insensitive. Replaces the default list. A block tied to a condition is always allowed. |
| `session_exit_grace` | `"10m"` | duration | Time the platform gets to record a status after an exit before Kelpie judges it. |
| `session_stale_after` | `"30m"` | duration | See [live session](#rules). |
| `done_evidence` | `[]` (off) | array of tables | Evidence that a task is done. Each entry: `name`, and either `artifacts` (globs on the path or file name of an artifact a session left since the task was last reopened, case-insensitive: test output, a live-check report; Hermes: the artifacts a completion lists, and files the run's profile attached) or `condition` (a condition kind, e.g. `ci_status`, satisfied and linked to the task; no connector keeps one on done tasks yet). |
| `done_evidence_require` | `"any"` | `any` or `all` | One kind of evidence is enough, or all are needed. |
| `done_evidence_action` | `"requeue"` | `requeue` or `alert` | What happens to a task done without it. |
| `done_evidence_within` | `"24h"` | duration, > 0 and <= 7d | Only tasks that reached done this recently, and at least `session_exit_grace` ago, are judged. Only an agent's completion is (a session ended declaring done when the task became done): not a person's task, one a person closed, or a parent. Hermes cannot requeue a done task: a human is told instead. A person approving a review is not judged (Hermes records no run for it), but a person forcing a running task to done on the dashboard looks like the agent's completion. Keep it within `[engine] once_window` (7d by default), or a judgement repeats. |
| `acceptance_patterns` | `[]` (off) | list of regexes | Lines of a person's comments (or of a description a person created) that match one are acceptance criteria (incident 18), e.g. `'\b\d+\s+(?:runs?\|times)\b'`, `'\b(\d+)\s*(?:of\|/)\s*\1\b'`, `'\bbefore\s+(?:auto)?merg'`; a question, a person repeating an earlier criterion, or a verdict on one ("AC-1a2b3c: met, ...", "not met"), is none; a person confirms with such a verdict, since a bare "saw the log, 20/20" reads as a new criterion. When the work lands (within `done_evidence_within`: an open PR that can merge now with a merge label, critical; a merged PR, critical; a mergeable PR of a task in review or accepted; the task accepted; an agent completing it), each needs evidence written after it that addresses it, opens with the verdict "met" (or a check mark) and reports its result: "AC-1a2b3c: met, 20/20 passed, <link>". Addressing means naming its reference (`AC-` and six hex digits; eight when two criteria of a task would share one) or quoting it: after a quote, the evidence starts once the quoted line or `>` block is over, so the person's own words never count; it runs to the next blank line, reference or quote. The result: the same ratio of runs ("20/20 passed"), the count with pass wording, or all its words. A failure, a partial result ("17/20"), another run count ("out of 21"), runs left out ("excluding run 7"), something still to do ("will", "pending", "retried", "[ ]") or a request ("please post the log") anywhere in that text's own words, asides in parentheses included (what it quotes and its links aside), and a verdict of "not met" or a cross, answers nothing and takes an earlier answer back, so report each criterion in a comment of its own. Any failure wording or partial result in a person's later comment on the task takes an answer back too (it is not told apart from an unrelated failure), and the person who asked, asking again, answers nothing. Evidence is a comment, a session summary, an artifact's file name, or a CI build of the task's branch that passed and whose workflow (GitHub: its file path, e.g. `.github/workflows/ac-1a2b3c.yml`) holds the reference. Text that does not address the criterion never answers it (incident 18's "CI green, merges cleanly"). While a task is ready, in progress or in review and not landing yet, each criterion stated in the last 72 hours is announced on it once, a comment with its reference (in act mode, with an adapter that writes comments). Otherwise one alert per criterion and severity, naming it and the person who asked. Parents and tasks a person closed are not judged. Hermes: a comment is a person's when its author is one of `[adapters.hermes] human_owners` (the CLI writes the caller's profile name, "user" by default), or a human owner Kelpie knows. |
| `acceptance_merge_labels` | `["automerge"]` | list of labels | A PR with one of these merges by itself: its alert is critical. |
| `acceptance_notify_roles` | `["reviewer"]` | list of roles | Named in the alert, with the person who asked. |

### 2. wait_condition

Wakes a waiting task when every condition of its current wait holds on fresh evidence;
alerts when a wait can never be met, runs past `max_wait`, or has no condition; comments on
sessions that poll a known check instead of registering a condition and exiting, and on
sessions that waited inside themselves (`sleep`, a CI watch, a loop over them) however they
ended. A wait
past `max_wait`, or one with no condition, that an open CI outage explains (rule 9) is
folded under the outage's alert instead of being sent; a dead end or a condition Kelpie
cannot see is not.

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
| `session_waits` | `3` | count, >= 0 | A session that waited inside itself this many times within `session_wait_lookback` gets one comment on its task (block on a condition and exit), however it ended; under the live polling check's key, so a session gets one comment or the other. Only waits of 30 seconds or more, or of unknown length (a watch, a loop), count. `0`: off. The waits come from the platform's own record of the commands (the Hermes adapter: each profile's `state.db`, `state_home`), as their shape and duration bucket only. |
| `session_wait_time` | `"10m"` | duration, > 0 | Also flag a session whose waits add up to this long (the lower bounds of their buckets: under 30s, 30s, 1m, 5m, 15m, 30m, 1h). |
| `session_wait_lookback` | `"6h"` | duration, > 0 | Waits older than this do not count (the Hermes adapter reads the last 6 hours: longer sees no more there). Sessions not on an open task Kelpie knows of (an orchestrator's, a finished task's) are counted per role in one `info` alert instead: the role's instructions are the owner's to change. |

**Waits rule 13 lists go to rule 13.** A waiting task that rule 13 lists in a person's
"needs you" digest, parked on a task the person can do now by a `human_task` condition with
no other unmet condition, gets no overdue alert from this rule: the person gets one message
instead of two, as for blocks (rule 4, above). This rule reads the farm with rule 13's whole
`[engine.rules.needs_you]` section and leaves exactly what rule 13 lists, its time
thresholds aside. Under the hand-off rule 13's snooze handling decides what the person
hears: a snoozed human task is in the digest's footer, and one whose snooze ended or whose
work grew is back in the digest. A listed wait whose conditions Kelpie cannot check (never
checked, last checked longer ago than `max_condition_age`, or the connector reports an error) keeps this rule's overdue alert, worded as a
connector problem ("C-1 was not checked on this pass: is the connector
running?"), snoozed or not: the digest cannot say what Kelpie does not know. Everything else
stays this rule's: a wait on a task the person cannot move yet (in the backlog or triage),
one that also waits for a PR or CI, a dead end (the human task was cancelled), a condition
Kelpie cannot see, a task waiting on nothing, and the wake. When a wait moves to the digest
while its alert is open, the "Resolved" notice says it is in the person's digest now (rule 4
does the same for a block). The hand-off applies only while rule 13 reaches people: it is loaded, and it is not in
`observe` mode while this rule is in `suggest` or `act` (else this rule alerts as before).
Kelpie decides that from the modes: `needs_you` is not a key to set here, and a mistake in
rule 13's section is reported as rule 13's. Kelpie logs a warning at startup when rule 13
waits longer than this rule before it lists a wait (`human_wait_alert_after` longer than
`max_wait`): the wait is then announced by neither rule in between.

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
| `parent_close_after` | `"1h"` | duration, or `false` | Stop pattern (incident 17): every subtask of a parent is finished, the last this long ago, and the parent is still open with no live session → alert "close it?" with the tasks waiting behind it and the merged PRs, and `close` (act mode, on an adapter that supports it). Only when its work has certainly landed: the last two scans of `[connectors.github.pulls]` read every repository's open and recently closed PRs in full, every PR linked to the parent or a subtask is merged, and every subtask has a merged PR of its own into the default branch. Otherwise no `close`: a PR closed without merging (or a merged revert) alerts `parent-prs-unmerged` naming it, anything else unknown alerts `parent-unverified` ("check its PRs"). An open PR of the family means the parent is not finished. Rules 1 and 5 never resume a finished parent. |
| `prework_after` | `"4h"` | duration, or `false` | Stop pattern (incident 17): a card in a pre-work status (Hermes `[adapters.hermes] prework_statuses`) this long, with open tasks depending on it → alert with those tasks. The daily report counts these states as their own cost-of-chaos line, "cards stuck before work", in card-hours (a task-hour counted once across causes); a card past this threshold that nothing depends on (and that is not held, ignored or queued behind its own parent) is only listed ("Stuck, blocking nothing", with its age), not counted. |
| `hold_labels` | bottlenecks' | list of labels | Tasks with one of these labels are parked on purpose: no stop pattern looks at them. Unset: `bottlenecks` `hold_labels` and `holds` labels. |
| `ignore_tasks` | `[]` | list of ids or globs | Tasks rule 3 never looks at, e.g. `["t_parked*"]`: the escape hatch on boards without labels (Hermes), where holds cannot be expressed. |

Statuses: `in_progress`, `review`, `accepted`. Terminal statuses and `blocked`, `waiting`,
`backlog`, `ready` are refused. Each stop-pattern alert carries `params.pattern`
(`review-undriven`, `no-next-action`, `dependents-not-woken`, `dead-end-chain`,
`recovery-loop`).

### 4. stale_blocks

Clears a block when every linked condition holds on fresh evidence. Alerts on blocks it
cannot clear. A block Kelpie set itself (a run rule 10 stopped, recognised by its signed
marker) is not one of them: it waits for a human, so rule 13 lists it instead. While a CI
outage that explains the block is open (rule 9), its overdue and no-condition alerts are
folded under the outage's one alert instead of being sent; dead ends, stale evidence and
a condition Kelpie cannot see are not. A block first seen during the outage is announced if it outlives it; an escalation
that came due during the outage is sent when it ends.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `unblock_grace` | `"5m"` | duration, >= 0 | Wait this long after the last condition was met before unblocking, so the agent or platform can do it first. |
| `max_condition_age` | `"15m"` | duration, > 0 | See [fresh condition](#rules). |
| `unlinked_alert_after` | `"4h"` | duration or `false` | Alert on a block with no condition Kelpie can see after this. `false`: off. |
| `max_block` | `"8h"` | duration or `false` | Alert when a linked condition is still unmet after this. `false`: off. |
| `stale_evidence_after` | `"1h"` | duration or `false` | Alert when the connector has not checked a linked condition for this long. `false`: off. |
| `storm_min_tasks` | `5` | count, 0 (off) or 2–1000 | A block storm (#229): this many tasks blocked on the same reason (the task's own id, dates, times and hashes stripped; the PRs, URLs and other tasks it names kept apart; more than a kind of wait; blocks without a condition Kelpie can check, not snoozed)... |
| `storm_window` | `"2h"` | duration, > 0 | ...within this of each other are one outage: one alert with the shared reason, the person it names and the tasks. Once open it stays open while half of `storm_min_tasks` (at least 2) are still blocked on the reason. |
| `storm_mode` | `"observe"` | `"observe"` or `"rule"` | A new check: its alert (and its "resolved" notice) is only logged until set to `"rule"`, which follows the rule's mode and folds this rule's no-condition and overdue alerts of its tasks under it (not rule 13's inbox items). |

A condition that can never be met (a PR closed without merging, a human task cancelled)
alerts at once.

**Blocks rule 13 lists go to rule 13.** A blocked task that rule 13 lists in a person's
"needs you" digest gets no no-condition, overdue or dead-end alert from this rule: the
person gets one message instead of two. Rule 13 lists an agent's block whose reason asks a
person (`waiting_human: needs_input: CEO: …`), an agent task parked on a person's task they
can do now, and a person's own blocked task with agent work behind it. This rule reads the
farm with rule 13's whole `[engine.rules.needs_you]` section (`approval_reasons`,
`condition_reasons`, which default to the `[[block_conditions]]` patterns, and
`planned_labels`), so the two cannot disagree, and it leaves exactly what rule 13 lists, its
waits aside. Everything else stays this rule's, so every block is announced by exactly one
of the two:

- a block rule 13 does not list: parked on a person's task the person cannot move yet (in
  the backlog or triage, or behind agent work in progress), or a person's blocked task with
  nothing behind it;
- an agent's block with a dead end (the human task it waited on was cancelled: the alert
  lists the tasks stuck behind it) or a condition Kelpie cannot see: rule 13 does not list
  these;
- a block whose reason matches a condition pattern (`needs_input: automerge PR #12`) or
  that has an unmet linked condition other than a human task (also when it waits on a
  person's task as well): it waits on that condition.

A person's own listed task whose block can never clear (a dead end, a condition Kelpie
cannot see) gets no alert here either: the digest's "Needed" line says it. A connector that
stopped checking alerts whoever lists the task.

Clearing a block once its conditions hold is unchanged. The hand-off applies only while
rule 13 reaches people: it is loaded, and it is not in `observe` mode while this rule is in
`suggest` or `act` (else this rule alerts on every block as before, so nothing goes silent).
Kelpie decides that from the modes: `needs_you` is not a key to set here. A mistake in
rule 13's section is reported as rule 13's. Kelpie logs a warning at startup when rule 13
waits longer than this rule before it lists a block (`approval_wait_after` longer than
`unlinked_alert_after`, or `human_wait_alert_after` longer than `max_block`): in between,
neither rule announces it.

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
| `planned_labels` | `[]` | list of labels | Labels (milestones as labels) that mark a task not started yet (backlog, ready) as later (incident 20); a blocked or waiting task was started and waits on what it names. A planned task is not ready for work for its own reasons, so it does not wait on a human task it depends on, and neither does what waits on it: it is not behind a human gate. A planned human task gates only the tasks a condition parks on it (they were started), not those that depend on it. Tasks in a pre-work status (Hermes `prework_statuses`: triage, scheduled) and held tasks are planned too; a label on a linked PR counts. Rule 13 inherits these and the hold labels; the daily report's agent-hours waiting on people follow rule 13 (its wait causes still file a held task under `hold`). |
| `human_gate_after` | `"1h"` | duration, >= 0 | A human task gates the agent work behind it once it has been ready or in progress this long (for the farm-level alert and the escalation). |
| `human_gate_min_tasks` | `3` | count, >= 1 | With `escalate_human_gates = true`: escalate a gate with at least this many agent tasks behind it. Not used otherwise. |
| `escalate_human_gates` | `false` | bool | `true`: also escalate each human task with `human_gate_min_tasks` agent tasks behind it to its owner. By default rule 13 escalates human work, one message per person; this rule keeps the farm-level alert (free slots, only human tasks ready). |
| `ready_is_queued` | `false` | bool | Assigned ready or review tasks nobody started are queued work of their role, not a busy owner. For platforms that report the assignee of an unstarted task as its owner; Hermes does not need it (its tasks carry `assignee`, and an owner only while a run claims them). |
| `ready_prs` | `false` | bool | Open PRs GitHub reports as mergeable count as ready work for idle agents (needs `[connectors.github.pulls]`). Set it where agents merge PRs. |
| `pr_role` | unset | role | The role that merges PRs. Unset: any idle agent. |

`[[engine.rules.bottlenecks.holds]]` entries:

| Key | Default | Meaning |
| --- | --- | --- |
| `label` | *required* | The label that marks held tasks. Unique. |
| `owner` | unset | Who owns the hold; named in the alert. |
| `expires_after` | `hold_expiry`, else `"4h"` | Expected length. A hold's age is how long its oldest queued task has not moved, or since the label went on a PR (`[connectors.github.pulls]`). |

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
| `time_to_full_warning`, `time_to_full_critical` | `"72h"`, `"24h"` | duration, or `false`; critical shorter than warning (also against the other's default: a warning under 24h needs a shorter critical, or `false`) | A disk that fills within this at its growth rate (`[resources.prometheus] growth_window`, capped at what held over `growth_sustain`) warns, or is critical (incident 19). The alert names the fastest-growing measured directories on that filesystem. A disk past its utilization threshold gets the forecast in that alert, at the worse severity, not a second alert. |
| `time_to_full_release` | warning × 7/6 (`"84h"`; critical × 7/6 with the warning off) | duration, not shorter than the warning; `false`: no hold | A disk's open alert (opened by the forecast or by its utilization: one condition per disk) stays open, a warning, until the disk would fill later than this, so a growth rate near the warning does not open and resolve it again and again. |
| `min_growth_per_day` | `0.1` | GB per day | Slower growth forecasts nothing. |
| `growing_paths_shown` | `3` | count, 0 to 20 | Directories named in the alert. |
| `path_max_age` | `"1h"` | duration | Directory sizes older than this are not named; the alert asks whether the script still runs. |
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
forecasts the CI minutes quota, and reports a CI outage as one incident. Needs builds from
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
| `outage_min_prs` | `3` | count, 0 (off) or 2–1000 | A CI outage: some window holds failures of this many change requests (repository and branch; retries count) at the same workflow, job and step, with evidence that the environment broke; twice as many without it... |
| `outage_window` | `"2h"` | duration, > 0 | ...that window's length; a failure counts until the job passes again in its repository. |
| `queued_after` | `"30m"` | duration, > 0 | CI jobs not starting: a build still queued this long while no runner on the farm takes work is a sign, as is a job the CI refused (no runner, no step) that used to run, or a whole run refused; signs of `outage_min_prs` change requests within `outage_window` → one outage. |
| `not_started_mode` | `"observe"` | `"observe"` or `"rule"` | A new check: its outage is only logged until set to `"rule"` (then it follows the rule's mode and folds the tasks' own alerts). |

Waste categories, each build counted once in this order: superseded, already verified
commit, docs-only, draft, WIP commit.

The waste summary also recommends the CI configuration change that would have saved each
category's minutes, the most saved first: cancel a running build when a newer commit is
pushed (`cancel_in_progress`; it saves only the part of a build after its newer build
started), skip a build of a commit that already passed this pipeline for the same trigger
(`skip_verified`), skip changes that touch only docs (`ignore_docs_paths`), skip draft change
requests (`skip_drafts`), skip WIP commits (`skip_wip`). The changes are named, not written in
any CI's syntax. Its `params.pipelines` lists them per pipeline, the most minutes saved
first, at most 10 (`pipelines_total` counts them all):

| Key | Meaning |
| --- | --- |
| `source`, `repo`, `workflow` | The pipeline: the CI connector that reported it, the repository, the workflow |
| `minutes` | Minutes its changes would have saved in the window |
| `changes` | Each `{change, minutes, builds}`; `ignore_docs_paths` adds `paths` (the `docs_globs`, fnmatch: `*` crosses `/`), `skip_wip` adds `patterns` (the `wip_patterns`) and `case_insensitive: true` |

A CI outage (incident 20) is one `warning` "CI outage since <time>": the failing workflow,
job and step, the repositories, runners and PRs it affects, and the tasks waiting on those
PRs' CI or merge. Evidence that the environment broke, not each change: two repositories
fail on the same self-hosted runner, or a commit that passed the job before fails it now
(a re-run; it does not count toward `outage_min_prs` itself). Without it, `outage_min_prs`
doubles, so a few PRs red at their own tests are not called an outage. Only the
repositories the evidence ties together (and those failing the same way on their runners)
are in the outage; a PR red at the same step elsewhere is not. It stays open, without repeating
itself, until a build passes that job again in its repositories; it also closes when its
failures leave the CI connector's window or too few of them stay readable, and may then
open again. The failing step comes from the job list the CI connector reads for billable
minutes (no logs); a build whose jobs it did not read takes no part. Times are the builds'
finish times.

CI jobs that never start (#229) are a separate outage: a CI that hit a spending limit or a
failed payment refuses jobs (they end at once with no runner and no step) or leaves runs
queued. A refused job is not a failing one: it never counts toward the outage above. It is
a sign when the CI refused the whole run, or where that job used to run (an earlier build
of the repository and workflow started it for the same trigger; for a push, the same
branch: a deploy job that branch or environment rules refuse is no sign). It counts until a
later build there starts that job again (a skipped job never ran). A queued build is a
sign after `queued_after`, unless the CI holds it on purpose (an approval, a concurrency
group) or runners take work anywhere on the farm (a build is running, or a later one
started a job). Signs of `outage_min_prs` change requests within `outage_window` of each
other → one `warning` "CI jobs not starting since <time>" with the repositories, PRs and
waiting tasks, saying which: check the CI's billing and spending limits or a protection
rule (refused), or that runners are up (queued). The "Resolved" notice names the first
build that ran a job refused since the outage began. Builds are dated by when they
started. Observe-only until `not_started_mode = "rule"`.

### 10. loop_breaker

Stops runaway runs from traces: identical consecutive tool calls, error streaks, and hard
budgets. Info alert when a budget or a subscription limit is near. Warning when a
provider's cost report and the spend priced from traces disagree for its newest closed day
past `warn_gap` and `warn_min_usd` of [[connectors.cost_reports]](#connectorscost_reports).

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
| `tool_outage_tools` | `[]` (off) | list of non-empty strings | Correlated tool failures: the farm's build and test runners (e.g. `["ios_sim.sh", "xcodebuild"]`), matched as the command of a tool call's last segment, path aside (see below). |
| `tool_outage_min_tasks` | `3` | 2 to 1000 | Distinct tasks (a session tied to no task counts as one) failing one tool with one exit code within `tool_outage_window` → one outage. |
| `tool_outage_window` | `"30m"` | duration, > 0 | How close together those failures must be; the `[traces] window` at most, whose spans the snapshot holds. |
| `tool_outage_mode` | `"observe"` | `"observe"` or `"rule"` | A new check: its outage is only logged until set to `"rule"`. |

```toml
[engine.rules.loop_breaker.agents.agent-ops-1]
identical_calls = 20
[engine.rules.loop_breaker.roles.maintenance]
consecutive_errors = 8
```

Correlated tool failures (#229, signal 2): a tool of `tool_outage_tools` failing on the
farm's own hosts, where no CI check sees it (a broken Xcode install: every
`ios_sim.sh` run exits 127). It reads the exit codes adapters record on tool calls
(`kelpie.tool.exit_code`, `kelpie.tool.result`; the Hermes adapter takes them from the
terminal tool's result, never its `error` key): a foreground call that exited non-zero is a
failure, its exit code the error class; one that exited 0 a success; a call sent to the
background (its 0 means started) and one the platform refused (Hermes: exit -1) are
neither. A call is the tool's when the tool is the command of the command's last segment
(after `;` or `|`, or after `&&` only from setup commands such as `cd` or `export`; past
`FOO=1`, `env`, `time`, `timeout <duration>`): `cd app && ios_sim.sh test` is,
`lint.sh && ios_sim.sh test`, `ios_sim.sh test | xcbeautify` and `pkill ios_sim.sh` are
not. Commands are read as the trace redaction kept them: a script run as `bash x.sh` or
`timeout 600 ./x.sh` is masked whole and never seen, and a success counts only when nothing
after the tool was masked (a masked pipe's exit code is not the tool's). Failures of one tool
with one exit code by `tool_outage_min_tasks` distinct tasks within `tool_outage_window`,
with no success of the tool since → one `warning` "Tool outage: ios_sim.sh exits 127 for
3 tasks", with the tasks and what the code usually means (127 not found, 124 timed out, 137
killed). One outage per host where the tasks' host is known: a healthy host's successes do
not hide a broken one. One task retrying its own failure is none. The "Resolved" notice
names the tool's first success after the outage's last failure. Observe-only until
`tool_outage_mode = "rule"`.

### 11. no_progress

Alerts when a session spends tokens without a status change, commit or artifact, with
recommendations; and, progress or not, when a session's context runs away: every step
re-reads all of it (an `info` alert per session, recommending compaction or a bounded
session). The context check reads the spans' own sessions, so it also works where the board does
not know a trace's session (the alert then names no task unless the spans do).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `window` | `"30m"` | duration, > 0 | Tokens and progress are compared over this sliding window. `[traces] window` must be at least this long (Kelpie warns otherwise). |
| `min_tokens_without_progress` | `100000` | tokens, >= 1 | Alert at this many input plus output tokens with no progress. |
| `claim_grace` | `"2m"` | duration, >= 0 | A status change this soon after the session start is the claim, not progress. |
| `context_alert_tokens` | `150000` | tokens, >= 0 | Alert when a session's latest step within `window` reads a context (input tokens, cached ones included) of at least this, after `context_min_steps` steps in the window. Once open, the alert stays open above 80% of it. `0`: off. |
| `context_min_steps` | `5` | count, >= 2 | Steps (counted model calls, of all its models) a session needs in `window` before its context is judged. The context is the main model's: the one with most input tokens in the window, while its latest step is among the session's last two (side calls on a small model are no compaction). The alert counts as tokens re-read, not as lost work, in the daily report. |

### 12. conflict_warning

Two live tasks edit the same files: each gets one comment naming the other and the shared
files. Needs an adapter that reports `Task.files` (Hermes does not yet).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `expected_shared` | `[]` | list of globs | Files many tasks are meant to edit, such as a changelog. Ignored when comparing. |
| `serialize` | `false` | bool | Also park the later task in `waiting` until the earlier ones are done. Needs an adapter with `SupportsWait` and `wait_condition` in `act` mode to wake it (Kelpie warns otherwise). |

### return_causes

Why tasks come back (incident 7). Returns (`Task.returns`; Hermes: a reviewer's changes
requested and reopened reviews) are grouped by cause over a window: what sent a task
back (a merge conflict, a failing or flaky test, else what review found:
`review_findings`), not the stage it went through. When merge conflicts dominate and some
files keep coming up in them, Kelpie raises one alert with the hot files and a
recommendation, and comments on in-progress tasks that touch them. Otherwise a dominant
cause gets an informational alert with the shares by cause; review findings are not
reported as dominant when every return of the window was filed under a stage (a Hermes
farm's normal state). Order: `merge_conflict`, then `cause_patterns` categories, then a
failing test (named, or CI's wording), then `<stage>_findings`, then the platform's
category.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `window` | `"24h"` | duration, > 0 | Returns counted (Hermes reports every return of the tasks on the board, whatever their age; finished tasks older than its `lookback` are not on it). |
| `min_returns` | `5` | count, >= 1 | No finding with fewer returns in the window. |
| `conflict_ratio_threshold` | `0.5` | share, (0, 1] | Merge conflicts must make up this share of the returns for a hot-file alert. |
| `hot_file_min_conflicts` | `3` | count, >= 1 | Conflicts naming a file that make it hot. |
| `dominant_ratio` | `0.5` | share, (0, 1] | Share of a cause (other than `other`) reported as dominant when there is no hot file. |
| `expected_shared` | `conflict_warning`'s | list of globs | Files many tasks are meant to edit: never hot. |
| `test_names` | pytest `test_...`, XCTest `SomeTests.testSomething` | list of regexes with a named group `test` | Test names in return reasons (incident 18). |
| `flaky_min_tasks` | `2` | count, >= 2 | A test named in the returns of this many different tasks in the window is flaky: its returns count as `flaky_test`, and it gets an alert of its own with the tasks it sent back (no `min_returns` needed). |
| `cause_patterns` | `merge_conflict` patterns | table of category -> regexes | Classify a return by its reason (case-insensitive), after `merge_conflict` and before failing tests and the platform's category. An entry named `merge_conflict` replaces the default. |
| `ci_wording` | `true` | bool | CI's wording ("tests failed", "CI is red", "the build failed", "3 failed") counts as `failing_tests` when no test is named; not in a clause that says none failed or that it was fixed. `false`: only named tests (`test_names`). |
| `stage_categories` | `["review"]` | list of platform categories | Categories that name where a return happened, not why: a return filed under one whose reason names no conflict or failing test counts as `<stage>_findings` (`review_findings`). `[]`: the platform's category as the cause, as before. |

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

### 13. needs_you

Everything waiting for a person, one escalation per person, sorted by cost of delay
(agent-hours waited so far):

- human tasks (owners with `kind = "human"`) with agent work waiting on them, through
  dependencies or `human_task` conditions, that the person can move now;
- agent tasks blocked with a reason that asks a person (`approval_reasons`) and no human
  task linked to the block, routed to `approvers[role]`, else `default_approver`, else
  listed as unassigned.

Only agent tasks that wait on the human task itself add to its cost; work deeper in the
chain counts in the number held. The alert is a long-lived state per person: announced
when it opens, again when the list of tasks changes or at the escalation thresholds,
resolved when empty. `escalate_to` names the person; delivery goes to the alert channels
like every other alert, as one digest ("Waiting for you: 2 tasks (oldest 46 h)") built from
`items` (every item, at most 200, `items_total` counting them; the message shows 10 and
counts the rest): per item its `task_id`, `title`, `needed` (an approval's block reason, or
why a human task is blocked: for one that can never clear on its own, the dead end or the
condition Kelpie cannot see, which `stale_blocks` then does not alert on; none when the
title says it), `since` and `blocking` (the
tasks waiting behind it). The daily report heads "Needs a human" with each open inbox, and
counts the agent-hours of the agent tasks waiting on each person in its cost of chaos
("agents waiting on people"), from the count every alert reports (`waiting_agent_tasks`,
re-sampled about hourly while the inbox stays open). Planned work behind the listed human
tasks (incident 20) is apart and not counted there: `planned_agent_tasks` and `planned` (the
first planned agent task on each dependency path, held ones included; at most 20 ids).
Rule 6's human-gate alerts carry `planned_agent_tasks` too. This
rule owns escalating human work; rule 6 keeps the farm-level alert (free slots, only human
tasks ready) and escalates single human gates only with `escalate_human_gates = true`.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `human_wait_alert_after` | `"1h"` | duration, > 0 | A human task enters the inbox once agent work waiting on it has waited this long. |
| `approval_wait_after` | `"30m"` | duration, > 0 | An agent waiting for a person's input or approval enters it after this long. |
| `planned_labels` | `bottlenecks`' `planned_labels` and hold labels | list of labels | A task not started yet (backlog, ready) labelled with one of these (or whose linked PR is), or in a pre-work status (Hermes triage, scheduled), is not ready for work for its own reasons: it waits on no human task (no cost, not held), and a planned human task is in the inbox only for the tasks a condition parks on it. A blocked or waiting task was started: it waits on what it names (an approval, a human task), labels or not. The daily report's agent-hours waiting on people follow, since they come from this rule. Set it only to differ from rule 6: it replaces both inherited lists, hold labels included. |
| `approval_reasons` | `["waiting_human", "needs_input", "approval"]` | list of prefixes | Block reasons that ask a person, matched as whole words at the start, case-insensitive. `waiting_human: transient` blocks are left out. `stale_blocks` reads this section, to leave the blocks this rule lists to it. |
| `condition_reasons` | the `[[block_conditions]]` block patterns | list of regular expressions | A block whose reason matches one (searched, case-insensitive), or that has a linked condition other than a human task, waits on a condition, not on a person, whatever its kind: agents block with `needs_input` to get past Hermes' block loop guard while a PR merges. Tie the patterns to waiting wording (`\b(?:automerge\|merge of\|waiting for)\b[^\n]{0,80}?PR\s*#(?P<pr>\d+)`) rather than any PR mention: a real question that names a PR ("may I force-push PR #12?") would leave the inbox and reach only rule 4 (after `max_block`). Keep them simple (no nested quantifiers, no leading `.*`): they run on the first 2000 characters of every blocked task's reason each tick. |
| `approvers` | `{}` | role → human owner id | Who answers an agent role's questions (the task's role, else its owner's role). |
| `default_approver` | unset | human owner id | Otherwise this person. |
| `critical_after_hours` | `24` | agent-hours, > 0 | The escalation is critical once a person's total reaches this. |

### output_offenders

A command (or tool) whose output floods one role's context above `[traces.outputs]
threshold_tokens` per call on `repeat_days` days is a repeated offender, ranked with the
daily report's ranking from stored tool spans plus the hook spans in the snapshot,
recomputed hourly over the calendar days of `[report] timezone` (today counts while it
runs). The rule addresses the owner of the role's instructions (`[traces.outputs] owners`)
with a `comment` on the most recent offending task that is still open: the command, its
tokens per call, the filter to add to the instructions and the estimated saving. Without
an open task it alerts instead. The request is made once per role, command and filter
within `once_window` (7 days), whichever task it lands on; the daily report checks the
fix before/after. Only plain command or tool names are posted on the board; others are
alerted in a cleaned form.

The rule takes no parameters of its own: thresholds and owners are in
[[traces.outputs]](#tracesoutputs). It needs `[traces] enabled`; on a farm that reports
tool calls only through hooks, `[adapters.hermes] lookback` must cover `repeat_days`.

### instruction_bloat

Incident 21: the instruction files each role loads (skills, `CLAUDE.md`, `AGENTS.md`, role
prompts), measured read-only by Kelpie (sizes, section counts and hashes; never the text).
A file is reported when it crossed `max_chars` within the growth window, when it grows
faster than `max_growth_per_week`, or when it is over `max_chars` and costs at least
`max_tokens_per_day`, counted only for files read into every session of their role or
from counted loads: a large skill opened on demand that does not change is not bloat. The
alert gives its size in characters and estimated tokens, its growth, whether it only grows
(appends and nothing removed), its tokens a day and where that figure comes from, the
near-duplicate sections and repeated values that involve it, and recommends a short core
plus topic references. Copies that involve no reported file get their own informational
alert per role. Both are lasting states: alerted once, kept open while the file stays over
`max_chars` (also once the growth window has moved past its growth), resolved when it is
split. Kelpie never edits instruction files; the rule only alerts.

| Key | Default | Type | Meaning |
| --- | --- | --- | --- |
| `max_chars` | `40000` | integer > 0 | Size threshold in characters (about 10,000 tokens). |
| `max_growth_per_week` | `10000` | integer > 0 | Characters added per week, over the growth window. |
| `max_tokens_per_day` | `500000` | integer > 0 | Daily read cost that makes a file over `max_chars` worth splitting even when it is stable. |
| `min_span` | `"1d"` | duration > 0 | Growth is judged only over at least this much history. |

## [storage]

| Key | Default | Meaning |
| --- | --- | --- |
| `path` | `"kelpie.db"` | SQLite database: action ledger, alert states, snoozes, spans, recorded fixes. `":memory:"` keeps it in memory. `$KELPIE_DB` overrides it. |

## [snooze]

A snooze records that a person knows a task waits on purpose until a date (a decision
deferred to a release, a manual pass planned for a day). `kelpie snooze <task> --until ...`
stores one ([install.md, "Command line"](install.md)), and so does a press on a Telegram
alert's snooze button ([Snoozing from the alert](#snoozing-from-the-alert)). A stored
snooze wins over a marker for the same task. `kelpie serve` puts active snoozes, and those expired within the last 7
days, into every snapshot; expired snoozes are pruned from the database after 30 days.
`kelpie why <task>` shows the task's snooze.

Dates and times, for `--until` and markers alike: a bare date (`2026-10-25`) means through
that day, so the snooze ends when the next day starts in `[report] timezone`; a date and
time without an offset (`2026-10-25T09:00`) is in `[report] timezone` too; with an offset
(`Z`, `+02:00`, `-0500`) it is that instant. Kelpie echoes the resolved time in
`[report] timezone` and in UTC.

**Markers** (off by default). With `marker = true`, an agent can record a person's decision
in the block reason as `until:YYYY-MM-DD` or `until:YYYY-MM-DDTHH:MM` (seconds allowed).
With several markers the latest wins. A marker inside double quotes (`"..."`), typographic
double or single quotes (U+201C/U+201D, U+2018/U+2019) or backticks is not read: that is
text the agent copied. Any agent can write these words, so turn markers on only where
agents record people's decisions, as with `[[adapters.hermes.human_decisions]]`. A marker
snooze is weaker than a person's: rules may only hold repeat human-wait alerts on it, never
agent-health alerts, and list it as "from marker".

**What a snooze changes.** Only the repeat reminders of a human wait, in three rules:

- `needs_you` (rule 13): a snoozed item leaves the person's digest (its counts, cost and
  `waiting`) and is listed in the digest's footer instead: "Snoozed: <title> through
  2026-10-25 (<reason>, by <who>)", "(from marker)" for a marker. Setting the snooze changes
  the digest once ("changed"); while it holds, it causes no announcement or escalation, and
  a person whose items are all snoozed gets no digest. An item whose block can never clear
  (a dead end, a condition Kelpie cannot see) is never held: that is news.
- `stale_blocks` (rule 4): no no-condition and no overdue alert on a snoozed block. Its
  dead-end, unseen-condition and stale-evidence alerts still fire: those are facts, not
  reminders. A marker holds only a block that waits on a person (it asks one, or waits
  only on human tasks), never an agent's own wait on CI or its own parking.
- `wait_condition` (rule 2): no overdue alert on a waiting task whose only unmet
  conditions are human tasks a snooze holds; a dead end still alerts. A wait Kelpie cannot
  check (a condition never checked or not checked within `max_condition_age`, or one the connector reports an error for) is never
  held: it keeps alerting. While rule 13 reaches people, a wait it lists is left to it, and
  rule 13's snooze handling decides what the person hears (see
  [2. wait_condition](#2-wait_condition)); a wait Kelpie cannot check keeps rule 2's alert
  then too.

Agent-health rules (`loop_breaker`, `no_progress`, `exit_contract`, `orphan_status`, ...)
never honour a snooze. A snoozed task alerts again when its snooze ends ("Snooze ran
through 2026-10-25", for the 7 days an expired snooze is kept) or when more tasks are
blocked behind it than when it was snoozed ("2 more tasks blocked since the snooze").
That count is of the open tasks behind the task through dependencies and task conditions,
whatever their status. `kelpie serve` records it at the first complete tick after the
snooze: every local adapter and connector read fine, and the task's own part of the farm
is observed (an offline agent holds up only its own tasks). Until then growth is not
checked (`kelpie why` shows "baseline pending"; after an hour Kelpie logs it once). Growth
is latched: the first time serve counts more, it stores when and how much more with the
snooze, and the task stays back in the digest until the snooze is replaced or expires,
even if the count drops again. A marker never has a baseline, so it never comes back this
way. One known case: a waiter linked only by a condition its block reason attaches counts
only while it is blocked, so a baseline taken while it is briefly unblocked is one short
and its next block brings the task back once. Snoozes are stored in schema 9 of
`kelpie.db`; `kelpie serve` upgrades to it and drops the baselines `kelpie snooze` stored
before (counted offline), so it counts them again. `kelpie snoozes` and `kelpie why` read
schema 8 as it is. Snooze the
task rule 13 lists (the human task, or the blocked task that asks); `kelpie snooze` warns
when a task is only parked behind a human task, since that snooze holds nothing. Alerts
show a snooze's end in `[report] timezone`: one ending at midnight as the last day it
covered, as `--until` takes a date. In the daily report a people or unknown wait that a
snooze holds (the task's own, or that of a task it waits on: any snoozed task it depends
on, or a human task an unmet condition names), while rule 13 does not list the task as
live, is its own wait cause, "snoozed (deliberate)": listed apart ("Snoozed: X task-hours
of N task(s) waiting on purpose, through … at the latest"), never the biggest wait and
never in the cost of chaos. A CI, merge, outage or hold wait stays what it is.

| Key | Default | Meaning |
| --- | --- | --- |
| `max_days` | `90` | How far ahead `kelpie snooze --until` may reach, 1 to 365 days. A bare date is counted by date: the date exactly `max_days` after today is the last one allowed. A later one is refused (exit 2), and the message names the last date or time allowed. |
| `marker` | `false` | Read `until:` markers in block reasons. A marker that is not a valid date, or ends too late (`marker_max_days`), is ignored and logged once; `kelpie doctor` counts them on every local board ("snooze markers parse"). |
| `marker_max_days` | `30` | How long after its block was set (`Block.since`, when the agent wrote the reason) a marker may end, 1 to `max_days`. A bare date is counted by date: the date exactly `marker_max_days` after the block's is the last one allowed. A marker ending later is never honoured, even once that date comes within reach; a new block starts a new period. The default is lowered to `max_days` when that is smaller. |

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
| `end_worker` | `true` | bool | After a stop's block, end the run's worker processes (submitter, agent, the agent's descendants) once their pid and start fingerprint match what Hermes recorded; Linux (`/proc`) and macOS (libproc). `false`: left to Hermes' dispatcher, which reaps a worker it can verify at least 2 minutes later. See the [Hermes adapter](../src/kelpie/adapters/hermes/README.md). |
| `author` | `"kelpie"` | lowercase name | What Kelpie signs its comments, blocks, parks and wakes with on the board (`--author`, `HERMES_PROFILE_NAME`); its own comments are skipped when people's are read, so not the name of a profile or of one of `human_owners`. |
| `worker_grace` | `"5s"` | duration, > 0 and <= 30s | Between SIGTERM and SIGKILL when ending a stopped run's worker; it runs inside the tick, after the block. A stop takes about `cli_timeout` + `worker_grace` + 2s at most (plus waits on a busy board): on a `kelpie agent`, keep that well below `[hub] action_timeout`. |
| `state_home` | Hermes' home of `db_path` | path or `false` | Hermes' home: each profile's `state.db` (`state.db` itself for `default`, `profiles/<name>/state.db` for the others), read-only, at most once a minute, for the waits inside sessions of the last 6 hours (rule 2's `session_waits`) and each session's token usage within `lookback` (the daily report's spend per role). Default: the directory of `db_path`, or `<home>` for a named board's `<home>/kanban/boards/<slug>/kanban.db`. A home without any `state.db` is logged once. `false`: not read. |
| `state_sources` | `["kanban", "subagent", "cron"]` | list of Hermes session sources | Whose sessions are read for waits: Kanban workers, their subagents and cron jobs by default, not a person's own chats (`cli`, `telegram`, ...). |
| `prework_statuses` | `["triage", "scheduled"]` | list of `triage`, `scheduled` | Hermes statuses before any work starts: rule 3 alerts when a card sits there past `prework_after` with tasks depending on it. Kelpie's own parks in `scheduled` are `waiting`, not pre-work. |
| `human_owners` | `[]` | list of names | Assignees that are people, not agents; case-insensitive. Their tasks get a human owner, which `[connectors.human_tasks]` needs. Also changes `ping_pong`'s default escalation target and who `bottlenecks` and `orphan_status` count as agents. A person stays a person when another adapter lists the same name, in any case (e.g. a Claude Code task-list owner), and never counts as an idle agent. Comment and description authors with these names are people too (rule 1's acceptance criteria). |
| `boards` | none | array of tables | Several boards of this Hermes home: see [[[adapters.hermes.boards]]](#adaptershermesboards). Then `db_path` and `wait_key` are set per board; setting them here stops Kelpie at startup. |
| `human_decisions` | `[]` | array of tables: `marker`, `relays`, optional `person` | A person's decisions an agent records (`[[adapters.hermes.human_decisions]]`, e.g. `marker = "решение CEO"`, `relays = ["partner"]`, `person = "ceo"`): a comment, or a task description, by one of `relays` (profiles, case-insensitive) that opens with `marker` and a separator on its line (`:`, `-` before no word, an en or em dash; case-insensitive, past invisible characters and markdown emphasis: "Решение CEO: ...") is a person's, all of it, written by `person` (one of `human_owners`; else the relay). "Жду решение CEO", a quote or the marker further on are the agent's own words. Rule 1 then reads its acceptance criteria and verdicts as that person's; relayed comments are kept apart from people's own, so they never push those out. Relays are required, and may not be human owners, `kelpie` or `user` (Hermes' default CLI author), nor appear in two entries; `kelpie status` lists them: any agent can write the words. On a multi-host farm set it where the Hermes adapter runs (the `kelpie agent` host). |

### [[adapters.hermes.boards]]

One Kelpie watches several Kanban boards of one Hermes home (named boards,
`~/.hermes/kanban/boards/<slug>/kanban.db`), as one adapter. Everything that belongs to the
home stays in `[adapters.hermes]` and is shared by the boards: the spool, `cli`, profiles
and their `roles`, `human_owners`, `human_decisions`, `author`, `lookback`, `git_state` and
`[adapters.hermes.tools]`. Each board has:

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `name` | required | 1-32 of `a-z 0-9 - _`, starting with a letter or digit | Prefixes the board's ids: task `t_1a` of board `qr` is `qr:t_1a` in rules, alerts, the report and `kelpie why`, its run 42 is session `qr:hermes-run-42` (run ids restart on every board). Distinct per board. |
| `db_path` | required | path | The board's `kanban.db`. Opened read-only. Distinct per board. |
| `wait_key` | `hermes-wait-<name>.key` next to the ledger | path | Signs Kelpie's parks and stops on this board; a marker of one board never verifies on another. Distinct per board. |

```toml
[adapters.hermes]
spool = "~/.hermes/kelpie/spool.jsonl"   # one spool for the home: each line goes to its board
human_owners = ["alice"]

[[adapters.hermes.boards]]
name = "qr"
db_path = "~/.hermes/kanban/boards/qr/kanban.db"

[[adapters.hermes.boards]]
name = "sign"
db_path = "~/.hermes/kanban/boards/sign/kanban.db"
```

- **Spool.** Read once per tick; a line goes to the board that has its task and, under that
  task, its run. A line that names no Kanban task, or that two boards claim, goes to none (a
  session is never on two boards). With `db_path` alone (no `boards`) such lines are kept, unattributed.
- **Host-level readings exist once:** tool runs and slot pools (`[adapters.hermes.tools]`)
  are read once over every board's tasks; resources (`[resources.prometheus]`), OTLP ingest
  and the ledger are Kelpie's, not a board's. A stored OTLP span that names a task by its
  id on the board (`[traces] task_attribute = "hermes.kanban.task_id"` gives `t_1a`) is
  linked to `qr:t_1a` when exactly one board has that id.
- **Actions** (rule 10's stop, parks and wakes, comments) go to the board in the id's
  prefix, through `hermes` with that board's `HERMES_KANBAN_DB`; an id of no board is
  refused before anything runs.
- **A board that cannot be read** is left out of that tick and listed as unobserved: its
  open alerts are held, not resolved, and `kelpie_adapter_errors_total{adapter="hermes/<name>"}`
  counts it. The other boards go on. If none can be read, the adapter fails as one board would.
- `kelpie status` lists each board (`board qr: <path>`); `kelpie doctor` checks each
  (`kanban.db (qr)`, `worktrees readable (qr)`, `PR blocks have conditions (qr)`); the hooks,
  worker-stop and host checks run once.
- On a multi-host farm the same section goes in the `kelpie agent`'s config; ids are then
  `<host>:<board>:<id>`.

The single-table form (`db_path` in `[adapters.hermes]`) is unchanged: ids are not prefixed.
Moving from two Kelpie instances to one: [install.md](install.md#two-hermes-boards-on-one-host).

### [adapters.hermes.tools]

Long tool runs and tool slots, read from the Hermes host's process table with `ps` and
`lsof` (Kelpie runs there as the agents' user). Absent: not read. A task's live session is
busy (`Session.running_tool`, from the process's start) while a process named in `names`
is alive and it or one of its ancestors works in the task's workspace: the path Hermes
records for the task (the longest that holds it; a path several tasks share belongs to
none), or a directory matching `workspace_patterns`. Rule 9 holds its commit reminders
meanwhile (up to its `tool_grace`; an orphaned tool of an earlier run still in the workspace
counts too). `{task}` and Hermes' paths name unfinished tasks that do not share their
workspace. A command line is matched by the base names of its executable and, for an
interpreter (a shell, `env`, `python3` or a versioned Python, `node`, `xcrun` ...; an option's value such as `--sdk iphonesimulator` is taken for the script), of its script (the first next
word that is no option: `bash -x ios_sim.sh`; not `vim ios_sim.sh`) and never kept. A
failed read (a timeout, an error, more output than 4 MB) reuses the last good one for 5
minutes, so a slow `lsof` under a build does not remind mid-build; each cause is warned
once.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `names` | *required* | list of process base names | Tool processes that keep a task busy, e.g. `["xcodebuild", "ios_sim.sh"]`. No tool is built in. |
| `workspace_patterns` | `[]` | list of paths with `{task}` once | Directories of a task's workspace besides Hermes' own path, as path components; `{task}` is a task id on the board, e.g. `[".worktrees/{task}", "workspaces/{task}"]`; not `~` paths. A path several tasks share in Hermes is still searched with these. |
| `timeout` | `"5s"` | duration, > 0 and <= 30s | Limit for each `ps` or `lsof` call; all calls of one snapshot within three of them. |

`[[adapters.hermes.tools.slots]]` entries, pools of tool slots (simulators): empty lock
files held with `lockf -s -t 0 <slot> <command>` by agents and CI runners alike. A slot is
held while a process has its file open (Kelpie never takes a lock to probe one: a probe
would make a real `lockf -t 0` fail); its holder is an agent's when the holder or an
ancestor works in a task workspace (of an unfinished task), else CI's. Exported as `kelpie_tool_slots{pool}` and
`kelpie_tool_slots_held{pool,holder}` (`agent`, `ci`). A `kelpie agent` host does not send
them to the hub (only the board travels): read them where serve runs the adapter.

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | *required* | The pool's name (letters, digits, `.`, `_`, `-`), unique. |
| `paths` | *required* | Glob patterns of its lock files (`~` expanded; relative ones resolve against Kelpie's working directory), e.g. `["~/sim-slots/slot-*.lock"]`. |
| `size` | the files that exist | How many slots the pool has (a count, > 0). Set it unless the lock files are created once and kept (`lockf -k`): BSD `lockf` without `-k` deletes a slot's file when its command ends, so counting files would count only the held ones. |

## [adapters.claude_code]

The Claude Code platform adapter: shared task lists plus the spool of its command hook, and
stop markers the hook enforces. Absent: no Claude Code sessions. Details: the
[Claude Code adapter README](../src/kelpie/adapters/claude_code/README.md); first run:
[quickstart](quickstart-claude-code.md).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `task_lists` | `[]` | list of names | `CLAUDE_CODE_TASK_LIST_ID` values whose lists are read. `[]`: sessions only, no board tasks. |
| `tasks_dir` | `"~/.claude/tasks"` | path | Where Claude Code keeps task lists (`<tasks_dir>/<list>/<id>.json`). Read-only. |
| `spool` | `"~/.claude/kelpie/spool.jsonl"` | path or `false` | Spool written by the hook. `false`: no sessions, no hook spans. |
| `lookback` | `"24h"` | duration, > 0 | Sessions and completed tasks older than this are left out. |
| `stop_dir` | `"~/.claude/kelpie/stop"` | path or `false` | Where `stop` writes markers; the hook must read the same directory as the same user. `false`: `stop` becomes an alert. |
| `enabled` | `true` | bool | `false` keeps the section but does not build the adapter. |

## [adapters.openclaw]

The OpenClaw platform adapter: the spool written by the Kelpie OpenClaw plugin, the cards of
OpenClaw's Workboard plugin as tasks, and stop markers the plugin enforces. Absent: no OpenClaw sessions. Details: the
[OpenClaw adapter README](../src/kelpie/adapters/openclaw/README.md); first run:
[quickstart](quickstart-openclaw.md).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `state_dir` | `"~/.openclaw"` | path | OpenClaw's state directory (`OPENCLAW_STATE_DIR` on the Gateway host). |
| `workboard` | `"<state_dir>/plugins/workboard/workboard.sqlite"` | path or `false` | The Workboard database, opened read-only. Missing: sessions only (a warning once). `false`: not read. |
| `spool` | `"~/.openclaw/kelpie/spool.jsonl"` | path or `false` | Spool written by the plugin. `false`: no sessions, no hook spans. |
| `lookback` | `"24h"` | duration, > 0 | Sessions and finished cards older than this are left out. |
| `stop_dir` | `"~/.openclaw/kelpie/stop"` | path or `false` | Where `stop` writes markers; the plugin's `stopDir` must name the same directory. `false`: `stop` becomes an alert. |
| `cli` | `"openclaw"` | name or path or `false` | OpenClaw CLI that aborts a stopped session's run in flight (`openclaw gateway call sessions.abort`). `false`: markers only. |
| `stop_ttl` | `"24h"` | duration, > 0 | How long a stop marker holds; must match the plugin's `stopTtl`. |
| `cli_timeout` | `"20s"` | duration, > 0 and <= 60s | Limit for the abort of one stop (up to two commands: by session key, then by run id), plus up to 2 seconds to reap a command that timed out; it runs inside the tick. |
| `token_spans` | `true` | bool | LLM spans with each run's tokens from the plugin (it needs conversation access). Set `false` if OpenClaw's `diagnostics-otel` also sends its token spans to Kelpie: the daily report would count a run twice. |
| `enabled` | `true` | bool | `false` keeps the section but does not build the adapter. |

## [adapters.github_issues]

GitHub Issues as the task queue, for agents that take issues and return pull requests
(Copilot coding agent, Codex cloud, the Claude Code GitHub Action, OpenHands). Adapter name
`github-issues`; task ids are `owner/repo#12`. Actions (`requeue`, `unblock`, `comment`,
`reassign`) go through the issues API; the token then needs Issues: write, and Kelpie's notes
(quoting rule reasons) are as public as the repository.
Where to reach GitHub defaults to [[connectors.github]](#connectorsgithub); the adapter
makes its own client from it (its own cache and per-tick budget). The status and owner
rules: the [issues adapter README](../src/kelpie/adapters/issues/README.md); first run:
[quickstart](quickstart-github-issues.md).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `repos` | `[connectors.github]` `repos` | list of `owner/name` | The repositories whose issues are tasks (one board each). Required without `[connectors.github]`. |
| `api_url` | `[connectors.github]` `api_url`, else `"https://api.github.com"` | URL | GitHub's API (https, or http on loopback). |
| `token_env` | `[connectors.github]` `token_env`, else `"GITHUB_TOKEN"` | variable name | The token: read access to issues and pull requests (metadata). Set explicitly (here or in the connector), it must be set. Unset: anonymous, 60 requests an hour. Required here when `api_url` differs from the one the inherited token is for. |
| `timeout` | `[connectors.github]` `timeout`, else `"10s"` | duration, <= 60s | One request. |
| `cache_ttl` | `[connectors.github]` `cache_ttl`, else `"60s"` | duration, >= 30s | How long an answer is reused before it is asked again (conditionally). |
| `lookback` | `"7d"` | duration, > 0 | Closed issues updated within this are read (done and cancelled tasks; one page of 50). |
| `max_requests_per_tick` | `60` | count, 1–5000 | Requests per tick for per-issue details (events, blockers, sub-issues); the rest use what was read before. The lists are always read first, bounded by their page caps (up to 21 requests per repository; unchanged pages are answered `304 Not Modified`, free against the rate limit). |
| `tick_budget` | `"15s"` | duration, > 0 | Wall-clock time per tick for the per-issue details (the lists are bounded by their page caps and `timeout`). |
| `label` | none | label | Only issues with this label are tasks. None: every issue. On a public repository, set one only maintainers apply: otherwise anyone who opens issues fills the task list (500 open issues per repository are read). |
| `blocked_labels` | `["blocked"]` | list of labels | An open issue with one is `blocked`, since the label was put on. |
| `ready_labels` | `[]` | list of labels | An open issue nobody holds with one is `ready` (waiting for an agent); without one it is `backlog`. People start these agents, not a dispatcher, so only a label says an issue waits for one. |
| `role` | `"issue-agent"` | string | The role of tasks no agent holds, and the agents' default. Distinct from other adapters' roles, so rule 3 never proposes an issue agent for another platform's task (or the reverse) unless you give them the same role. |
| `agents.<name>` | none | table | An agent that works issues; `<name>` is its owner id in Kelpie. Keys: `assignee` (the login it is assigned as, e.g. `Copilot`), `label` (the label that starts it, e.g. `fix-me`), `author` (the login its pull requests come from, e.g. `copilot-swe-agent[bot]`), `mention` (the comment text that starts it, e.g. `@codex`: `requeue` and `reassign` post it), `role` (default `"issue-agent"`). One of `assignee`, `label` or `author` is required. An account or label names one agent, and an agent's label cannot also be a board's `label`, blocked or ready label. |
| `boards.<owner/name>` | the keys above | table | Per repository: `label` (`false`: every issue), `blocked_labels`, `ready_labels`, `role`, and `agents` (the agent names Kelpie may hand its tasks to; default all). |
| `enabled` | `true` | bool | `false` keeps the section but does not build the adapter. |

## [adapters.linear]

Linear issues as the task queue, with the same status and owner rules as
[[adapters.github_issues]](#adaptersgithub_issues) (the [issues adapter
README](../src/kelpie/adapters/issues/README.md), "Linear"). Adapter name `linear`; boards are
teams, task ids Linear's identifiers (`ENG-123`). Agents integrated in Linear are app users an
issue is **delegated** to: name the app user as an agent's `assignee`.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `teams` | (required) | list of team keys | The teams whose issues are tasks (`ENG`), one board each. |
| `token_env` | `"LINEAR_API_KEY"` | variable name | A Linear API key (personal, `lin_api_…`; an OAuth token is sent as Bearer). Required: Linear has no anonymous access. Writes need a key that may comment and edit issues. |
| `api_url` | `"https://api.linear.app/graphql"` | URL | https, or http on loopback; a different one needs `token_env` set here. |
| `timeout` | `"10s"` | duration, <= 60s | One request. |
| `refresh` | `"2m"` | duration, >= 30s | How often the issues are read again (GraphQL has no conditional requests, and Linear meters requests and query complexity per hour); between refreshes the last read is served. A write makes the next tick read again, unless reads are stretched: they take at most half the hour's complexity limit (25 minutes apart at most). |
| `lookback` | `"7d"` | duration, > 0 | Closed issues updated within this are read (the most recently updated page of 25). |
| `max_requests_per_tick` | `20` | count, 0–5000 | Per-issue history reads per refresh (who was assigned, labels put on, who edited the description); the rest use what was read before. The lists are always read, bounded by their page caps (pages of 25, at most 20 open and 1 closed per team). |
| `pull_repos` | `[]` (github.com, any repository) | list of `owner/repo` or `host/owner/repo` | The repositories whose pull requests, attached by Linear's GitHub integration, count as an issue's (`review`). Links anyone pastes never count. |
| `label`, `blocked_labels`, `ready_labels`, `role` | as for GitHub Issues | | A `ready_labels` label makes an issue nobody holds `ready`; a started state with nobody on it is `in_progress` with no owner (rule 3 may hand it to one of the board's agents). |
| `ready_states` | `[]` | list of workflow state names | An issue nobody holds in one of these states (`["Todo"]`, any case) is `ready`; otherwise `backlog`, as on GitHub. |
| `agents.<name>` | none | table | As for GitHub Issues; `assignee` is a Linear display name (a person, or an agent's app user, which Kelpie delegates to; printable, not ending in `[bot]`), `label`, `mention` (unverified on Linear), `role`. No `author`: Linear shows no pull request author. |
| `boards.<TEAM>` | the keys above | table | Per team: `label` (`false`: every issue), `blocked_labels`, `ready_labels`, `ready_states`, `role`, `agents`. |
| `enabled` | `true` | bool | `false` keeps the section but does not build the adapter. |

## [traces]

OTLP/HTTP trace ingest on `POST /v1/traces`, on the same port as `/metrics`. On by default.
What is kept from each span: [privacy.md](privacy.md).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `enabled` | `true` | bool | `false`: no ingest, no span store, no budgets. |
| `token_env` | `"KELPIE_TRACES_TOKEN"` | variable name | Bearer token exporters must send. Used when the variable is set. Written here, the variable must be set or Kelpie refuses to start. With `token_file`: the variable name inside that file. |
| `token_file` | none | path | An env file holding `<token_env>=<token>`, read instead of the environment and read again when it changes, so the token can be rotated without a restart (below). It must exist, hold the token and be a regular file of the user `kelpie serve` runs as (not a symlink), or Kelpie refuses to start; `kelpie traces rotate-token` creates it. A relative path is taken from the working directory, like `[storage] path`. Not read with `[hub]` (that port takes no spans). |
| `token_grace` | `"2h"` | duration, 0 to 7d | How long `kelpie traces rotate-token` keeps the replaced token valid. 0: it accepts no previous token at all. |
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

**Rotating the token.** With `token_file` set, `kelpie traces rotate-token [--grace 2h]`
writes a new token to the file (mode 600, replaced atomically under a lock; other lines
are kept, commented-out copies of the token lines are dropped; a missing folder is
created with mode 700) and keeps the token it replaces valid for the grace period, so sessions that
started with it keep reporting. `--grace 0s` accepts no previous token at all, revoking
those of earlier rotations too. The replaced token is kept only as a SHA-256 hash with its expiry, on the
line `<token_env>_GRACE`. It prints no token, only what it did and how many previous
tokens are still accepted. It runs only as the user who owns the file's folder and
refuses a symlink, a file of another user and a folder others may write to.
`kelpie serve` reads the file again within a second, and only trusts a regular file of
its own user (or root) that others may not write, in a folder others may not write;
otherwise it keeps the tokens it read before and `kelpie_traces_token_file_ok` is 0.
`/metrics` has `kelpie_traces_grace_tokens` (previous tokens still accepted) and
`kelpie_traces_grace_requests_total` (requests that used one: agents not on the new token
yet). `kelpie doctor` warns when the token is in a backup (`*.bak*`, `*~`, `*.orig`,
`*.old`) or a file others can read under the token file's folder, the Hermes home or
`~/.claude`, and lists the other files that hold a copy. See
[security.md](security.md#traces-token).

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

Examples for Langfuse, Phoenix and Datadog are in [How Kelpie works, "Forwarding"](guide.md#forwarding-to-langfuse-phoenix-or-datadog).

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
| `owners` | `{}` | table: role -> owner | Who owns each role's instructions: rule `output_offenders` addresses its comment to them, and the daily report names them. |
| `filters` | built-in table | array of tables | Replaces the built-in filter table as a whole; first match wins. |

With `[traces.outputs]` present, `[traces] retention` must cover `repeat_days` and twice
`window`.

`[[traces.outputs.filters]]` entries (built-in, in order: `rtk`, `xcbeautify`,
`errors-only` for shell commands; then for other tools `downscale-images` for image tools
(`vision*`, `*_vision*`, `*screenshot*`, `*_image`, ...), `range` for file reads,
`compact-view` for tools that show a whole record (`*_show`, `show_*`, e.g. Hermes'
`kanban_show`) and `ask-less` for any other tool, so every tool is ranked by its output tokens
with advice for its kind. Globs match on word boundaries (`get_revision` is no vision tool).
A shell tool's calls that carry no command (`tool:terminal`, `tool:Bash` with `[traces]
commands = false`) group every command the role ran: the catch-all leaves them alone):

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | *required* | Filter name. |
| `how` | *required* | One line telling the operator how to apply it. |
| `commands` | *required* | Globs over the command prefix or executable; `*` is any shell command, `tool:*` any other tool. |
| `reduction` | *required* | Expected share of output tokens removed, between 0 and 1 (exclusive). |
| `platforms` | any | Agent platforms the filter works with, e.g. `["hermes", "claude-code"]`. |
| `advice` | none | For advice that is not a filter (the built-in tool rows): what to do, as a title (`"Downscale images"`). |

## [[budgets]] and [prices]

Spend is priced from traces, so budgets need `[traces]` enabled.

`[[budgets]]` entries:

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `name` | *required* | string | Unique. |
| `scope` | *required* | `day` \| `agent` \| `task` \| `project` | `day`: the whole farm. `agent`: the session's owner. `task`: the span's task. `project`: tasks matched by `match`. |
| `unit` | *required* | `usd` \| `tokens` | `usd` needs prices: the default table, or `[prices]` when it is off. |
| `soft` | unset | number, > 0 | Alert at this spend. |
| `hard` | unset | number, > `soft` | Stop the run at this spend (act mode, through `loop_breaker`): `agent` and `task` budgets; `day` and `project` budgets only alert unless `loop_breaker` has `stop_on_shared_budgets = true`. At least one of `soft` and `hard` is required. |
| `match` | unset | string | `project`: `label:<name>` or a task id glob (*required*); all matching tasks share one budget. `agent`/`task`: limits the budget to the owners or tasks it names (an id or a glob); each matching agent or task still has its own budget, so `match = "agent-*"`, `hard = 50` is $50 per agent, not in total. Without it every agent or task has one. Not allowed for `day`. |
| `window` | `"24h"`; `task`: `[traces] retention` | duration, > 0 | Rolling window. Must not exceed `[traces] retention`. |
| `burn_window` | `"1h"`, or `window` if shorter | duration, <= `window` | Burn rate and "runs out at" forecast. |
| `mode` | `"api"` | `api` \| `subscription` | `subscription`: spend is an estimate, and the forecast is the time to the next provider limit. |
| `providers` | all | list of provider names | `subscription` only: the limits whose hits drive the forecast (names from `[connectors.llm_quotas]`). |
| `min_limit_window` | `"10m"` | duration, > 0 | `subscription` only: ignore limit hits whose limit refills sooner than this. |

`[prices."<model or glob>"]`, US dollars per 1 million tokens. They override Kelpie's
default table (see [[spend]](#spend)). The first match wins:

1. an entry named exactly as the span's model;
2. the first glob, in config order, matching the model as written;
3. an entry for the same model snapshot as written by another platform: lower case,
   without a Claude Code `[1m]` tag, a `vendor/` or `models/` prefix (OpenRouter,
   Gemini), Bedrock's `anthropic.` and region prefixes and `-v1:0` suffix, or Vertex's
   `@20251001`, with dotted Claude versions read as dashed (`claude-sonnet-4.5`);
4. an entry without a date for the model's family: `[prices."claude-haiku-4-5"]` prices
   `claude-haiku-4-5-20251001` and `us.anthropic.claude-haiku-4-5-20251001-v1:0`, while a
   dated entry such as `[prices."gpt-4o-2024-08-06"]` prices only that snapshot;
5. the first glob matching the normalized id or the family (case-insensitive);
6. the default table, by family.

Tokens of a model without a price still count for `tokens` budgets; their cost is unknown
and left out of `usd` budgets, and `kelpie status` and `kelpie doctor` name such models.

| Key | Default | Meaning |
| --- | --- | --- |
| `input` | *required* | Input tokens (cache reads and writes excluded). |
| `output` | *required* | Output tokens. |
| `cached_input` | `input` | Input tokens read from the prompt cache. |
| `cache_write` | `input` | Input tokens written to the prompt cache (Anthropic: 1.25 × `input` for 5-minute writes). |

Cache reads and writes come from the spans: `gen_ai.usage.cache_read.input_tokens` and
`gen_ai.usage.cache_creation.input_tokens` (also `gen_ai.usage.cache_write.input_tokens`
and OpenLLMetry's `gen_ai.usage.cache_read_input_tokens` /
`gen_ai.usage.cache_creation_input_tokens`), Claude Code's `cache_read_tokens` /
`cache_creation_tokens`, and OpenClaw's usage records. A `[prices]` entry replaces the
default row as a whole: without `cache_write` it prices cache writes at its `input`, not at
the default's 1.25 × input.

## [spend]

| Key | Default | Meaning |
| --- | --- | --- |
| `default_prices` | `true` | Price models from Kelpie's default table when `[prices]` has no entry for them. `false`: only `[prices]`. |

`[spend.providers.<name>]`, one per model provider (1-63 letters, digits, `_` or `-`:
`anthropic`, `openai`, `google`, or your own, such as a gateway):

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `"auto"` | `api`: pay per token, spend is dollars. `subscription`: a plan with a usage limit, spend is tokens and the share of the limit (dollars only as an estimate). `auto`: decided from the rest of the config, below. |
| `models` | `[]` | Model names or globs that belong to this provider (on the id as written, or case-insensitively on its family id; for reconciling with a cost report, another platform's ids are claimed only by a pattern that names the platform, such as `*.anthropic.*`, `*@*` or `openrouter/*`). Needed only for models the default table and the name rules below do not place. |
| `quotas` | `[]` | Names of `[connectors.llm_quotas]` probes that watch this provider's limits, so a subscription budget's `providers` (those names) can tell this provider's mode. |
| `limit_tokens` | unset | Subscriptions: the tokens your plan allows per `limit_window`, as you know them; Kelpie never guesses a limit. |
| `limit_window` | unset | Duration (`"5h"`, `"7d"`), together with `limit_tokens`. |

**The provider of a model:** the first `models` pattern that matches, then the provider the
default table knows it under, then its name: `claude-*` is `anthropic`, `gpt-*`,
`chatgpt-*`, `codex-*` and `o<digit>*` are `openai`, `gemini-*` is `google`.

**`auto` mode** reads nothing but the config, so every command gives the same answer;
first match wins:

1. the provider has a [[connectors.cost_reports]](#connectorscost_reports) table (an
   admin key: there is a bill): `api`;
2. a `[[budgets]]` entry with `mode = "subscription"` whose `providers` name the provider or
   one of its `quotas`, or that names no providers (it covers them all): `subscription`;
3. `limit_tokens` is set: `subscription`;
4. a `day` budget (the whole farm) in `usd` with mode `api`, the budget default: `api`
   (weak evidence: set `mode` to be sure);
5. otherwise `unknown`: `kelpie doctor` warns and says what to set.

A model whose provider nothing tells is `subscription` only under a subscription budget
that names no providers (it covers them all); otherwise `unknown`.

A subscription budget whose `providers` names a probe that no `quotas` lists sets no
provider's mode; `kelpie serve` logs it and `kelpie doctor` warns.

`kelpie status` lists each configured provider and each provider seen in the last 7 days
of spans with its mode and the reason; `kelpie doctor` warns about providers without a
mode and models whose provider nothing tells.

**The default table** (`kelpie.core.prices`) holds the standard API prices of popular
Anthropic, OpenAI and Google models, as their pricing pages showed them on the date
`kelpie status` prints ("checked ..."). Each row has input, output and cache read prices,
and Anthropic rows the 5-minute cache write price (the OpenAI and Google rows have none, so
their cache writes cost the input price). An announced price change (an introductory price that
ends) applies from its day, and a row prices every snapshot of its model. Prices change
after Kelpie is released: check the rows you rely on against the provider's page, and add
`[prices]` entries where they differ. The estimate leaves out what the table does not
model: prompt-length tiers (Claude Haiku 5.5 above 100k prompt tokens, Claude Sonnet 4.5
and 4 above 200k, OpenAI above 272k, Gemini above 200k cost more; the base price is
used), batch and flex discounts, priority and fast tiers, regional and US-only uplifts,
and Gemini's cache storage fee. Budgets, `kelpie status` and `kelpie doctor` use the
default table, and so does the daily report, which follows each provider's spend mode
and names the models without a price.

| Provider | Models | Source |
| --- | --- | --- |
| Anthropic | Claude Fable 5.1 / 5, Mythos 5.1 / 5, Opus 5.5, 5, 4.8, 4.7, 4.6, 4.5, 4.1, 4, Sonnet 5.5, 5, 4.6, 4.5, 4, Haiku 5.5, 4.5, 3.5 | [platform.claude.com pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| OpenAI | GPT-5.5, GPT-5.4, GPT-5.4 mini, GPT-5 | [developers.openai.com pricing](https://developers.openai.com/api/docs/pricing) |
| Google | Gemini 4 Argon, Gemini 3.8 Flash, Gemini 3 Flash Preview | [ai.google.dev pricing](https://ai.google.dev/gemini-api/docs/pricing) |

## [instructions]

The instruction files each role loads (skills, `CLAUDE.md`, `AGENTS.md`, role prompts),
measured for rule [`instruction_bloat`](#instruction_bloat) (incident 21). Off unless the
section is present: with it, Kelpie reads these files on its own host, read-only (a file that is itself a
symlink is skipped), and stores only their sizes and hashes (see
[privacy](privacy.md#instruction-files)); it never edits them. Files on a `kelpie agent`
host are not measured yet.

| Key | Default | Type | Meaning |
| --- | --- | --- | --- |
| `defaults` | `true` | bool | Also measure the platform defaults of the adapters configured here (below). |
| `every` | `"1h"` | duration ≥ 1m | How often the files are measured. |
| `window` | `"7d"` | duration ≥ 1d | Growth, appends and edits are counted over this. |
| `max_bytes` | `2000000` | integer > 0 | A larger file is skipped and logged. |
| `max_files` | `2000` | integer > 0 | Files measured per scan, at most. |
| `similarity` | `0.8` | number in (0, 1] | Two sections whose bodies are this alike (Jaccard over 3-word shingles) are near duplicates. |
| `same_heading_similarity` | `0.5` | number in (0, 1] | ...or this alike under the same normalised heading. |
| `min_value_files` | `3` | integer ≥ 2 | The same `key: value` line in this many files of a role is one value kept in several places. |

`[[instructions.files]]` adds files; the first entry naming a path wins, and these come
before the defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `paths` | required | Globs (`~` expanded, `**` any depth). `{role}` in a path matches one path part and names the role; no `**` before it. |
| `role` | every role | The role that loads them (not with `{role}`). |
| `loaded` | `"always"` | `always`: read into every session of the role; `on_demand`: opened when needed (a skill). |
| `owner` | unset | Who owns these instructions; named in the alert. |

Platform defaults (`loaded` in brackets): Hermes `~/.hermes/SOUL.md` (always),
`~/.hermes/skills/**/SKILL.md` (on demand), and per profile `~/.hermes/profiles/{role}/SOUL.md`
and `.../skills/**/SKILL.md`, the profile mapped to its role as `[adapters.hermes] roles`
does; Claude Code `~/.claude/CLAUDE.md` (always), `~/.claude/skills/*/SKILL.md` (on demand),
`~/.claude/agents/{role}.md` (always); OpenClaw `AGENTS.md`, `SOUL.md`, `TOOLS.md` (always)
and `skills/*/SKILL.md` (on demand) in `<state_dir>/workspace` and `<state_dir>/workspace-{role}`.
A project's own `CLAUDE.md` and `.claude/skills` live in its checkout: list them under
`[[instructions.files]]`.

Tokens a day spent reading a file are its estimated tokens (characters / 4) times the
sessions of its role (on its platform, for the defaults) that started in the last 24 hours
(`token_basis = "sessions"`; for an on-demand file an upper bound). Counting real skill
loads (Hermes `skill_view` calls, Claude Code skill loads) is not done yet.

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
| `growth_window` | `"6h"` | duration, 30m to 7d | Window of the disk and directory growth rates (`deriv`): rule 7's time-to-full forecast. |
| `growth_sustain` | `"1h"`, or `growth_window` when that is shorter | duration, 5m to 24h and at most `growth_window` | How long growth must go on to count. The growth rate is the rate over `growth_window`, capped at the first quartile of the rates in the slices (`growth_sustain` / 4, at least 1m, aligned to the clock) of the last `growth_sustain`, a slice that shrank counting as 0. So a burst within about half of `growth_sustain` adds a few GB a day at most, for about one slice (5 GB written in 17 minutes reads at most about 5 GB a day on a flat disk, 35 on one growing 30 GB a day), then nothing; a cleanup of less than a few hours' growth leaves about the rate (an open forecast does not resolve), while a larger one turns the window's trend negative, so an open forecast resolves until the cleanup leaves the window (about 4 to 5 hours with the 6h default); a longer burst counts in part (5 GB in 40 to 50 minutes reads up to about 15 to 18 GB a day). Growth that comes in bursts with gaps longer than a slice (a build every 30 minutes, nothing written in between) reads as no growth. A shrinking trend stays negative: no forecast. |
| `load_on_macos` | `false` | bool | macOS counts threads waiting on I/O in its load average (300 to 800 on a healthy farm Mac), so hosts whose `node_uname_info` says Darwin get no load reading unless this is true. Needs node_exporter's uname collector (on by default), as does leaving out macOS's read-only volumes; a host once seen as macOS stays so when that query fails, and until it first answers no host gets a load reading. |

`[resources.prometheus.queries]` keys: `disk_size`, `disk_avail`, `memory_total`,
`memory_available`, `load1`, `cores`, `cpu_busy`, `processes`, `processes_max`, `os`,
`disk_growth`, `path_bytes`, `path_growth`, `path_written`. The defaults read Linux and macOS
alike: memory is `MemAvailable`, else free + inactive (macOS); processes are node_exporter's
`--collector.processes` (Linux), else the agent user's processes against
`kern.maxprocperuid` (or a lower process ulimit) from `scripts/kelpie-process-limit.sh` (macOS), while its file is under 5 minutes old; disks leave out
read-only filesystems on macOS (its sealed `/`, simulator runtimes, disk images; on Linux a disk remounted read-only stays in sight) and the APFS
system volumes, so a Mac shows its data volume, `/System/Volumes/Data`. Each result must keep the
`host_label` label (and `mountpoint` for disk, `path` for directories, whose `mountpoint`
names the filesystem they are on). `path_bytes` and `path_growth` read `kelpie_path_bytes`
from `scripts/kelpie-path-sizes.sh` (install.md, Host resources); `path_written` is when
node_exporter last saw that file change, so sizes from a script that stopped count as old.
The growth queries take the lower of two rates (`deriv`, in bytes per day): over
`growth_window` (the trend) and the first quartile over the slices of the last
`growth_sustain`, each at least 0 (`quantile_over_time` of a subquery, Prometheus 2.7 or
later; doctor warns when the query fails). The trend is not clamped: a disk that shrinks
over the window reads negative and forecasts nothing. A slice needs at least two scrapes, or
the query answers nothing: keep `growth_sustain` at least 8 scrape intervals long (doctor
notes a host with no growth rate). The growth queries only answer for series
that existed half a window ago, so a new Prometheus or host forecasts nothing at first. The
growth and directory queries may fail or be empty without costing the other readings (each
failure is counted in `kelpie_resource_read_failures_total`, cause `<query>_<cause>`). In
every query, custom ones too, `{host_label}`, `{window}` (`growth_window`), `{half_window}`,
`{sustain}` (`growth_sustain`) and `{slice}` (a quarter of it, at least 1m) are filled in; a
custom `disk_growth` or `path_growth` is sustained only if it uses `{sustain}` and `{slice}`
itself. The defaults are in `kelpie.example.toml`. `processes` needs node_exporter's
`--collector.processes`.

## [connectors.github]

Read-only GitHub connector: evaluates `pr_merged` and `ci_status` conditions for the listed
repositories only.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `repos` | *required* | list of `owner/name` | Nothing else is ever queried. |
| `token_env` | `"GITHUB_TOKEN"` | variable name | Token with read access to PRs and CI, see [token permissions](#github-token-permissions). Unset variable: anonymous (60 requests an hour, public repos). Written here, the variable must be set. |
| `api_url` | `"https://api.github.com"` | URL | GitHub Enterprise: `https://<host>/api/v3`; `token_env` must then be set explicitly. |
| `timeout` | `"10s"` | duration, > 0 and <= 60s | Whole request. |
| `cache_ttl` | `"60s"` | duration, >= 30s | Answers are reused this long, then revalidated by ETag. |
| `max_requests_per_tick` | `50` | count, 1 to 5000 | Beyond this, cached answers or an unknown result. |
| `tick_budget` | `"10s"` | duration, > 0 and <= 60s | Wall-clock time for GitHub per tick. |
| `ci_workflows` | `[]` | list of workflow names or paths, or a table `"owner/name" = [...]` | The workflows that **must** have a result for `ci_green` / `ci_status`, whatever check runs and statuses say (see below). Each entry is a workflow's name (its `name:`, or the file path if it has none) or its file path such as `.github/workflows/ci.yml`, like `cancel_workflows`; matched exactly, case included. A name that several workflow files share matches all of them: use the path, which is exact. A list applies to every repo; a table gives lists per repo (keys must be in `repos`; a repo left out has no list). Empty: no workflow is required. Needs `Actions: read`. At most 50 per list. |
| `enabled` | `true` | bool | `false` keeps the section but does not query GitHub. |

### GitHub token permissions

The least a read-only fine-grained personal access token needs, on the configured
repositories (repository permissions, all **read-only**):

| Permission | For |
| --- | --- |
| Metadata | Always granted: repository identity. |
| Pull requests | `pr_merged`, the PR head commit, open PRs and their files; the sample commit `kelpie doctor` probes with. |
| Contents | Rule 9 (`[connectors.github.ci]`) reads runs and pull request data with it. CI conditions alone do not need it. |
| Actions | Workflow runs: rule 9 (`[connectors.github.ci]`) and the CI fallback below. |
| Commit statuses | The combined commit status (legacy CI and external services). |

Plus, only for `[connectors.github.ci]` `billing_user`: the user permission **Plan**
(read), on a token of its own (see below). Cancelling builds needs `Actions: write` on yet
another token. **Checks** is not in the list because fine-grained personal access tokens
cannot be given it (a GitHub App can).

**Where CI is read from.** A `ci_status` condition (and a `ci_green` block condition) is
decided per commit from the check runs and the combined status. When one of those answers
HTTP 403, which is what a token without `Checks` or `Commit statuses` gets, Kelpie also
reads the commit's workflow runs (`GET /repos/{repo}/actions/runs?head_sha=...`,
`Actions: read`) and judges them with whichever of the two it can still read; everything it
reads must be green. What is **not** seen then:

- With check runs refused, check runs of GitHub Apps other than Actions are invisible.
- With statuses refused, so are commit statuses, which is how Jenkins, Buildkite or
  CircleCI usually report. Actions runs cannot stand in for them. Grant
  `Commit statuses: read` if any CI reports that way; `kelpie doctor` warns about it.

A 403 for a rate limit or for an organization policy (SAML SSO enforcement, an IP allow
list, an organization that forbids classic tokens, recognised by GitHub's own wording or
the `X-GitHub-SSO` header) is not a missing permission: the result is unknown
(`params.error` is `rate_limited` or `policy`) and nothing is remembered. Any other 403 is
taken as a refusal, including "Actions is disabled ... by an enterprise policy": the runs
cannot be read either, so the result is unknown and, again, nothing is remembered. That a repository needs the fallback is remembered for an hour,
and only when the Actions runs could be read in the same probe; a tick then asks for the
runs only, and the two refused calls are tried again after the hour, in case the
permission was granted. `params.ci_source` on the condition says which sources were read
(`statuses+check-runs`, `statuses+actions`, ...); `kelpie doctor` prints it per repository
with the exact permission each refusal means (a missing `Actions: read` or `Pull requests:
read` fails, a refused `Commit statuses` warns).

**Verdict.** Every readable result and the Actions verdict are combined; the first row that
applies wins.

| Sources say | Verdict |
| --- | --- |
| any red (failure, error, cancelled, timed out, `action_required`, ...) | `failure` |
| more results than were read (statuses or check runs over 100, runs over 300) | `incomplete`, unknown |
| nothing at all (no status, check run or counted run) | `none`, never green |
| something still running, or a `ci_workflows` entry without a result | `pending` |
| check runs refused, the Actions runs have none that count, a readable source is green, and the repository has an active workflow of its own | `pending` (the real CI may not have started) |
| every result green, at least one `success` | `success` |

The Actions runs themselves (`kelpie.connectors.github.ci_runs`):

- What can make the commit green, and satisfy a `ci_workflows` entry, is a run that counts
  *in full*: a run of a listed workflow, whatever triggered it; with `ci_workflows` empty, a
  run of the events `push`, `pull_request` and `merge_group`. A `pull_request` run counts in
  full only for the pull request the condition names (the run's `pull_requests`): the same
  commit in two PRs does not share a green, and a run whose PR is unknown (from a fork) is
  not trusted for one.
- **Every run that does not count in full counts one-sided**, whatever the reason: an
  unlisted workflow, another event (`workflow_run`, `pull_request_target`, `issue_comment`,
  `schedule`, `workflow_dispatch`, `repository_dispatch`, ... , whose relation to the commit
  `head_sha` cannot vouch for), another PR's run on the same head, or a run with an unknown
  PR (a failed fork `pull_request` run, listed or not). A concluded red makes the commit
  `failure`; it never makes it green. It makes it `pending` only while a re-run (attempt 2
  or more) is in progress, because the runs list shows only the latest attempt and would
  otherwise forget the red it replaced. GitHub bounds that wait as it bounds the job on the
  check-run path (a job queued without a runner is cancelled after 24 hours; a run waiting
  for an environment approval can wait up to 30 days). A one-sided run still on its first
  attempt is not waited for: it reads as nothing until it fails. List a workflow in
  `ci_workflows` to wait for it. So `ci_workflows` and the PR filter add requirements and
  remove nothing: a failed unlisted `Lint` run is red with or without a list, as its check
  run would be on the check-run path.
- Per workflow and event (and per set of PRs for the one-sided runs) only the latest run
  counts: the one that started last (a re-run starts again), then the highest attempt. A
  later run that was skipped does not replace an earlier real result. A failed first attempt
  that a re-run fixed is green; a failed `pull_request` run is not hidden by a later manual
  or `push` run.
- Green only when every counted workflow concluded `success`. `failure`, `cancelled`,
  `timed_out`, `action_required`, `startup_failure` and `stale` make it red, even while
  another workflow is still running. A run that is queued, waiting or in progress, or
  completed with a conclusion Kelpie does not know, makes it pending.
- `skipped` and `neutral` do not block, but they are no evidence of passing CI either: if
  nothing but skipped or neutral runs counts, or no run exists for the commit, the runs
  have nothing to say. Alone that is `none`. Beside a green status it is `pending` while
  check runs are refused and the repository has an active workflow of its own (read from
  `GET /repos/{repo}/actions/workflows`, remembered for an hour; GitHub's built-in
  `dynamic/...` workflows, such as Dependabot updates, CodeQL default setup and pages
  builds, do not count): the status decides only for a repository without one. With check
  runs readable they already include Actions' own, and this guard is off. The check-run
  path counts `skipped` and `neutral` as success, because GitHub itself has reported those
  checks.
- `ci_workflows` lists the workflows that must have run, for example the aggregator
  workflow, so that a fast DCO or labeler check cannot make a commit green before the real
  CI exists. It adds requirements and removes nothing from the readable sources: check runs
  and statuses, where readable, always count in full. It holds on **every** path: when it is
  set for a repo, the Actions runs are always read, whatever check runs and statuses say.
  Among the runs the listed workflows are required: one with no run on the commit, or
  with nothing but skipped runs, makes the state `pending`; unlisted runs can only add red. If the runs cannot be
  read (`Actions: read` missing, Actions disabled), the state is unknown, never green; a red
  check run or status still gives `failure`. `kelpie doctor` checks every entry against the
  repository's workflows (a name that matches none, or only a disabled workflow, fails; a
  name shared by several files warns, use the path; with more than 100 workflows these
  become warnings) and fails when the runs cannot be read.
- A commit on which no workflow runs (path filters, a fork PR) stays `pending` while a
  repository has an active workflow and check runs are refused; that is the price of not
  trusting a lone status. List the workflows that gate merging to make the wait explicit.
- Up to 300 runs of a commit are read (3 pages of 100 within the tick budget; a run seen
  twice because one started between two pages counts once). More runs than that, or a
  total that grew between pages, with no failure among those seen, is unknown.
- The runs are asked for by the full commit id: a `ci_status` condition naming an
  abbreviated `sha` is unknown while a source is refused or `ci_workflows` is set.

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
| `included_minutes` | unset | minutes, > 0 | Monthly quota; becomes a `ci_minutes` resource for the forecast. Minutes used are read only with `billing_user` or `billing_org`; otherwise rule 9 projects the burn. |
| `quota_reset_day` | `1` | day of month, 1 to 28 | When the quota resets (UTC). |
| `allow_cancel` | `false` | bool | Allow `cancel_build`. |
| `cancel_repos` | `[]` | list | Required with `allow_cancel`; among `repos`. |
| `cancel_workflows` | `[]` | list of workflow paths | Only runs of these workflows, e.g. `.github/workflows/ci.yml`. Empty: any. |
| `cancel_branches` | `[]` | list of globs | Only runs on these branches. Empty: any branch but the default branch, which is never cancelled. |
| `cancel_token_env` | unset | variable name | A separate token with Actions: write on `cancel_repos`, used only for the cancel request. |
| `billing_user`, `billing_org` | unset | login | Read minutes used from this user's or organization's billing usage report (one of them; needs `included_minutes` and `billing_token_env`). |
| `billing_token_env` | unset | variable name | A token of its own that may read the account's billing usage; not `token_env` or `cancel_token_env`. |
| `billing_interval` | `"15m"` | duration, 5m to 1h | One read this often. A reading is kept for two intervals and only within its quota period. |
| `billing_skus` | `["actions_linux", "actions_windows", "actions_macos"]` | non-empty list | SKUs that draw on `included_minutes`; any case, spaces or hyphens read as `_`. |

**Minutes used.** With `billing_user` or `billing_org`, Kelpie reads GitHub's billing usage
report (`/users/{user}/settings/billing/usage` or `/organizations/{org}/...`, the enhanced
billing platform) and counts Actions minutes of `billing_skus` dated from the last reset to
today (UTC). These minutes are counted as reported, without Windows or macOS multipliers, so the
reading errs low. Rule 9 then calls the quota exhausted only from that reading (a critical
alert naming the tasks that wait on CI). That alert means builds stop, which holds only
when usage stops at the included minutes: a $0 Actions budget, the default for a personal
account. If the account pays for minutes beyond them, set `included_minutes` to everything
its budget allows, or leave billing out. Do not use this when the account also runs
Actions on public repositories, whose free minutes may appear in the report.

The billing token is used for this report only, through a client of its own. For a user it
is a fine-grained token with the user permission "Plan" (read). For an organization it is a
fine-grained token of an owner or billing manager with the organization permission
"Administration" (read); do not use a classic token, whose `admin:org` scope is full
organization admin. A failed read (refused, not on the enhanced billing platform, stale, or
an implausible report) gives no reading and is logged once, and rule 9 falls back to the
burn projection. Each read has the request and time budget of `tick_budget` on its own,
after the scan.

### [connectors.github.pulls]

Open pull requests for rule 6 (`bottlenecks`), and those closed within `closed_lookback` for rule 3 (`orphan_status` proposes closing a parent only when every PR of its family is merged, and only when the last two scans read both lists of every repository in full: no error, no page GitHub last confirmed before the scan began, no page limit reached, no PR seen twice across pages, and no PR the scan before saw missing from both lists, which shows a list moved while it was read). Every repository agents push task branches to must be in `[connectors.github] repos`: a PR elsewhere is invisible. The closed list costs one to three requests per repository per scan (a list longer than one page is read twice: up to six more, outside the per-tick budget and `tick_budget`, an unchanged page being a free 304 for an authenticated token), read after mergeable states and label times; when the per-tick budget runs out first, no parent is proposed for closing (warned after 10 such scans). A PR closed without merging blocks a close only while it is within `closed_lookback`, and a merged PR older than that no longer covers its subtask (raise `closed_lookback` for parents that run longer). A list longer than one page is read again, past the cache (an unchanged page is a free 304), to see that it did not move. Keep `[connectors.github] cache_ttl` plus `tick_budget` below `poll_interval` (warned at startup), or the lists come from the cache and are never known in full. A PR belongs to a task when its head branch is
the task's own branch, or when one of the task's codes is a whole token of the PR's title or
head branch: the task id (Hermes `t_…` in `hermes/t_…`) and, with `task_code`, every match
of that pattern in the task's title (e.g. `IOS-036`). A code needs at least 4 characters with
a letter and a digit. Mentions in a PR's description or comments never link it (incident 17:
8 of 10 such matches were wrong on a live farm), and a fork's PR links nothing. Hold labels (`holds`, `hold_labels`) on a PR hold its task, aged
from when the label went on (read from the PR's issue events); a held PR with no open task
is held work too. With rule 6's `ready_prs` (default off), PRs GitHub reports as mergeable
(`mergeable_state` `clean`), not drafts, not forks', not held and unchanged for
`idle_alert_after` count as ready work for idle agents of `pr_role` (default: any). Up to
300 open PRs of each repository (most recently updated first) are read. A repository
GitHub stops answering keeps its last list for three scans, then its PRs are left out with
a warning. The token needs Pull requests: read and Issues: read (label events).

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `enabled` | `true` | bool | |
| `poll_interval` | `"2m"` | duration, >= 30s | List open PRs this often; the last list is served in between. |
| `max_details` | `20` | count, 0 to 100 | PRs whose mergeable state is read per scan, one request each, within `[connectors.github] max_requests_per_tick`. |
| `closed_lookback` | `"7d"` | duration, 1h to 30d | Closed PRs (merged or not) are listed this far back, up to 300 per repository, after the open PRs and mergeable states of a scan. Rule 5 makes no WIP commit for a session that started before it (a PR closed during the session would not be seen). |
| `task_code` | unset | regular expression, at most 200 characters | Task codes in task titles (besides the id) that link a PR whose title or branch carries them, e.g. `'^[A-Z][A-Z0-9]+-[0-9]+'` (anchored, so `UTF-8` later in a title is not a code). No capturing groups, no repeated group that itself repeats, no backreferences or lookarounds: it runs over agent-written titles every scan. |

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

## [connectors.cost_reports]

Reads what each provider's cost report says was spent per day (the bill), so the spend
Kelpie prices from traces can be checked against it. Off unless present; one table per
provider, `[connectors.cost_reports.<provider>]`, where `<provider>` is the provider's name
in [[spend]](#spend) (`anthropic`, `openai`): a provider with a cost report is in `api`
spend mode unless `mode` says otherwise. At most 10.

| Key | Default | Meaning |
| --- | --- | --- |
| `kind` | *required* | `anthropic` (`GET /v1/organizations/cost_report`) or `openai` (`GET /v1/organization/costs`). |
| `admin_key_env` | *required* | Variable holding an admin key with read access to costs: Anthropic's Admin API key (`sk-ant-admin...`), OpenAI's admin key. Never the agents' key. Must be set when `kelpie serve` starts. |
| `api_url` | the provider's API | https URL; plain http only to loopback. No credentials, query or fragment. A path is a prefix (a gateway): requests go to `<path>/v1/...`. |
| `interval` | `"1h"` | Read at most this often (>= 5m); the reports fill in within minutes. |
| `timeout` | `"10s"` | Each request (> 0, <= 60s). |
| `days` | `7` | Complete UTC days read each time (1 to 31); today is never read, its figure is not final. |
| `workspaces` | all | `anthropic`: count only these workspace ids (the default workspace has none). |
| `projects` | all | `openai`: count only these project ids (sent as repeated `project_ids`; whether OpenAI filters by them reliably is not confirmed, so compare the figure with the dashboard once). |
| `warn_gap` | `0.25` | Rule 10 warns when a day's estimate and bill differ by at least this share of the bill... |
| `warn_min_usd` | `5` | ...and by at least this many dollars (either way). |

What is counted: Anthropic, token costs per model (`cost_type = "tokens"`; web search, code
execution and session costs are left out). OpenAI, the day's total over all line items
(images, audio, fine-tuning and storage included): use `projects` to keep it to the farm's
keys. Credits and adjustments (negative items) are counted. Priority Tier usage is not in
Anthropic's cost report. Only `kelpie serve` reads the reports, on a background thread (a
slow provider never holds up a tick; all pages of one read share 60 seconds); figures are
stored in the Kelpie database (90 days) for the daily report. A report that cannot be read
keeps its last figures and is tried again after 5 minutes; `kelpie doctor` reads one day
of each to check the key.

**Reconciliation.** For each UTC day a report covers that ended at least 6 hours ago
(late spans and the providers' own figures settle) and that the span store holds whole
(within `[traces] retention` and not before its oldest span), Kelpie prices that day's
counted LLM calls of the provider and compares the sum with the bill:
`Snapshot.reconciliations`, recomputed when the figures change and at least hourly (after
a failure, in 5 minutes). A call belongs to the day its span ended in. A call counts for
the provider of its model ([[spend]](#spend)) unless another platform wraps the id
(Bedrock, Vertex `@date`, a `vendor/` router such as OpenRouter): that platform bills it,
not this cost report, unless a `[spend.providers.<name>] models` pattern that names the
platform matches the id as written (`*.anthropic.*` does; `claude-*` claims neither
`us.anthropic.claude-...` nor `claude-...@date`). Calls without a known
provider are reported as unknown tokens. Until a newer day of a provider settles (with
`days = 1`, before 06:00 UTC), its last reconciled day stands; after a restart, the
stored figures stand in until the report is read again.

What the bill covers differs: Anthropic's figure is its token charges only, OpenAI's is
the day's total (web search, tools and other items too), so an OpenAI estimate is
expected to sit below its bill by those items; raise its `warn_gap` if they are routine.
The daily report's "Bill check" shows the same comparison for one settled day.

`/metrics` has `kelpie_cost_report_up` per provider once it was read, and, for the newest
reconciled day of each provider, `kelpie_spend_reported_usd`, `kelpie_spend_estimated_usd`,
`kelpie_spend_gap_ratio` ((estimate - bill) / bill, absent when the bill is 0 or a credit)
and `kelpie_spend_reconciled_day_timestamp_seconds`. A gap past the thresholds (never a day
billed as a credit) is a rule 10 warning naming the likely causes: traces below the bill
point at agents that do not export spans, other users of the same key, models without a
price or the bill's non-token items; above, at prices above the bill's (batch discounts,
credits, a `[prices]` entry) or another platform's calls counted under the provider's
`models`. Kelpie needs traces enabled for this; without them figures are only stored.

## [alerts]

Real-time alerts. None are sent without a channel. Rule alerts are not sent while
`dry_run` is true or for a rule in `observe`; `kelpie report --send` sends its summary
whatever the modes. An alert goes to every channel whose `min_severity` it reaches. The
summary is laid out like an alert: the report's title, its headline in bold under the
severity's icon (`[report] severity`), one line each for the biggest wait, the open conditions
and the top recommendations, and an "Open report" link when `[report] url` gives the
report's address. `language` translates only "Open report" and "and N more"; the
report's own text stays English.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `dedup_window` | `"10m"` | duration, >= 0 | Identical alerts within this window are sent once per channel. |
| `queue_size` | `50` | count, 1 to 1000 | Per channel; alerts that do not fit are dropped and counted. |
| `language` | `"en"` | `en` \| `ru` | Language of Kelpie's own words in an alert (headlines, labels, units, "Kelpie did"). Text that rules and agents wrote is shown as it is. Any other value stops startup. |
| `task_url` | `[report] task_url` | http(s) URL with `{id}` | The "Open task" link of each task in an alert; the id is URL-quoted into `{id}`, which must not be in the host. At most 600 characters once a 64-character id fills `{id}`. Unset: `[report] task_url`, or no links when that is unset too. |
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
| `format` | `"html"` | `html` (bold headline and titles, links) or `plain`. An HTML message Telegram cannot parse is sent once more as plain text. |
| `buttons` | `false` | Snooze buttons on the alerts a snooze would quiet, and a poller that takes the presses ([Snoozing from the alert](#snoozing-from-the-alert)). On at most one Telegram channel; needs `[storage] path`. |
| `allowed_user_ids` | `[]` | Telegram user ids (numbers, at most 100) that may press the buttons. With `buttons = true` an empty list stops startup and fails `kelpie doctor`. |

Slack channel (`type = "slack"`):

| Key | Default | Meaning |
| --- | --- | --- |
| `webhook_env` | *required* | Variable holding an incoming-webhook URL (`https://hooks.slack.com/services/...`). Checked at startup. |
| `min_severity` | `"warning"` | `info`, `warning` or `critical`. |
| `timeout` | `"10s"` | > 0 and <= 60s. |
| `name` | `"slack"` | Lower-case word; needed with two Slack channels. |
| `allow_host` | unset | Another host for the webhook (a relay); plain http only if it is loopback. |

An alert reads the same in every channel: a severity icon and a one-line headline (with
the transition: still open after, changed, resolved after), then one entry per task with
its title, age and id, a short "Needed" line of at most 140 characters, how many tasks it
blocks and the "Open task" link, then the reason ("Why", up to 1000 characters, unless every
task says what is needed), "Kelpie did" only when Kelpie acted, would act or failed, and
the rule. Telegram gets HTML with every task and agent value escaped. Slack gets Block
Kit `rich_text` blocks: each such value is a text element, which Slack shows as it is (no
formatting, mentions or links can come from it); Kelpie's own emphasis is an element style
(bold headline and titles, code for ids and the rule), and "Open task" is a link element.
The plain `text` fallback is sent with formatting off and `&`, `<` and `>` escaped. Nothing
is inserted into the text, so ids, commands and URLs copy as they are. In every channel `@`
in task and agent text is shown as a fullwidth `＠`, so `@here` or `<!channel>` ping nobody,
and invisible format characters are removed.

### Snoozing from the alert

A person can snooze a wait from the alert that reminds them of it, without a terminal
([#176](https://github.com/ch8er/kelpie/issues/176)). It appears only on the items a snooze
would quiet: each item of rule 13's digest (not one whose block can never clear), rule 4's
no-condition and overdue alerts (not a dead end, a condition Kelpie cannot see or stale
evidence), and rule 2's overdue alert on a wait for one person's human task (the button
snoozes the human task, and says so: "1. Snooze H-7 1d"). Never on agent-health alerts, and
never on a "resolved" message.

- **Slack** gets no buttons. Under each such item it shows the command that does it, as
  plain text on its own line so it copies cleanly: `kelpie snooze T-101 --until
  2026-10-22`, a date 3 days after the alert's day in `[report] timezone` (through that day;
  edit it before you run it). A task id that is not a plain token (letters, digits and
  `._:/#+=,%~-`), or that starts with `-` (the command would read it as an option), gets no
  line: it would have to be pasted into a shell. Ids with `#` or `~` are shell-quoted.
- **Telegram**, with `buttons = true`, puts three buttons under each such item: "1. Snooze
  1d", "3d", "7d". A press snoozes that task from now for that many days (stored like
  `kelpie snooze`, "from chat"), answers with a short toast and edits the alert: a line
  "✅ 1. Snoozed T-101 until 2026-10-22 14:05 UTC by Alice" is added at the end and that
  item's buttons go (when the line would not fit Telegram's 4096 characters, the buttons go
  and the line is sent as a reply to the alert). The next digest lists the task in its
  footer. A button that has expired answers so, with the `kelpie snooze` command that still
  works, and that message's buttons are taken off. A press never shortens
  a snooze set with `kelpie snooze` that ends later: it is refused (`already_snoozed`) and
  the toast says until when the task is already snoozed.

Like `kelpie snooze`, a press stores its snooze even under the global dry-run (`dry_run =
true`, the default): a snooze changes nothing on the farm, it only quiets alerts. Its ledger
entry then says so (`dry_run: true` in its parameters, and "Stored under the global
dry-run" in its reason).

```toml
[[alerts.channels]]
type = "telegram"
token_env = "KELPIE_TELEGRAM_TOKEN"
chat_id = "-1001234567890"
buttons = true
allowed_user_ids = [123456789, 987654321]   # who may snooze; ask @userinfobot for yours
```

Use a bot dedicated to Kelpie. Kelpie reads presses with `getUpdates` long polling only,
never a webhook, so it needs no inbound port; Telegram refuses `getUpdates` while the bot
has a webhook, and another program reading the same bot's updates would take Kelpie's
presses. `kelpie doctor` checks both settings and the bot ("snooze buttons telegram": FAIL
with an empty `allowed_user_ids`, without a database, or when the bot has a webhook set).

**Security model.** The buttons are an inbound control channel, so they are built to do one
thing and refuse everything else.

- *What a press can do*: snooze one task for 1, 3 or 7 days (at most `[snooze] max_days`).
  Nothing else: no button can unblock, close, stop, requeue or reassign a task, or change a
  rule's mode. A snooze only holds repeat human-wait reminders (see [[snooze]](#snooze)),
  and it ends early when more work is blocked behind the task, as any snooze does.
- *Who*: a press counts only when its sender's Telegram user id is in `allowed_user_ids`
  **and** the message is in the configured `chat_id`. A user id is a number Telegram
  assigns; a name or `@username` is never trusted. Everything else is ignored and counted
  in `kelpie_chat_rejected_total{reason}`: `unknown_user`, `wrong_chat`, `bad_ref`,
  `expired_ref`, `used_ref`, `already_snoozed`, `rate_limited`, `malformed`,
  `not_callback`. A press from someone not in `allowed_user_ids` or from another chat gets
  no answer at all, and neither does a malformed one or a user's 11th press within a
  minute. A refused press of an allowed person in the chat is answered "Not allowed." and
  nothing more (for `already_snoozed`, until when the task is snoozed; for `expired_ref`,
  that the button has expired).
- *What a button carries*: no task text, only a signed reference,
  `s<days>.<record id>.<MAC>` (at most 41 bytes; Telegram allows 64). The record, kept by
  Kelpie, names the alert, the task, the item, when it was issued and the message it went
  out in; the MAC is the first 16 bytes of an HMAC-SHA256, under a key Kelpie generates on
  first use, over the chat id, the days and the record id, compared in constant time.
  Record ids are never reused. A forged, truncated or edited reference, or one from another
  chat or another message, is `bad_ref`.
- *Expiry*: a reference is good for 24 hours (the longest escalation step), and only while
  it is on the latest message of its alert in that chat. Every message of the alert counts,
  with buttons or without: the next escalation step, a changed digest, a digest with
  nothing left to snooze or the alert resolving makes the older message's buttons expire
  (`expired_ref`), and Kelpie removes them from that message (best effort). An alert that
  resolves expires its buttons also when the channel does not show the "resolved" message
  (it is `info`, below the default `min_severity`), when that message cannot be sent, or
  when the alert queue is full; then only the removal from the chat may be skipped (also
  while Telegram asks Kelpie to slow down). Each reference is used once (`used_ref`): it is
  claimed before the snooze is written, and given back only when the write fails.
- *Replay*: Kelpie stores the `getUpdates` offset and moves it past an update before acting
  on it, so a press is taken at most once, also across restarts. The offset also moves past
  updates Kelpie drops as malformed, so none comes back.
- *Where the key lives*: in the button store, `kelpie.buttons.db` beside `[storage] path`
  (for `/var/lib/kelpie/kelpie.db`: `/var/lib/kelpie/kelpie.buttons.db`), created with mode
  `0600` (tightened if it was readable by others), owned by Kelpie's user, never opened
  through a symlink (Kelpie refuses to start when another user owns the file, or when the
  file SQLite opened is not the one it checked, and warns when the directory is writable by
  others), apart from `kelpie.db`, which other commands read and people share to debug. It
  also holds the update offset, the references (pruned 7 days after issue), each alert's
  latest message id and Kelpie's copy of the messages with buttons (their text as Telegram
  answered it, which the acknowledgements edit). The key is never logged and never in config. Delete the file to rotate the key: every
  button already sent then stops working.
- *Every press is recorded*: the snooze (`kelpie snoozes`, "from chat", with the person's
  name) and a ledger entry (kind `snooze`, rule `chat`, outcome `executed`, executor the
  channel; who, with their user id, what and until when; `kelpie why <task>` lists it),
  written in one transaction that takes the write lock before it reads, so a `kelpie
  snooze` from another process cannot slip in between: there is no chat snooze without its
  entry.
- *Untrusted text*: the person's name is shown cleaned, without invisible characters and
  with `@` broken, set as code so that Telegram turns nothing in it into a link, and the
  acknowledgement is plain text with the message's own formatting entities, never parsed
  as HTML. It is built from Kelpie's copy of the message, never from the copy a press
  carries, so presses close together keep every acknowledgement. A task id with a control
  or invisible character gets no button. Every Bot API call goes through Kelpie's HTTP helper to the configured `api_url`
  only (no redirects, bounded answers), with the token never logged.
- *Never in the control loop*: the poller runs in its own thread, waits at least a second
  between two polls, backs off exponentially on errors (up to 5 minutes, longer when
  Telegram says so; `kelpie_chat_api_errors_total{call,cause}`), and stops with Kelpie
  under the alert delivery's shutdown deadline. Presses are counted in
  `kelpie_chat_actions_total{outcome}` (`snoozed`, `rejected`, `failed`).

## [report]

The daily "cost of chaos" report (rule 8), built by `kelpie report`, or by `kelpie serve`
once a day when `at` is set.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `timezone` | `"UTC"` | IANA name | Where a day starts and ends. |
| `dir` | unset | path | Writes `kelpie-report-<date>.md` there. Unset: stdout only. |
| `task_url` | unset | http(s) URL with `{id}` | Links task ids in the Markdown; the id is URL-quoted into `{id}`, which must not be in the host. No credentials, and at most 600 characters once a 64-character id fills `{id}`. Anything else stops startup. |
| `url` | unset | http(s) URL with `{date}` | Where the full report of a day can be read (e.g. where `dir` is served); the chat summary links it as "Open report", with the day (YYYY-MM-DD) in `{date}`, which must not be in the host. Unset: no link. |
| `hour_usd` | unset | dollars, > 0 | What an hour lost costs: a stuck task-hour, or an agent-hour idle behind a hold or waiting on a person. Unset: hours stay hours. |
| `channels` | all | list of channel names | `[[alerts.channels]]` that get the summary with `--send`. |
| `severity` | `"warning"` | `info` \| `warning` \| `critical` | Severity of the summary; channels filter by `min_severity`. |
| `verify_window` | `"1d"` | duration string, > 0 | Before/after of fixes: this long on each side. |
| `review_roles` | `["reviewer"]` | list of roles | Whose sessions make "review time". |
| `min_sample` | `10` | count, >= 1 | Fewer observations on a side: the row says *direction only*. |
| `sections` | unset | `[report.sections.<name>]` tables | Settings of a report section. A key the section does not accept is noted in the report and the section does not run until the table is fixed; a table for a section that does not exist is noted too; a bad value fails that section only (also noted). |
| `llm` | unset | `[report.llm]` table | The optional LLM root-cause section; see [[report.llm]](#reportllm). |
| `at` | unset | `"HH:MM"`, local time in `timezone` | `kelpie serve` builds yesterday's report once a day after this time, writes it to `dir` and sends the summary to `channels`. A serve that was down at that time catches up the same day; a failed run is retried every 10 minutes, at most 3 times a day. Unset: only `kelpie report` (by hand or from cron). |

The report reads the board through the adapters. For the day and its before/after window
it widens `[adapters.hermes] lookback` on its own (for that run only; the rules keep the
configured value), so a short `lookback` does not cut the report's data, as long as the
Hermes spool and board still hold it.

### [report.llm]

An optional section, "Root causes (generated by an LLM)", in the Markdown report (with the
other sections, before the before/after of fixes). Off unless this table exists; the
report is complete without it, and the summary sent to the channels never includes it. It
is the only place Kelpie calls an LLM, never in the control loop: `kelpie report` and the
`[report] at` schedule call it after the report is built.

The model gets aggregates only: rule names, causes, actions, severities, metric names and
units (Kelpie's own words), hours, dollars, counts; tasks as `task-1`, `task-2`... and
people as `person-1`... Never task titles, bodies or ids, block reasons, alert texts,
commands, tool output, fix notes, roles or names. The real task ids are put back into the
answer locally. The section says what the model saw, also when the call failed (see
[Privacy](privacy.md#llm-root-cause-section)). The answer is quoted and marked as
generated, with HTML, links and images neutralised; a failure is a note in the section.

| Key | Default | Unit | Meaning |
| --- | --- | --- | --- |
| `provider` | *required* | `anthropic` \| `openai` | `anthropic`: the Messages API. `openai`: Chat Completions, also any compatible server (`base_url`). |
| `model` | *required* | model id | The provider's model id. |
| `api_key_env` | see below | environment variable name | Where the key is read from at report time. Never in the config file. |
| `base_url` | the provider's API | https URL (http only on localhost) | Another endpoint, e.g. a compatible server on the same host so nothing leaves it. Without `/v1`: Kelpie adds `/v1/messages` or `/v1/chat/completions`. |
| `timeout` | `"60s"` | duration, up to 10m | One deadline for the whole call. |
| `max_tokens` | `1000` | 1 to 32000 | Upper bound on the answer: `max_tokens` for Anthropic and compatible servers, `max_completion_tokens` for OpenAI's own API. With a reasoning model it includes the reasoning; raise it if the section says "empty answer". |

The key goes only where it belongs: with the provider's own API, `api_key_env` defaults to
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. With another `base_url` it must be named
explicitly, so the provider's key is never sent to a third party by accident; on
localhost, without `api_key_env`, no key is sent at all.

Each run makes one call. A scheduled run whose write or send fails is retried (up to 3
times a day), and each retry calls the model again.

```toml
[report.llm]
provider = "anthropic"
model = "claude-sonnet-5-5"
```

### Report sections

`[report.sections.tokens]` (token optimization, `kelpie.traces.tokens`) and
`[report.sections.tokens_fit]` (context and model fit, `kelpie.traces.fit`) accept the
fields of `TokenSettings` and `FitSettings`. Durations are strings (`"5m"`), counts whole
numbers >= 0, ratios, shares and tolerances numbers between 0 and 1, lists lists of names;
any other value fails that section (noted in the report).

| Key | Default | Meaning |
|---|---|---|
| `tokens.cache_ttl` | `"5m"` | A call after a longer pause re-reads the prompt uncached ("expired"). |
| `tokens.runaway_tokens` / `runaway_factor` / `min_steps` | `100000` / `4` / `5` | A session that reaches this context, this many times its first, over this many steps, without compacting, runs away. |
| `tokens.compact_at` | `50000` | The context where compacting or a handoff would cut a runaway session. |
| `tokens.min_saving_tokens` | `50000` | A recommendation needs at least this many tokens a day. |
| `tokens_fit.routine_roles` | `[]` | Roles whose switch to a cheaper model is recommended (priced). Empty: model fit is reported, never recommended. |
| `tokens_fit.allowed_models` | `[]` | Candidate models besides those the farm already runs (seen in traces). |
| `tokens_fit.routine_min_calls` / `routine_output_tokens` / `routine_max_tool_tokens` | `20` / `400` / `1000` | Routine work: at least this many calls a day, at most this median output and mean tool output per call. |
| `tokens_fit.min_after` | `"12h"` | After a model switch, how long before its quality is judged. |
| `tokens_fit.min_saving_tokens` | `50000` | A recommendation needs at least this many tokens a day. |

The other fields (`break_ratio`, `fragment_min_sessions`, `review_reads`, ...) are
documented in those modules.

## [agent]

For `kelpie agent` on a machine with agents ([deploy.md](deploy.md),
[multi-host.md](multi-host.md)). The agent also reads the `[adapters.*]` sections and
`[storage] path` (where the Hermes park key lives); it ignores rules, connectors, alerts
and the report.

| Key | Default | Meaning |
| --- | --- | --- |
| `name` | required | This machine on the farm: 1–63 of `a-z`, `0-9`, `-`, not starting or ending with `-`. Its task ids become `<name>:<id>`. Must match an `[[hub.agents]]` entry. |
| `server` | required | `https://host[:port]` of the hub (port 9465 by default), no path. `http://` only with `allow_plain_http`. |
| `token_env` | `KELPIE_AGENT_TOKEN` | Variable holding this agent's token: at least 32 printable characters, random (`openssl rand -hex 32`). |
| `ca_file` | system trust store | Certificate (or CA) the hub's certificate must chain to. The host name is always checked. |
| `allow_plain_http` | `false` | Allow `http://`; the server name must resolve to loopback or private addresses only (RFC 1918, `100.64.0.0/10`, IPv6 ULA). The token then crosses the network in clear. |
| `interval` | `15s` | Between reports (1s–5m). The server merges a report for 2 × interval + 10s. |
| `timeout` | `30s` | One request to the hub (1s–5m). |
| `actions` | every kind the adapters declare | Narrows the action kinds this agent carries out, e.g. `["comment"]`. |
| `max_body_bytes` | 16 MiB | Largest report sent; must not exceed the hub's. |
| `relay` | `"127.0.0.1:4318"` | Loopback address of the OTLP relay (spans from this machine go through it to the hub); `false`: none. |

## [hub]

For `kelpie serve` taking reports from agents. With `[hub]`, in-process `[adapters.*]` are
refused (run `kelpie agent` on the server's machine too), the engine never guesses which
board an action belongs to, and `/v1/traces` on the main port is off (spans come through
the agents' relays).

| Key | Default | Meaning |
| --- | --- | --- |
| `listen` | `"0.0.0.0:9465"` | Address and port of the hub listener. |
| `tls_cert`, `tls_key` | — | Certificate and key (PEM) for HTTPS. Required unless `allow_plain_http`; the two exclude each other. |
| `allow_plain_http` | `false` | Plain HTTP; `listen` must then be a loopback or private address, never `0.0.0.0`. |
| `silent_after` | `90s` | No fresh report for this long: one alert "agent X silent"; "resolved" when it is back (10s–1h). |
| `action_timeout` | `30s` | How long an action waits for the agent's result, and each agent's total per tick (1s–2m). |
| `max_clock_skew` | `2m` | A report whose clock is further off is not merged, and a human is told (5s–1h). |
| `max_body_bytes` | 16 MiB | Largest request body, also after gzip. |
| `max_connections` | `64` | Connections not yet authenticated (8–10000; also at most 16 per address, per /64 for IPv6); each has 3s to present its token, and when all are taken the oldest is cut. |

`[[hub.agents]]`: one entry per machine, `name` (as in its `[agent] name`) and `token_env`
(the variable holding its token on the server). Names and variables must be distinct, and
so must the tokens.

`/metrics` gains `kelpie_agent_up{agent}` and
`kelpie_agent_last_report_timestamp_seconds{agent}`; adapter errors on an agent are counted
as `kelpie_adapter_errors_total{adapter="<agent>/<adapter>"}`.

## Checks across sections

Kelpie refuses to start when:

- `[[budgets]]` exist but `[traces] enabled = false`; a budget `window` exceeds
  `[traces] retention`; a `usd` budget exists without `[prices]` while
  `[spend] default_prices = false`.
- `[traces] retention` is shorter than `window` or `totals_window`, or, with
  `[traces.outputs]`, shorter than `repeat_days` or twice its `window`.
- `[[traces.forward]]` exists with `[traces] enabled = false`.
- `[[block_conditions]]` exist without `[connectors.github]`, or name a `repo` not in its
  `repos`.
- `ci_economy` `window` is longer than `[connectors.github.ci] window`.
- A Hermes adapter is enabled, `dry_run = false`, and a rule other than `loop_breaker`
  (and `ci_economy` with `allow_cancel`) is in `act`.
- A variable named by an explicit `token_env`, `webhook_env`, `headers_env` or
  `cancel_token_env` is missing or malformed.
- `billing_user` or `billing_org` is set without `billing_token_env` or `included_minutes`,
  or both are set; `billing_token_env` names `token_env`'s or `cancel_token_env`'s variable,
  or its variable is missing or malformed; `billing_skus` is empty.
- The [exposure check](install.md#otlp-token-and-the-exposure-check) fails.

Kelpie logs a warning when:

- a configured rule is planned but not implemented;
- `no_progress` `window` is longer than `[traces] window`;
- `conflict_warning` with `serialize`, or `dispatch_gate`, is in `act` without
  `wait_condition` in `act`;
- `[resources.prometheus] interval` or a provider's `interval` is longer than rule 7's
  `max_reading_age`;
- twice `[connectors.github.ci] billing_interval` (how long a reading is kept) is longer
  than rule 9's `max_reading_age`;
- the billing token holds the same value as the main or the cancel token;
- a budget's `providers` names a provider that is not in `[connectors.llm_quotas]`;
- `loop_breaker` and `wait_condition` set different `poll_patterns`;
- no OTLP token is set, or credentials would go over plain http to a non-loopback host.
