# Changelog

All notable changes to Kelpie (distributed as `kelpie-sre`) are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[PEP 440](https://peps.python.org/pep-0440/), so a pre-release is written `0.1.0a1`, not
`0.1.0-alpha.1`. The release process is in [docs/release.md](docs/release.md).

## [0.1.0a1] - Unreleased

First public pre-release, an alpha: interfaces and configuration keys may still
change. It starts in global dry-run (`dry_run = true`) with every rule in `observe` mode,
so a fresh install changes nothing on your farm until you turn it on, rule by rule.

### Added

#### Rules

Rules are deterministic functions over a snapshot of the farm. There is no LLM in the
control loop. Each rule runs in `observe`, `suggest` or `act` mode.

- `checkpoints`: reminds a live session to commit, makes a WIP commit when a session ends
  with uncommitted work, and leaves a resume note when the task goes back to the queue
  ([#14](https://github.com/ch8er/kelpie/pull/14), [#15](https://github.com/ch8er/kelpie/pull/15)).
- `exit_contract` (rule 1): requeues a task whose session ended without a final status or
  with a block that has no allowed reason; optionally requires evidence before "done" and
  reports the reopened-after-done rate ([#8](https://github.com/ch8er/kelpie/pull/8), [#112](https://github.com/ch8er/kelpie/pull/112)).
- `wait_condition` (rule 2): lets an agent register a condition (CI, merge, quota reset,
  a person) and exit, wakes the task when the condition holds, and alerts when a wait can
  never be met ([#9](https://github.com/ch8er/kelpie/pull/9)).
- `orphan_status` (rule 3): alerts on tasks nobody owns or will move (ownerless statuses,
  parents left open, cards stuck before work) and proposes an idle agent; a parent is closed
  only when every pull request of its family is merged ([#10](https://github.com/ch8er/kelpie/pull/10), [#83](https://github.com/ch8er/kelpie/pull/83), [#109](https://github.com/ch8er/kelpie/pull/109), [#115](https://github.com/ch8er/kelpie/pull/115)).
- `stale_blocks` (rule 4): clears a block when the outside condition it waited on is met,
  on fresh evidence only, and alerts on blocks it cannot clear ([#11](https://github.com/ch8er/kelpie/pull/11)).
- `ping_pong` (rule 5): escalates a task that bounces between developer and reviewer for
  more than a set number of rounds ([#27](https://github.com/ch8er/kelpie/pull/27)).
- `bottlenecks` (rule 6): finds idle agents behind a sequential chain, overloaded roles
  next to idle ones, idle agents with ready work, forgotten holds, and human tasks on the
  critical path ([#30](https://github.com/ch8er/kelpie/pull/30), [#48](https://github.com/ch8er/kelpie/pull/48), [#90](https://github.com/ch8er/kelpie/pull/90)).
- `resources` (rule 7): warns before disk, memory, CPU, load or processes run out, limits
  heavy tasks per host under pressure, and watches LLM provider quotas and key health
  ([#32](https://github.com/ch8er/kelpie/pull/32), [#35](https://github.com/ch8er/kelpie/pull/35)).
- `ci_economy` (rule 9): classifies wasted CI builds (superseded, already verified,
  docs-only, draft, WIP), cancels superseded runs, forecasts the CI minutes quota, and
  reports a CI outage (many change requests failing at the same job and step) as one
  incident with its scope and the tasks it explains, instead of one alert per blocked task
  ([#41](https://github.com/ch8er/kelpie/pull/41), [#107](https://github.com/ch8er/kelpie/pull/107), [#178](https://github.com/ch8er/kelpie/pull/178)).
- `loop_breaker` (rule 10): stops a run that repeats identical tool calls, hits an error
  streak or exceeds a hard budget ([#33](https://github.com/ch8er/kelpie/pull/33)).
- `no_progress` (rule 11): alerts when a session spends many tokens with no status change,
  commit or artifact ([#26](https://github.com/ch8er/kelpie/pull/26)).
- `conflict_warning` (rule 12): warns while two live tasks edit the same files, before they
  collide at merge; can optionally hold the later task ([#51](https://github.com/ch8er/kelpie/pull/51)).
- `dispatch_gate`: parks a ready task in `waiting` while its prerequisite (a parent task or
  an unmet PR/CI condition) is not met ([#53](https://github.com/ch8er/kelpie/pull/53)).
- `needs_you` (rule 13): one "needs you" inbox per person, ordered by cost of delay,
  covering human tasks with agent work behind them and approvals agents wait for; a block
  that waits on a condition is not treated as a question ([#76](https://github.com/ch8er/kelpie/pull/76), [#93](https://github.com/ch8er/kelpie/pull/93), [#130](https://github.com/ch8er/kelpie/pull/130)).
- `return_causes`: groups why tasks come back (for example merge conflicts) and turns a
  dominant cause into a recommendation, naming the files involved ([#110](https://github.com/ch8er/kelpie/pull/110)).
- `output_offenders`: asks the owner of a role's instructions to add an output filter when
  the same command repeatedly floods the context with huge tool output ([#102](https://github.com/ch8er/kelpie/pull/102)).

#### Engine

- Per-rule mode (`observe` | `suggest` | `act`) and a global dry-run override ([#4](https://github.com/ch8er/kelpie/pull/4)).
- Actions are routed to the executor that owns the task, whether a platform adapter or a
  connector, and a session action such as `stop` goes to the adapter that owns the session
  ([#3](https://github.com/ch8er/kelpie/pull/3), [#104](https://github.com/ch8er/kelpie/pull/104)).
- An action ledger in SQLite with deduplication and rate limiting ([#5](https://github.com/ch8er/kelpie/pull/5)).
- Transition-based alerts for long-lived states: announced when they open, change or
  resolve, not on every tick ([#12](https://github.com/ch8er/kelpie/pull/12)).
- While a CI outage is open (rule 9), the alerts it explains (blocks and waits that last
  too long or name no condition, on its PRs) are folded under its one alert and recorded
  for the report, not sent one per task ([#183](https://github.com/ch8er/kelpie/pull/183)).
- An action that a platform cannot perform becomes an alert for a human instead of an error
  ([#66](https://github.com/ch8er/kelpie/pull/66)).

#### Platform adapters

- Hermes: reads the kanban board, run summaries, workspace state and a hooks spool through
  a small plugin; reads task descriptions and the files a task changes in its worktree; maps
  profiles to roles ([#13](https://github.com/ch8er/kelpie/pull/13), [#15](https://github.com/ch8er/kelpie/pull/15), [#58](https://github.com/ch8er/kelpie/pull/58), [#60](https://github.com/ch8er/kelpie/pull/60), [#63](https://github.com/ch8er/kelpie/pull/63)).
- Hermes actions: stop a run through the Hermes CLI, and park a task in `scheduled`, wake it
  or comment on it, so `wait_condition`, `dispatch_gate` and `conflict_warning` can act
  ([#36](https://github.com/ch8er/kelpie/pull/36), [#75](https://github.com/ch8er/kelpie/pull/75)).
- Hermes keeps the platform status, and an owner counts as a person or an agent
  consistently across adapters, so a person is never counted as an idle agent
  ([#106](https://github.com/ch8er/kelpie/pull/106), [#114](https://github.com/ch8er/kelpie/pull/114), [#118](https://github.com/ch8er/kelpie/pull/118)).
- Claude Code: reads shared task lists and a command-hook spool, stops a session through
  the hook, and detects idle interactive sessions that are still alive ([#79](https://github.com/ch8er/kelpie/pull/79), [#88](https://github.com/ch8er/kelpie/pull/88),
  [#113](https://github.com/ch8er/kelpie/pull/113)).

#### Connectors

- GitHub: read-only pull request and CI state for `pr_merged` and `ci_status` conditions,
  open pull requests (labels as holds, mergeable PRs as ready work), recent builds and CI
  minutes from GitHub's billing usage report. A PR belongs to a task only through the task
  code in its title or branch ([#31](https://github.com/ch8er/kelpie/pull/31), [#90](https://github.com/ch8er/kelpie/pull/90), [#103](https://github.com/ch8er/kelpie/pull/103), [#107](https://github.com/ch8er/kelpie/pull/107)).
- LLM provider quotas and key health, with providers defined in config rather than code
  ([#35](https://github.com/ch8er/kelpie/pull/35)).
- Human tasks: a task can wait on a person's task on the same board, and a block reason can
  be linked to such a task by pattern ([#38](https://github.com/ch8er/kelpie/pull/38)).
- Optional host resource readings from Prometheus ([#32](https://github.com/ch8er/kelpie/pull/32)).

#### Traces

- OTLP/HTTP ingest (protobuf and JSON) on the existing HTTP port, following the
  OpenTelemetry GenAI semantic conventions, with a span store ([#16](https://github.com/ch8er/kelpie/pull/16)).
- Spans built from adapter hooks when a platform emits none, merged with OTLP spans so
  nothing is counted twice ([#17](https://github.com/ch8er/kelpie/pull/17), [#20](https://github.com/ch8er/kelpie/pull/20)).
- Optional forwarding of accepted traces to Langfuse, Phoenix or Datadog ([#18](https://github.com/ch8er/kelpie/pull/18)).
- Message content, tool arguments and tool results are not stored. Commands are reduced by
  an allowlist redaction shared by OTLP ingest and both hooks, and `kelpie spool scrub`
  re-redacts older spool files ([#101](https://github.com/ch8er/kelpie/pull/101)).

#### Budgets and spend

- `[[budgets]]` per day, task, agent or project, in USD or tokens, for API or subscription
  use, with soft and hard limits and a forecast of when a limit will be reached; `[prices]`
  per model. Nested LLM spans are counted once ([#29](https://github.com/ch8er/kelpie/pull/29)).
- A dated default price table for popular Anthropic, OpenAI and Google models under
  `[prices]` (`[spend] default_prices`), so `usd` budgets work without a price list; model
  ids are matched across platforms (Bedrock, Vertex, OpenRouter, dated snapshots), and
  `kelpie status` and `kelpie doctor` name the models that have no price ([#184](https://github.com/ch8er/kelpie/pull/184)).
- Prompt-cache writes are counted from the spans and priced at `cache_write` (Anthropic:
  1.25 × input) instead of the input price. Cache reads and writes are capped at a span's
  input on arrival, so a span with cache reads and no input carries no usage. Upgrading
  rebuilds the span store indexes (some seconds per million spans) and fills cache writes
  of stored OTLP spans; stored OpenClaw spans keep pricing writes at the input price until
  they leave the retention ([#196](https://github.com/ch8er/kelpie/pull/196)).
- Spend mode per provider (`[spend.providers]`): dollars for API keys, tokens and the share
  of a configured limit for subscriptions, decided from the config; `kelpie status` and
  `kelpie doctor` show each provider's mode and why ([#202](https://github.com/ch8er/kelpie/pull/202)).
- Providers' cost reports read with an admin key (`[connectors.cost_reports]`: Anthropic
  token costs per model, OpenAI daily totals), stored for the report ([#204](https://github.com/ch8er/kelpie/pull/204)), and
  reconciled with the spend priced from traces per settled day: on `/metrics` and as a
  rule 10 warning when they disagree ([#205](https://github.com/ch8er/kelpie/pull/205)).

#### Token optimization

- Huge tool outputs are found, ranked per role and command, and matched with a filter
  recommendation and an estimated saving, with before/after checks ([#49](https://github.com/ch8er/kelpie/pull/49)).
- Prompt-cache misses, context growth and restarts from zero, with estimated savings ([#65](https://github.com/ch8er/kelpie/pull/65)).
- Repeated fragments, unused tools, model fit per role and diff-only review ([#71](https://github.com/ch8er/kelpie/pull/71)).

#### Daily report

- Cost of chaos: hours and dollars lost to requeues, ownerless tasks, forgotten holds,
  agents waiting on people and cards stuck before work, with the top three things to fix
  ([#52](https://github.com/ch8er/kelpie/pull/52), [#59](https://github.com/ch8er/kelpie/pull/59), [#121](https://github.com/ch8er/kelpie/pull/121)).
- The biggest wait of the day by cause, with a suggestion ([#123](https://github.com/ch8er/kelpie/pull/123)).
- Waits inside an open CI outage (rule 9) count under "a shared CI failure", not CI or merges, and
  the report lists each outage of the day with its PRs and the alerts folded under it
  ([#182](https://github.com/ch8er/kelpie/pull/182), [#201](https://github.com/ch8er/kelpie/pull/201)).
- Suggestions check what the farm already has (approvers, holds, block conditions, budgets,
  modes); merge automation is never advised ([#173](https://github.com/ch8er/kelpie/pull/173)).
- Listed, never counted: cards stuck before work that block nothing, and agent tasks planned
  for later behind a person's open inbox ([#156](https://github.com/ch8er/kelpie/pull/156), [#188](https://github.com/ch8er/kelpie/pull/188)).
- Spend follows each provider's mode: dollars with an API key, tokens and the share of the
  plan's limit on a subscription; models without a price are named, and each provider's
  bill is checked against the estimate from traces ([#195](https://github.com/ch8er/kelpie/pull/195), [#209](https://github.com/ch8er/kelpie/pull/209)).
- Before/after verification of fixes recorded with `kelpie fix`, and `kelpie fix --list`
  ([#54](https://github.com/ch8er/kelpie/pull/54), [#57](https://github.com/ch8er/kelpie/pull/57)).
- Farm throughput per role, live gauges and Grafana panels; rework counted for the role that
  redid the work, not the reviewer ([#78](https://github.com/ch8er/kelpie/pull/78), [#116](https://github.com/ch8er/kelpie/pull/116)).
- A sections extension point with per-section settings, and token sections that read them
  ([#56](https://github.com/ch8er/kelpie/pull/56), [#84](https://github.com/ch8er/kelpie/pull/84), [#91](https://github.com/ch8er/kelpie/pull/91)).
- Runs inside `kelpie serve` at a configured time (`[report] at`) and reads the board as far
  back as its day needs ([#61](https://github.com/ch8er/kelpie/pull/61), [#67](https://github.com/ch8er/kelpie/pull/67)).
- An optional LLM root-cause section (`[report.llm]`, off by default). It sees only
  aggregates with anonymized ids, and its answer is escaped and marked as a hypothesis
  ([#95](https://github.com/ch8er/kelpie/pull/95)).

#### Alerts

- A shared delivery layer with per-channel queues, retries and flood control, with Telegram
  and Slack incoming-webhook channels ([#47](https://github.com/ch8er/kelpie/pull/47), [#50](https://github.com/ch8er/kelpie/pull/50), [#64](https://github.com/ch8er/kelpie/pull/64)).
- Alert text says what Kelpie did, taken from the outcome and the rule's mode ([#97](https://github.com/ch8er/kelpie/pull/97)).
- Readable alerts: a severity icon and a one-line headline, then each task with its title,
  age, id, a short "Needed" line, what it blocks and an "Open task" link (`[alerts]
  task_url`); "Kelpie did" only when Kelpie acted or would act. Telegram gets HTML (with a
  plain-text fallback and `format = "plain"`), Slack gets Block Kit rich_text blocks, and Kelpie's
  own words come in English or Russian (`[alerts] language`) ([#169](https://github.com/ch8er/kelpie/issues/169)).
- The daily report's chat summary uses the same layout: its title, the headline in bold under
  the severity's icon, the biggest wait, the open conditions and the top recommendations as
  lines, and an "Open report" link when the report has an address (no setting provides one
  yet, so the link is not shown). Telegram gets HTML, Slack
  rich_text blocks, with the same escaping, size limits, de-duplication and retries as alerts;
  "Open report" and "and N more" follow `[alerts] language`, the report's own text stays
  English ([#169](https://github.com/ch8er/kelpie/issues/169)).

#### Operations

- `kelpie serve` runs the control loop with `/metrics` and `/healthz`, and `kelpie status`
  lists the rules that would load with their mode ([#7](https://github.com/ch8er/kelpie/pull/7)).
- `kelpie doctor` checks a first live run (config, observe mode, boards, hooks, spans, git,
  Prometheus), with a Hermes quickstart; a Claude Code quickstart and checks followed, and
  a warning when block reasons name PRs but nothing reads them
  ([#22](https://github.com/ch8er/kelpie/pull/22), [#89](https://github.com/ch8er/kelpie/pull/89), [#137](https://github.com/ch8er/kelpie/pull/137)).
- `kelpie why <task>` explains why a task is not moving, read-only and offline by default
  ([#87](https://github.com/ch8er/kelpie/pull/87)).
- Snoozes, part 1 ([#176](https://github.com/ch8er/kelpie/issues/176)): `kelpie snooze <task>
  --until <date|duration>` records that a task waits on purpose until a date (a bare date
  means through that day, in `[report] timezone`), and `kelpie unsnooze` and `kelpie
  snoozes [--json] [--all]` manage them. With `[snooze] marker = true` (off by default:
  any agent can write it), an `until:YYYY-MM-DD` marker in a block reason records one from
  the board, ending at most `marker_max_days` (30) after the block was set. Snoozes are
  stored in `kelpie.db` (schema 8), put into every snapshot and shown by `kelpie why`;
  `kelpie doctor` counts the markers it cannot honour on every local board. No rule acts on
  them yet.
- `kelpie report` prints the daily report as Markdown, and `--send` posts the summary to the
  alert channels ([#47](https://github.com/ch8er/kelpie/pull/47), [#52](https://github.com/ch8er/kelpie/pull/52)).
- Prometheus metrics for the engine, adapters, connectors, traces and budgets, and a
  provisioned Grafana dashboard ([#7](https://github.com/ch8er/kelpie/pull/7), [#42](https://github.com/ch8er/kelpie/pull/42)).
- A tag-triggered release pipeline to TestPyPI, PyPI and GHCR ([#105](https://github.com/ch8er/kelpie/pull/105)).
- Documentation: install, configuration, privacy and extension guides ([#77](https://github.com/ch8er/kelpie/pull/77)), and an act
  checklist, the gate before act mode ([#46](https://github.com/ch8er/kelpie/pull/46), [#138](https://github.com/ch8er/kelpie/pull/138)).
- `kelpie demo`: a working day of real farm incidents (a stale block, a runaway loop, a
  forgotten hold, a human task gating agents) on a simulated Hermes farm (`kelpie.sim`),
  with the real Kelpie watching and the day's report at the end; offline, no config
  ([#134](https://github.com/ch8er/kelpie/pull/134), [#151](https://github.com/ch8er/kelpie/pull/151)). `--web` opens the report as a self-contained web page
  ([#152](https://github.com/ch8er/kelpie/pull/152)).
- Multi-host, work in progress: the design (`docs/multi-host.md`) and the first two steps,
  `kelpie agent` and the authenticated, bounded transport. `kelpie serve` does not yet
  accept agent reports, so multi-host is not usable end to end in this pre-release
  ([#96](https://github.com/ch8er/kelpie/pull/96), [#119](https://github.com/ch8er/kelpie/pull/119), [#124](https://github.com/ch8er/kelpie/pull/124)).

### Safety

- Dry-run is on by default and every rule starts in `observe`. `act` mode is refused at
  startup for rules whose actions the configured adapter cannot yet carry out ([#4](https://github.com/ch8er/kelpie/pull/4), [#13](https://github.com/ch8er/kelpie/pull/13)).
- A failed or unexecutable action always alerts at warning or above, once per failure
  rather than once per attempt ([#45](https://github.com/ch8er/kelpie/pull/45), [#72](https://github.com/ch8er/kelpie/pull/72)).
- Text that agents control cannot inject Markdown into the report, and ids, roles, models
  and tool names are escaped ([#108](https://github.com/ch8er/kelpie/pull/108), [#111](https://github.com/ch8er/kelpie/pull/111)).
- Outbound requests (Prometheus, quota probes, the LLM section) go through one HTTP helper
  with a single deadline, no redirects and a response size cap ([#35](https://github.com/ch8er/kelpie/pull/35)).
- Data from agents is decoded strictly, and an agent only runs the action kinds its
  adapters declare ([#124](https://github.com/ch8er/kelpie/pull/124)).
- A Hermes stop also ends the stopped run's worker processes (submitter, agent and the
  agent's tool commands) once their pid and start time match what Hermes recorded, instead of
  leaving them to Hermes' reaper for two minutes or more; the alert and `kelpie why` say what
  happened, or why Kelpie did not signal them ([#186](https://github.com/ch8er/kelpie/pull/186)).

### Fixed

- `kelpie why` showed a finished parent as `parent-unverified` ("the code host's PRs are not
  fully known") while `kelpie serve` asked to close it: serve knows the PR lists in full only
  after two scans in a row, `why` reads them once (`--refresh`) or not at all. A
  `parent-unverified` finding now carries a note saying so where serve may judge it
  differently, in the text and in `--json` (`findings[].note`); `why` stays read-only and
  scans only once
  ([#148](https://github.com/ch8er/kelpie/issues/148)).
- A run Kelpie stopped was alerted on as a stale block after 4 hours, and the report then
  advised block conditions for it. Kelpie now signs its stops, as it signs its parks; rule
  4 leaves a verified stop to rule 13, which lists it as waiting for a person, and rule 1
  never requeues it. Stops made before the upgrade are unsigned: they still alert after 4
  hours and are listed as waiting for input. On the demo farm, parks and stops are issued
  on its virtual clock ([#141](https://github.com/ch8er/kelpie/issues/141), [#158](https://github.com/ch8er/kelpie/pull/158)).
- The daily report recommended act mode for rules the farm's adapters cannot carry out, a
  config `kelpie serve` then refused (on Hermes, `stale_blocks`). It now recommends act mode
  only where the startup check accepts it and says what is missing for the others; the
  advice lists the tasks the rule would have acted on
  ([#142](https://github.com/ch8er/kelpie/issues/142), [#159](https://github.com/ch8er/kelpie/pull/159)).
- A task parked by `dispatch_gate` on a CI condition the platform registered without a commit
  was never woken by a fixing push: the GitHub connector wrote the head it saw into the
  condition's `sha`, and the park marker pinned it there. The connector now records that head
  only as `head_sha`, a result that is not parked. Upgrade note: tasks parked before the fix
  may stay pinned to an old commit. `kelpie why <task> --refresh` shows "pinned to <sha>; PR
  head is now <sha>"; unpark such a task by hand (`hermes kanban unblock <task>`, or the
  platform's equivalent) ([#147](https://github.com/ch8er/kelpie/issues/147),
  [#162](https://github.com/ch8er/kelpie/pull/162)).
- Lasting-state alerts failed to read their escalation defaults from a slotted dataclass,
  found on the first live run ([#24](https://github.com/ch8er/kelpie/pull/24)).
- Nested LLM spans were counted twice in token totals ([#29](https://github.com/ch8er/kelpie/pull/29)).

### Internal

- Alert wording is now structured fields passed through the `Alerter` port ([#135](https://github.com/ch8er/kelpie/pull/135)).
- One hold-labels helper shared by the app and the rules ([#99](https://github.com/ch8er/kelpie/pull/99)).
- A simulated Hermes farm with end-to-end scenarios against `kelpie serve`, including act
  mode and the daily report ([#62](https://github.com/ch8er/kelpie/pull/62), [#73](https://github.com/ch8er/kelpie/pull/73), [#74](https://github.com/ch8er/kelpie/pull/74), [#81](https://github.com/ch8er/kelpie/pull/81)).
- A code health and security audit plan was written before the first release ([#92](https://github.com/ch8er/kelpie/pull/92)).

[0.1.0a1]: https://github.com/ch8er/kelpie/commits/main
