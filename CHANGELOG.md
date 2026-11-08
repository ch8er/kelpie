# Changelog

All notable changes to Kelpie (distributed as `kelpie-sre`) are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[PEP 440](https://peps.python.org/pep-0440/), so a pre-release is written `0.1.0a1`, not
`0.1.0-alpha.1`. The release process is in [docs/release.md](docs/release.md).

## [Unreleased]

### Added

- The Hermes adapter carries out `unblock` (`hermes kanban unblock`, only the block its
  snapshot showed, never Kelpie's own stop) and `reassign` (`hermes kanban assign` of a
  `todo`, `ready` or `review` task no run claims, to an agent's profile, with a comment
  saying why), and declares both. `stale_blocks` and `orphan_status` may now run in `act`
  on Hermes (they stay in `observe` by default). The simulated Hermes CLI applies
  `unblock --reason`, `assign` and `comment`.
- The `[view]` listener also serves a read-only page for a phone (`GET /`, #299): today's
  cost of chaos and spend so far, then per board the open tasks by attention (needs you,
  stuck or blocked, running long past `[view] long_after`, the rest) and the open alerts.
  No script, no form; a browser gives the token as the password of its sign-in prompt. A
  web manifest lets a phone add it to the home screen. Off by default, on loopback.
- `kelpie status` shows the farm after the rules: tasks per status (per board), waiting
  conditions, open alert states and the last actions per rule, read-only (the board offline,
  the database without writing to it; with no adapter, the last statuses `kelpie serve`
  observed). `--json` prints the farm view alone for scripts, `--server [URL]` reads it from
  a running `kelpie serve`. The view model is `kelpie.core.farmview`.
- `[view]` (off by default): `kelpie serve` serves the farm view read-only as JSON on a
  listener of its own (`GET /v1/farm`, 127.0.0.1:9465). No write endpoint; with
  `[view] token_env` set every request needs the bearer token, and off loopback `kelpie
  serve` refuses to start without a strong one; `kelpie doctor` warns about it first.
- Block storms (rule 4, #229): once `storm_mode = "rule"`, rule 13's "needs you" digest
  folds the person's items of a storm's tasks into one item for the storm, so the person
  reads one cause instead of each block again; a task joining the storm no longer
  re-announces the digest. A storm's "resolved" notice says how many of its tasks were
  unblocked ("Block storm over: 5 of 7 tasks unblocked") and which are still blocked.
- Rule `unanswered_asks` (P1, #297, observe by default): an agent's `@question` to another
  agent in a task comment is an obligation until the recipient (or a person) answers.
  Kelpie reminds only while the recipient is idle, at most once per step (`steps`, default
  1h, 4h, 12h, 24h), stops after `stop_after` (3d), says `delivery: unknown` when it
  cannot tell the question was seen, and honours snoozes on the asking task. Blocked and
  waiting tasks and questions to people stay with rules 4, 2 and 13, so nothing is alerted
  twice.
- `kelpie serve` records the task status changes it observes on every tick (status and the
  platform's own status, per task, board and adapter) in its database, and the daily
  report's "Farm throughput" stages use them where the board keeps no history: queue time
  before a task's first pickup, and status changes between sessions. The section says
  which source it used. Recorded rows follow `[storage] retention` and `max_rows` (#157).
- `scripts/bootstrap.sh` prepares a fresh checkout or cloud session for the local checks
  (uv, Python 3.12, `uv sync --frozen`) in one idempotent step; `--check` also runs them.
- A design note in `docs/security.md` for answering an agent's question from the chat
  (#300): threat model, guards and the rejected presses. Proposed only; nothing is
  implemented and Telegram buttons still only snooze.

### Changed

- **The database schema is now 12**: `kelpie serve` migrates an older database at start.
  Older Kelpie versions (0.1.0a3 and before) refuse a database once it is migrated, and
  `kelpie report` and `kelpie status` of this version ask to run `kelpie serve` once on a
  database not yet migrated (#157).
- docs/privacy.md and docs/install.md state what `[storage] retention` prunes (raw ledger
  rows, closed alert states, observed status transitions); they still said the ledger was
  never pruned. The configuration reference's contents list every rule and `[instructions]`.

### Fixed

- Docs: the Hermes quickstart names the rules that can act on Hermes (it said only `stop`
  existed), the OpenClaw quickstart's doctor legend includes WARN, the multi-host
  check says when `kelpie_agent_up` turns 1, and the Hermes README's `human_decisions`
  example loads on its own. `kelpie doctor`'s OTLP hint no longer points at the Hermes
  quickstart's step number. A test now loads every TOML example in the docs and parses
  every `kelpie` command they show, so a renamed key or flag fails CI.

### Security

- A Hermes, Claude Code or OpenClaw spool line, a Claude Code task file or a Hermes event
  payload nested too deep for the JSON decoder (about 10 KB of `[`) is skipped like any
  malformed line. It used to raise `RecursionError` out of the adapter, so one such line
  from any agent left the adapter's whole snapshot out of every tick, and `kelpie spool
  scrub` aborted. OTLP tool arguments nested that deep are hashed as text, no longer
  rejecting the span as malformed (#364).
- Stop markers of the Claude Code and OpenClaw adapters are written relative to a handle
  on the stop folder, opened without following a symlink: an agent can no longer swap the
  folder for a symlink between Kelpie's check and its write (audit L4, #336).
- Outbound clients (`kelpie.net`, the GitHub client, trace forwarding, the doctor's
  Prometheus check) share one connect deadline across every resolved address, instead of
  giving each address the whole remaining time (audit L13, #336).

## [0.1.0a3] - 2026-11-06

Third alpha: 0.1.0a2 plus the macOS fix for the Hermes plugin (#353), verified on a live
macOS farm with `kelpie selftest stop --adapter hermes`. Nothing else changed. Like the
earlier alphas it starts in global dry-run (`dry_run = true`) with every rule in `observe`
mode.

### Before you install

These are the release notes for the tag, ahead of what changed.

**Upgrading from 0.1.0a2**

- **Update the Hermes plugin on every agent host**, from the installed package, in the
  user plugin directory and in every profile that has a copy, then restart the Hermes
  gateway(s). The first line finds the Python kelpie-sre is installed in (also with pipx or
  `uv tool install`, whose environments a plain `python` does not see); adjust `~/.hermes`
  if your Hermes home is elsewhere (with `pip install --user` or a system-wide install, set
  `PY` to the Python you installed kelpie-sre with):

  ```sh
  PY="$(dirname "$(readlink -f "$(command -v kelpie)")")/python"
  PLUGIN="$("$PY" -c 'import kelpie.adapters.hermes.plugin as p; print(p.__file__)')"
  if [ -f "$PLUGIN" ]; then
    cp "$PLUGIN" ~/.hermes/plugins/kelpie/__init__.py
    for d in ~/.hermes/profiles/*/plugins/kelpie; do [ -d "$d" ] && cp "$PLUGIN" "$d/__init__.py"; done
  else
    echo "kelpie-sre is not importable from $PY: nothing copied"
  fi
  ```

  On a multi-host farm, upgrade `kelpie-sre` on each agent host (where `kelpie agent` runs)
  and copy the plugin from there.
- **On macOS, rule 10 can now stop Hermes runs from spool evidence.** With the 0.1.0a2
  plugin every macOS spool line carried `"pid_start": null`, so rule 10 only alerted ("Not
  stopped") on such runs. Once the plugin is updated, a rule 10 in `act` stops a looping
  Hermes run on macOS from spool lines alone. If you keep rule 10 in `observe` or `suggest`,
  nothing changes. Linux spool lines are unchanged.
- **On macOS, workers started before the plugin update keep writing `pid_start: null`
  until they restart**: a worker loads the plugin once, so its lines stay unverified (rule
  10 alerts, does not stop) until that worker ends. Then run `kelpie selftest stop
  --adapter hermes`: every step should PASS. The self-test installs the installed
  package's plugin, not your copies, so also check that a fresh worker's spool lines carry
  a `pid_start` of the form `"|<digits>"`, not `null`.

**Rule 10 in `act` still trusts the shared traces token and Claude Code or OpenClaw spool
lines** (audit M1, narrowed; unchanged from 0.1.0a2). Per-agent tokens and the Hermes writer
check close it where they apply. What is left: spans under the shared token or a tokenless
ingest still report for every session, and Claude Code and OpenClaw spool lines name their
session themselves. Where agents that do not trust each other share a farm, give each its
own `[[traces.tokens]]` entry and set `spool_stops = false` in `[adapters.claude_code]` /
`[adapters.openclaw]` (their spool lines alone then only alert), or keep rule 10 in
`observe` or `suggest`. Details:
[docs/security.md](docs/security.md#known-residual-risks).

**Open findings** of the security audit
([#336](https://github.com/ch8er/kelpie/issues/336)), unchanged from 0.1.0a2, all low, and
what is left of M7:

- L4: stop-marker writes in the Claude Code and OpenClaw adapters are not relative to a
  directory handle; matters only if Kelpie runs with more privilege than the agents.
- L13: outbound clients give each resolved address the whole remaining connect time, so a
  host with several blackholed addresses can exceed the deadline several times.
- L14, what is left: the Dockerfile pins its base images by tag, not digest.
- M7, what is left: one acting instance per board is enforced on one machine, not across
  machines or users for a GitHub repository or Linear team. Run one acting Kelpie per
  tracker.

**Think before exposing `kelpie serve` off loopback**, as before: `/metrics` and `/healthz`
are unauthenticated; firewall the port or put an authenticating proxy in front
([docs/security.md](docs/security.md#listeners)).

**Known limits.** Verified live before this release: the macOS fix, on one macOS farm
(Hermes Agent v0.22.5), with `kelpie selftest stop --adapter hermes`. Not verified: live
runs on a real Linux Hermes and on real Claude Code and OpenClaw platforms with this
version, a macOS Hermes without psutil (it records no worker fingerprint, so its spool
lines stay unverified), the per-tick cost of per-agent token bindings on a busy farm
(about 0.7 s with 700k bindings, measured offline), the Docker image (the release
workflow builds and publishes it), and GitHub Issues and Linear beyond the token check. The
wheel and sdist were installed and `kelpie demo --fast` run on Python 3.12 and 3.13.
Upgrading from 0.1.0a1: read 0.1.0a2's notes below first.

### Fixed

- **macOS: rule 10 can stop a Hermes run from spool evidence** (#353). The Hermes plugin of
  0.1.0a2 took its start fingerprint only from `/proc`, so on macOS every spool line carried
  `"pid_start": null`, the writer check could never pass, rule 10 only alerted ("Not
  stopped") and `kelpie selftest stop` failed ("rule 10 did not stop run 1 within 300s").
  The plugin now reads its start through libproc (`proc_pidinfo(PROC_PIDTBSDINFO)`, ctypes,
  standard library only) and writes `"|<start>"` in centiseconds, the form a macOS Hermes
  records for the run's worker. The structure and the centiseconds are defined once, in the
  plugin, and Kelpie's process table (which verifies a worker before ending it) uses that
  definition. Linux lines are unchanged. **Update the plugin on every macOS agent host**
  (`cp src/kelpie/adapters/hermes/plugin.py ~/.hermes/plugins/kelpie/__init__.py`, and to
  every profile's `plugins/kelpie/__init__.py` that has a copy), then run
  `kelpie selftest stop --adapter hermes` again.
- `kelpie selftest stop`, when rule 10 does not stop the run, now says why when the spool
  tells: lines without `pid_start`, lines whose writer is not the run's recorded worker, or
  a Hermes that recorded no worker fingerprint (a macOS Hermes without psutil).

### Internal

- The self-test's fake Hermes no longer writes spool lines itself: it loads the plugin the
  self-test installed and calls its `post_tool_call` hook in the agent process, as Hermes
  does, so the self-test (run 15 times in a row in the macOS CI job) goes through the real
  spool path and writer check. A plugin whose lines cannot stop a run fails it.
- The macOS CI job also runs the writer tests: the plugin's fingerprint of a live process
  equals the one Kelpie's process table computes and the one Hermes records through psutil.

## [0.1.0a2] - 2026-11-05

Second alpha: the first live farm's fixes and the security audit's follow-ups. Like
0.1.0a1 it starts in global dry-run (`dry_run = true`) with every rule in `observe` mode.

### Before you install

These are the release notes for the tag, ahead of what changed.

**Upgrading from 0.1.0a1**

- **Back up `kelpie.db` first if you may go back.** It migrates to schema 11 on the first
  start, and 0.1.0a1 refuses a newer schema.
- **Update the Hermes plugin, the Claude Code hook and the OpenClaw plugin on every agent
  host.** Hermes spool lines now carry their writer: rule 10 stops a Hermes run on spool
  lines only when the run's own worker wrote them, so with an older plugin (or on macOS)
  it stops Hermes runs only from OTLP evidence and alerts otherwise. All hooks redact more
  of a command (`echo`/`printf` arguments, words after `auth` or `login`); `kelpie spool
  scrub` cleans spools written before, except a value an escaped blank already split off
  (`FOO_TOKEN=a\ b make`): delete or rotate those spools.
- **On a multi-host farm, upgrade `kelpie agent` on every agent host too.** The Hermes writer
  check, the `spool_stops` marking and the per-board lock run where the adapter runs: a
  0.1.0a2 hub with 0.1.0a1 agents still gets unchecked spool lines.
- **Rule 10 evidence is scoped per agent where you configure it.** `[[traces.tokens]]` gives
  each agent's exporter its own token, limited to its own sessions; spans for another
  agent's session are rejected. The shared `[traces] token_env` keeps working unchanged,
  and `kelpie doctor` warns while a rule acts on it ("traces tokens per agent").
  Migration: [docs/security.md](docs/security.md#per-agent-traces-tokens).
- **Ledger retention runs by default.** `kelpie serve` prunes raw action-ledger rows older
  than `[storage] retention` (`"30d"`) or past `max_rows` (`1000000`), by whole UTC days,
  after summing each day into daily aggregates kept for good (the report's before/after
  reads those). To keep more, raise them (`retention` up to `"3650d"`); see
  [configuration](docs/configuration.md#storage).
- **Multi-board Hermes configs:** `kelpie selftest stop` now works with
  `[[adapters.hermes.boards]]` (it failed before rule 10 could stop the run), so rule 10's
  act gate can be met on such a farm.
- **The self-test needs Hermes to keep boards in WAL mode** (its own default): a new step,
  "board in WAL mode", fails `kelpie selftest stop` when the board Hermes creates for the
  test uses another journal mode.
- **`~` is expanded in every path-valued config key**, including `[storage] path`, `[agent]
  ca_file`, `[hub] tls_cert`/`tls_key` and the Hermes and OpenClaw `cli`; `kelpie doctor`
  names paths that do not exist ("config paths exist", FAIL for a missing ledger
  directory).
- **Three may refuse to start:** a second acting Kelpie on the same board (one acting
  instance per board is now enforced with a lock); `kelpie serve` with a weak traces token
  (under 16 characters, spaces, or fewer than 6 different characters) while OTLP ingest
  listens off loopback, unless `allow_unauthenticated` is set; and a config whose
  `[engine] once_window`, `cooldown` or `retry_after` is longer than `[storage] retention`
  (30 days by default): raise `retention` to match.

**Rule 10 in `act` still trusts the shared traces token and Claude Code or OpenClaw spool
lines** (audit M1, narrowed; this replaces 0.1.0a1's "trusts every holder of the traces
token"). Per-agent tokens and the Hermes writer check close it where they apply. What is
left: spans under the shared token or a tokenless ingest still report for every session,
and Claude Code and OpenClaw spool lines name their session themselves. Where agents that
do not trust each other share a farm, give each its own `[[traces.tokens]]` entry and set
`spool_stops = false` in `[adapters.claude_code]` / `[adapters.openclaw]` (their spool
lines alone then only alert), or keep rule 10 in `observe` or `suggest`. Details:
[docs/security.md](docs/security.md#known-residual-risks).

**Open findings** of the security audit
([#336](https://github.com/ch8er/kelpie/issues/336)), all low, and what is left of M7:

- L4: stop-marker writes in the Claude Code and OpenClaw adapters are not relative to a
  directory handle; matters only if Kelpie runs with more privilege than the agents.
- L13: outbound clients give each resolved address the whole remaining connect time, so a
  host with several blackholed addresses can exceed the deadline several times.
- L14, what is left: the Dockerfile pins its base images by tag, not digest.
- M7, what is left: one acting instance per board is enforced on one machine, not across
  machines or users for a GitHub repository or Linear team. Run one acting Kelpie per
  tracker.

**Think before exposing `kelpie serve` off loopback**, as for 0.1.0a1: every listener has
connection guards (`[server]`), but `/metrics` and `/healthz` are unauthenticated; firewall
the port or put an authenticating proxy in front
([docs/security.md](docs/security.md#listeners)).

**Known limits.** Not verified before this release: live runs on real Hermes, Claude Code
and OpenClaw platforms with this version (on a real Hermes, that its hooks run in the
recorded worker process: `kelpie selftest stop` is the live check), the per-tick cost of
per-agent token bindings on a busy farm (about 0.7 s with 700k bindings, measured
offline), the Docker image (the release workflow builds and publishes it), and GitHub
Issues and Linear beyond the token check. The wheel and sdist were installed and
`kelpie demo --fast` run on Python 3.12 and 3.13.

### Added

- Action-ledger retention (#318): `[storage] retention` (30 days) and `max_rows` (a
  million) bound the raw action ledger, pruned by whole UTC days in a background thread of
  `kelpie serve` on its own connection, in short transactions. Each day is summed first into
  daily aggregates kept for good (per rule, action kind and mode: entries, executed,
  suppressed, failed), from which the report's before/after reads a rule's mode change once
  raw rows are gone. Alert states closed past retention go too; open states and snoozes
  never. The in-memory ledger keeps at most 100,000 entries. Schema 11 adds the aggregate
  tables; `kelpie doctor` shows the database's size and its oldest raw ledger row.
- Known runners anywhere in a command (#197 part 2b): `[traces] known_runners` (OTLP) and
  `KELPIE_KNOWN_RUNNERS` on agent hosts (the Hermes plugin, the Claude Code hook and the
  OpenClaw hook, which also takes `knownRunners` in its config) name build and test runners
  such as `ios_sim.sh`, `xcodebuild` or `npm test`. A command that names one anywhere
  (after `cd … &&`, `export …`, `env …`, in a pipe, inside `bash -c`) records it
  (`kelpie.tool.runners`, a spool line's `runners`: names from the list only, never the
  command's words), and the largest tool outputs group the call by it. Empty by default:
  nothing changes for farms that do not set it.
- `kelpie ci-config`: rule 9's proposed CI configuration changes, printed as a diff of each
  GitHub Actions workflow file (concurrency with cancel-in-progress, `paths-ignore` for docs,
  skipping drafts). Read-only; it needs only the read token. `ci_economy` emits the
  proposals as `propose_ci_config` with `propose_config = true` (off by default)
  ([#237](https://github.com/ch8er/kelpie/pull/237)).
- `propose_ci_config` opened as a **draft** pull request by the GitHub CI connector, from
  `kelpie/ci-config/<workflow file>`, when `[connectors.github.ci] allow_propose` is set for
  the repositories in `propose_repos`, with a token of its own (`propose_token_env`).
  Never on the default branch, never merged by Kelpie, one open proposal per repository,
  and a change a human turned down is not proposed again
  ([#237](https://github.com/ch8er/kelpie/pull/237)).
- Review economy in the daily report (#298): per repository and kind of change (docs only,
  tests only, generated files, a dependency bump, a small change, code), review rounds,
  review tokens and how often a review found something (a return, or another round). A
  light kind whose full reviews almost never find anything (at most 5% of at least 20)
  gets a recommendation for a lighter path, with the evidence and the tokens a day it
  would save; fewer reviews are "direction only", and code or a kind above the threshold
  never gets one. Kelpie only recommends; it never changes how tasks are reviewed
  (`[report.sections.review_economy]`).
- Tokens per return (#197 item 7): the "Farm throughput" section lists "Tokens on rework",
  the model calls on each task returned within the day after its first return, per role
  (who made the call) and per task, with tokens per return
  ([#326](https://github.com/ch8er/kelpie/pull/326)).

### Changed

- Before/after of fixes leaves out the tokens an outage held (#332, after #197 item 6):
  tokens per day and tokens per completed task no longer count the calls an outage's tasks
  made while it held them, attributed as the throughput section attributes them (#325),
  and each fix says how many tokens it left out. Without an outage the output is unchanged.
- Every outage kind in the report (#229, incident 20): a block storm's waits count under "a
  shared blocker" for the tasks it names, even when its reason names a person (a tool
  failing for many tasks, under "a shared tool failure"), and in the cost of chaos an
  outage's tasks (a CI outage's too; a finished one until it finished) count once, under
  "farm outages", from the outage's first alert that counts, ahead of the person's inbox and
  their stale blocks. The "Outages" list names each outage's kind, scope and who can end it;
  an outage whose check is still in observe is listed, and its waits stay where they were
  (until it is switched on; switched back to observe, it stops counting)
  ([#270](https://github.com/ch8er/kelpie/pull/270)).
- Outage tokens (#197 item 6): the model calls an outage's tasks made while it held them
  are the outage's, per role: in its line under Outages, in its cost-of-chaos line, and out
  of each role's cost per completed task. A call without a task id finds its task through
  its session, now including the platform's usage records (`Snapshot.usage`, #271)
  ([#325](https://github.com/ch8er/kelpie/pull/325)).

### Safety

- `spool_stops` in `[adapters.claude_code]` and `[adapters.openclaw]` (audit M1, #302, opt
  in; default `true`, unchanged): `false` marks that adapter's spool tool lines unverified,
  so rule 10 never stops a run on them alone. It sends its critical alert ("Not stopped")
  instead, and the platform's own OTLP tool spans of the run (under a per-agent token) still
  stop it. On OpenClaw, whose OTLP spans carry no session yet, that means alerts only. For
  agents that do not trust each other but share a spool
  ([docs/security.md](docs/security.md#known-residual-risks)).
- Hermes spool lines carry their writer (audit M1, #302): the plugin writes its `pid` and
  `pid_start` (its start fingerprint, as Hermes records a worker's), and the adapter checks
  them against the run's recorded worker. A tool line from another process, or one it
  cannot check (an older plugin, macOS, a run without a recorded worker), is kept but marked
  unverified, and rule 10 does not stop a run on such lines alone: it sends its critical
  alert ("Not stopped") and leaves the stop to a person. **Update the plugin on every agent
  host**, or rule 10 stops Hermes runs only from OTLP evidence. With per-agent traces
  tokens, the 0.1.0a1 release note "Rule 10 in `act` trusts every holder of the traces
  token" narrows to the shared token and Claude Code or OpenClaw spools
  ([docs/security.md](docs/security.md#known-residual-risks)).
- Per-agent traces tokens (audit M1, #302): `[[traces.tokens]]` gives each agent's exporter
  a token of its own that may report spans only for its owners, roles, sessions (patterns)
  or boards. Who a session or task belongs to comes from the board: a span for another
  agent's session or task is rejected and counted
  (`kelpie_spans_rejected_total{reason="out_of_scope"}`); a session or task the board does
  not show yet belongs to the first token that reports it, and once the board places it
  elsewhere that token's spans naming it are deleted. A span inherits its parent's session
  and task only when both came with the same scoped token.
  So an agent with its own token can no longer make rule 10 stop another agent's run with
  forged spans. `kelpie traces rotate-token --token <name>` rotates one of them. The shared
  `[traces] token_env` keeps working unchanged; `kelpie doctor` warns ("traces tokens per
  agent") while a rule is in `act` and the shared token (or no token) still reports for
  every session. Migration: [docs/security.md](docs/security.md#per-agent-traces-tokens).
- One acting Kelpie per board is enforced: `kelpie serve` with `dry_run` off and a rule in
  `act` whose actions an adapter carries out (not one that only alerts or cancels CI
  builds), and `kelpie agent` unless `[agent] actions = []`, hold a lock per board; a second
  acting instance on the same board refuses to start, naming the board and the holder's
  pid. Observe and suggest instances are unaffected (audit M7, [#321](https://github.com/ch8er/kelpie/pull/321)).
- The Linear client goes through Kelpie's HTTP helper: one deadline per request however
  slowly the server answers, and a time budget for all the reads of one refresh
  (`[adapters.linear] tick_budget`, 30s), so a slow Linear cannot stall the control loop
  (audit M4, [#324](https://github.com/ch8er/kelpie/pull/324)).
- Command redaction (all four copies: OTLP ingest, the Hermes plugin, the Claude Code hook
  and the OpenClaw hook) no longer keeps a secret passed as a plain word to `echo`,
  `printf` or `yes` (`echo pw | docker login --password-stdin` →
  `echo *** | docker login --password-stdin`) or after a credential word such as `auth`
  or `login` (`redis-cli auth pw` → `redis-cli auth ***`; `gh auth status` is now
  `gh auth ***`), and an escaped blank no longer ends an argument
  (`FOO_TOKEN=a\ b make` kept `b`; now `make`). Audit items L7 and L8
  ([#275](https://github.com/ch8er/kelpie/issues/275)). Update the hooks on agent hosts.
  `kelpie spool scrub` cleans the `echo` and credential-word leaks in spools written
  before, but not a value an escaped blank already split off (an old `hunter2 make` reads
  as an ordinary command): delete or rotate spools from farms that use such assignments.
- The multi-host hub takes from an agent only Kelpie's own adapter names (narrowed by
  `[[hub.agents]] adapters`) and tells apart at most 16 unreadable boards per adapter;
  other names are refused, counted in `kelpie_hub_refused_total` and alerted once per
  agent; host sleeps an agent reports are kept only when they ended and at most 64 per
  agent. One agent cannot grow the hub's executors, series, alerts or memory this way (audit M6, [#327](https://github.com/ch8er/kelpie/pull/327)).
- `kelpie serve` refuses a weak traces token (under 16 characters, spaces, or fewer than 6
  different characters) when OTLP ingest listens off loopback, unless
  `allow_unauthenticated` is set; this may stop a setup with a short
  `KELPIE_TRACES_TOKEN` published off loopback from starting. `kelpie doctor` warns about
  any weak token (audit L11). Workflow checkouts keep no token in `.git/config`, the release
  build's included (audit L14) ([#330](https://github.com/ch8er/kelpie/pull/330)).
- Agent text is bounded: task titles, comments, block, return and exit reasons and
  progress notes keep their first 8000 characters, and the ledger stores at most 16,000 of
  an action's reason and cuts long params (audit L9); role and pre-work status labels on
  `/metrics` are cut to 64 characters and the 33rd role on is summed under `other` (audit
  L10) ([#329](https://github.com/ch8er/kelpie/pull/329)).
- What Kelpie can write to GitHub, with the token each write needs:
  [docs/security.md](docs/security.md#what-kelpie-can-write-to-github).

### Fixed

- `kelpie selftest stop --adapter hermes` works with `[[adapters.hermes.boards]]`: it failed
  with `'HermesBoards' object has no attribute 'cli_environ'` before rule 10 could stop the
  run, so rule 10's act gate could not be met on a multi-board farm. With boards configured
  the self-test now stops the run through the boards adapter, routed by the board prefix of
  the run's id, as on the farm. The self-test (one board or several) also no longer reads the
  profiles' `state.db` of the user's Hermes home. Rule 10's real stop on a multi-board
  config was not affected: a test now checks it end to end on two boards (the block runs with that board's `HERMES_KANBAN_DB` and key, the worker
  ends, the other board is untouched), on Linux and macOS
  ([#333](https://github.com/ch8er/kelpie/issues/333)).
- `~` is expanded in every path-valued config key, in one place as the config is read.
  `[storage] path` kept a literal `~` (`kelpie serve` stopped with `cannot open ledger
  database ~/...`), and so did `[agent] ca_file`, `[hub] tls_cert` and `tls_key`, and the
  `cli` of the Hermes and OpenClaw adapters; the other keys were expanded each by its own
  loader or adapter. `$KELPIE_DB` is expanded the same way. Only a leading `~` (or
  `~user`); `$VARS` are not expanded. The keys are listed in
  `kelpie.configpaths.PATH_KEYS`, and a test fails when a settings field holds a path whose
  key is not there. `kelpie doctor` has a "config paths exist" line naming each path Kelpie
  reads that does not exist after expansion (FAIL when the ledger's directory is missing)
  ([#334](https://github.com/ch8er/kelpie/issues/334)).
- A Hermes stop no longer writes a GitHub PR URL into the task: rule reasons quote
  agent-controlled text (a tool or span name, ids; say `gh pr view
  https://github.com/owner/repo/pull/12`), and the `BLOCKED:`
  reason then made Hermes' `active_pr` guard hold the task back for 24 hours after a human
  unblocked it. Every text Kelpie writes into Hermes (stop, park, wake, comment) now goes
  through one cleaner, `kelpie.adapters.hermes.text`: PR URLs become `owner/repo#N`, control
  and format characters (bidi overrides, zero width, line separators) are replaced by a
  space, and the text carries neither marker. Parks, wakes and comments change slightly too:
  all format characters now become spaces (an emoji joined with zero-width joiners shows as
  its parts), and `kelpie-stop` in them reads `kelpie stop`, as `kelpie-wait` already did.
  The Claude Code stop reason also folds format characters (bidi overrides, zero width) into
  spaces and collapses runs of spaces, as OpenClaw's already did
  ([#160](https://github.com/ch8er/kelpie/issues/160)).
- `kelpie selftest stop` no longer fails "no model calls after the stop" when the
  agent's one request already sent when its worker was ended is answered just after the
  stop; any other call (two or more, or one in the 5s quiet window after a 1s drain) still
  fails it. A thread-scheduling race, seen on the macOS CI job.
- The self-test's flake on macOS (`attempt to write a readonly database`) is fixed at its
  cause. The test's fake Hermes kept its throwaway board in rollback-journal mode, unlike
  Hermes, which keeps boards in WAL mode. When the stop ended the worker in the middle of a
  heartbeat write, the worker left a hot journal behind. Kelpie opens boards read-only and
  never writes them, so it cannot roll that journal back. The fake now keeps its boards in
  WAL mode, where a killed writer leaves nothing to roll back. If a board ever does have a
  hot journal, the self-test now fails with that cause and what to do, not "unexpected
  OperationalError". A new step, "board in WAL mode", fails the self-test (rule 10's act
  gate) on a Hermes whose boards are not in WAL mode, where Kelpie could not read a board
  after a stop until Hermes writes it again. The macOS CI job runs the self-test 15 times in
  a row
  ([#320](https://github.com/ch8er/kelpie/issues/320)).
- CI (contributors): the required `ci-ok` check no longer passes, or reports skipped, for a
  PR head whose lint, test, e2e and macOS jobs did not run on that commit. A draft's
  `ci-ok` now fails, as does a run for a head the PR no longer has, so auto-merge cannot
  land an unbuilt head; docs-only changes still pass without the build
  ([#344](https://github.com/ch8er/kelpie/issues/344)).

## [0.1.0a1] - 2026-11-03

First public pre-release, an alpha: interfaces and configuration keys may still
change. It starts in global dry-run (`dry_run = true`) with every rule in `observe` mode,
so a fresh install changes nothing on your farm until you turn it on, rule by rule.

### Before you install

These are the release notes for the tag, ahead of what changed.

- **Think before exposing `kelpie serve` off loopback** (the default bind is `127.0.0.1`;
  docker compose publishes on `127.0.0.1` only). Every listener now has connection guards
  (header and body deadlines, connection caps, slow-client cutoff, body limits; `[server]`),
  but `/metrics` and `/healthz` are unauthenticated and the guards cannot stop a fast flood
  from many addresses: firewall the port to the networks that need it, or put an
  authenticating proxy in front ([docs/security.md](docs/security.md#listeners)).
- **Rule 10 in `act` trusts every holder of the traces token** (audit item M1). Spans and
  hook spool lines say which run they belong to, and every agent holds the one ingest
  token, so an agent can make `loop_breaker` stop another agent's run. If your agents are
  untrusted or sandboxed and share one token, keep rule 10 in `observe` or `suggest`, or
  give them separate farms. Fix: per-agent ingest tokens
  ([#302](https://github.com/ch8er/kelpie/issues/302)). Details:
  [docs/security.md](docs/security.md#known-residual-risks).
- Other open medium findings of the security audit
  ([#275](https://github.com/ch8er/kelpie/issues/275)), each fixed after this release:
  - M4: the Linear client has no overall deadline; a server that answers a byte at a time
    can stall the control loop (`[adapters.linear]` only). Follow-up on #275.
  - M6: one authenticated `kelpie agent` can grow the hub's executors, metric series and
    alerts by reporting new adapter names; give hub tokens only to agents you run.
    Follow-up on #275.
  - M7: only one Kelpie may act on a board, but nothing enforces it yet; two acting
    instances duplicate comments and requeues. Follow-up on #275.
- **Known limits.** Not verified before the release
  ([#249](https://github.com/ch8er/kelpie/issues/249)): the Docker image (its build and
  `kelpie demo` inside it; the release workflow builds and publishes it), live runs on real
  Hermes, Claude Code and OpenClaw platforms with this version (the adapters are tested
  against synthetic boards and spools, Hermes also against a simulated farm; plugin and
  hook registration were not run), and GitHub Issues and Linear beyond the token check.
  The wheel and sdist were installed and run on Python 3.12 and 3.13.

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
  Many tasks blocked on the same reason at once are one outage (a block storm), with the
  person who can end it; observe-only until switched on ([#252](https://github.com/ch8er/kelpie/pull/252)).
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
  CI jobs that never start (refused for billing or limits, or queued too long) across
  many PRs are one outage too, not a toolchain failure; observe-only until switched on
  ([#254](https://github.com/ch8er/kelpie/pull/254)).
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
- On a farm with several adapters, each rule's actions are judged per adapter: `kelpie
  doctor` and the daily report say which adapters a rule cannot act on yet (there its actions
  become alerts), and an adapter that declares its action kinds is sent no other
  ([#219](https://github.com/ch8er/kelpie/pull/219)).
- Host sleep and wake: a suspend (the wall clock running ahead of the monotonic clock
  between ticks) of the host `kelpie serve` runs on is recorded, and session ages, rule 1's
  grace and rule 3's ownerless and undriven checks count awake time, so a laptop that slept
  raises no requeue or orphan alert for the gap; a session that does not come back after
  the wake is requeued once as "ended while the host slept" ([#311](https://github.com/ch8er/kelpie/pull/311)).
- `kelpie agent` watches its own host the same way and reports its sleeps; the hub counts
  that agent's ages in awake time, never another host's ([#312](https://github.com/ch8er/kelpie/pull/312)).

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
- OpenClaw: reads Workboard cards and a spool written by a Kelpie plugin inside the
  Gateway, and stops a session (a marker the plugin enforces, plus aborting the run in
  flight); doctor checks and a quickstart ([#140](https://github.com/ch8er/kelpie/pull/140),
  [#149](https://github.com/ch8er/kelpie/pull/149), [#154](https://github.com/ch8er/kelpie/pull/154), [#155](https://github.com/ch8er/kelpie/pull/155)).
- GitHub Issues and Linear as the task queue of issue agents (Copilot coding agent, Codex
  cloud, the Claude Code Action, OpenHands): reads issues and their pull requests, and in
  `act` mode requeues, unblocks, comments and reassigns through the tracker's API; doctor
  checks and a quickstart ([#180](https://github.com/ch8er/kelpie/pull/180), [#192](https://github.com/ch8er/kelpie/pull/192),
  [#200](https://github.com/ch8er/kelpie/pull/200), [#203](https://github.com/ch8er/kelpie/pull/203), [#221](https://github.com/ch8er/kelpie/pull/221)).

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
- The traces ingest token can be rotated without dropping spans: `[traces] token_file`
  (an env file Kelpie reads again when it changes) and `kelpie traces rotate-token`, which
  writes a new token, keeps the old one valid for `token_grace` (as a hash only) and prints
  no token; grace use is on `/metrics`, and `kelpie doctor` warns about backups and readable
  files that hold the token ([#236](https://github.com/ch8er/kelpie/pull/236), [docs/security.md](docs/security.md#traces-token)).
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
- `kelpie fix --at` takes `now` and times before now (`-30m`, `-2h`, `-1d2h`) as well as
  ISO times, and the confirmation gives the recorded time in UTC ([#235](https://github.com/ch8er/kelpie/pull/235)).
- Toolchain versions per host from node_exporter's textfile collector (metric and labels
  are config data; `scripts/kelpie-toolchain.sh` writes the active Xcode and CoreSimulator
  on macOS, or any `TOOL=COMMAND`): an outage (rule 9's CI outages, rule 4's block storm,
  rule 10's tool failures) that begins within `toolchain_window` after a version change names
  the change as its likely cause, in the alert and the daily report (incident 20,
  [#284](https://github.com/ch8er/kelpie/issues/284)).
- Honest before/after: fixes less than a window apart are measured together and credited
  to none alone (per fix only its own role or command), per-day rates whose signal began
  inside the window are listed as not compared, throughput is compared only over hours
  when agent work was available (a spike of released backlog right after a fix is named
  and the next full day compared instead), and small samples say *direction only*
  ([#193](https://github.com/ch8er/kelpie/issues/193), [#284](https://github.com/ch8er/kelpie/issues/284)).
- Farm throughput per role, live gauges and Grafana panels; rework counted for the role that
  redid the work, not the reviewer ([#78](https://github.com/ch8er/kelpie/pull/78), [#116](https://github.com/ch8er/kelpie/pull/116)).
- A sections extension point with per-section settings, and token sections that read them
  ([#56](https://github.com/ch8er/kelpie/pull/56), [#84](https://github.com/ch8er/kelpie/pull/84), [#91](https://github.com/ch8er/kelpie/pull/91)).
- Runs inside `kelpie serve` at a configured time (`[report] at`) and reads the board as far
  back as its day needs ([#61](https://github.com/ch8er/kelpie/pull/61), [#67](https://github.com/ch8er/kelpie/pull/67)).
- An optional LLM root-cause section (`[report.llm]`, off by default). It sees only
  aggregates with anonymized ids, and its answer is escaped and marked as a hypothesis
  ([#95](https://github.com/ch8er/kelpie/pull/95)).
- Host sleep is listed apart ("Host asleep: 47 min", per host) and never counted in the
  hours lost: each host's sleep is taken out of the chaos states on its tasks ([#313](https://github.com/ch8er/kelpie/pull/313)).

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
  lines, and an "Open report" link when `[report] url` gives the report's address (a
  template with `{date}`). Telegram gets HTML, Slack
  rich_text blocks, with the same escaping, size limits, de-duplication and retries as alerts;
  "Open report" and "and N more" follow `[alerts] language`, the report's own text stays
  English ([#169](https://github.com/ch8er/kelpie/issues/169)).
- A block that asks a person (`waiting_human: needs_input: CEO: …`) reaches them once, in
  rule 13's "Waiting for you" digest: rule 4 no longer sends its "no condition Kelpie can
  check", overdue or dead-end alert for a task rule 13 lists, while rule 13 reaches people
  (it is loaded and not in `observe` while rule 4 alerts). Rule 4 reads the farm with rule
  13's settings and leaves it exactly what rule 13 lists, so each block is announced by one
  of the two; agents' dead ends, conditions Kelpie cannot see, blocks rule 13 does not list
  (parked on a person's task they cannot move yet) and blocks that wait on a condition
  (`needs_input: automerge PR #12`) stay rule 4's. Each digest item carries its exact
  `task_id`, `needed`, `since` and the number of tasks behind it (`blocking`), the totals
  count the tasks behind an approval, and every item is sent (`items_total`). "Needed" keeps
  the agent's whole first sentence (up to 140 characters, closing quotes included, a long
  quoted ask cut inside and closed) instead of a clause, and is left out when it would only
  repeat the title; for a person's own task whose block can never clear, it names the dead
  end or the condition Kelpie cannot see, which rule 4 no longer alerts on separately
  ([#169](https://github.com/ch8er/kelpie/issues/169)).
- A waiting agent task parked on a person's task by a `human_task` condition reaches them
  once, in rule 13's "Waiting for you" digest: rule 2 (`wait_condition`) no longer sends its
  overdue alert for a waiting task rule 13 lists, while rule 13 reaches people (it is loaded
  and not in `observe` while rule 2 is not), the way rule 4 leaves blocks to it. Rule 2
  reads the farm with rule 13's settings and leaves exactly what rule 13 lists; its dead-end,
  condition-not-visible, no-condition and wake outputs, a wait on a task the person cannot
  move yet and a wait that also depends on a PR or CI stay its own. Without rule 13 reaching
  people rule 2 alerts as before, and a snoozed human wait is still held. A listed wait
  whose conditions Kelpie cannot check (never checked, not checked within `max_condition_age`, or the connector reports an error)
  keeps rule 2's overdue alert, snoozed or not, worded as a connector problem ("C-1 was not
  checked on this pass: is the connector running?"). When a wait or a block
  moves to the digest while its alert is open, the "Resolved" notice says it is in the
  person's digest now instead of "no longer reported". Kelpie logs a
  warning at startup when rule 13's `human_wait_alert_after` is longer than rule 2's
  `max_wait`, the same check as for rule 4
  ([#213](https://github.com/ch8er/kelpie/issues/213)).
- The daily report's "agents waiting on people" no longer counts an agent task that also
  waits on a PR or CI first: rule 13 does not count it as waiting on the person's task,
  nor in that person's cost of delay ([#169](https://github.com/ch8er/kelpie/issues/169)).
- Each rule's alert opens with a short line of its own ("Runaway run on t_loop: 12 identical
  consecutive `heartbeat_check` calls") instead of the reason's first sentence, cut at 140
  characters and repeated in "Why"; an alert about one task adds what a person should do
  ("Needed: Stop the run if it is still going, then check the task"; left out once Kelpie
  did it in act mode) and, where the rule knows it, how many tasks wait behind it
  ("Blocking: 2 tasks", counted as rules 2, 4 and 13 count them). A headline longer than
  100 characters is cut at a word, never inside a quote. The reason stays whole under it as
  "Why", also when a task link is set, and is unchanged in the ledger and the report. These
  lines are the rule's English: `[alerts] language` translates Kelpie's fixed words only.
  Every rule but `needs_you`, whose digest has lines of its own
  ([#169](https://github.com/ch8er/kelpie/issues/169)).
- Rule 4 (`stale_blocks`) words each of its alerts: "T-1 blocked 44h with nothing Kelpie can
  check" (needed: "Clear the block, or register a condition Kelpie can check"), "T-1 blocked
  5h on a condition Kelpie cannot see: C-9", "T-1 blocked 9h, past the 8h limit: waiting
  for pr_merged", "Block on T-1 can never clear: PR #12 closed without merging" (needed:
  "Replace the blocker that can never clear") and "Evidence for T-1's block is stale:
  human_task not checked for 2h" (needed: "Check that whatever checks human_task conditions is running (kelpie doctor shows
  the connectors)", worded by condition kind because a rule cannot know connector names),
  each with the number of tasks waiting behind the block. The block's own text is not in them;
  it stays in "Why", and the reasons are unchanged
  ([#169](https://github.com/ch8er/kelpie/issues/169)).

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
  `kelpie doctor` counts the markers it cannot honour on every local board.
- Snoozes, part 2 ([#176](https://github.com/ch8er/kelpie/issues/176)): while a task is
  snoozed there are no repeat reminders for its wait. `needs_you` leaves a snoozed item out
  of the person's digest (its state key, counts and cost) and lists it in a footer,
  "Snoozed: <title> through <date> (<reason>, by <who>)" in English and Russian; a digest
  of only snoozed items does not open, and a snoozed item whose block can never clear comes
  back at once. `stale_blocks` holds its no-condition and overdue alerts on a snoozed
  block, and `wait_condition` its overdue alert on a wait for a snoozed human task; dead
  ends, unseen conditions and stale evidence still alert. The task alerts again when the
  snooze expires ("Snooze ran through <date>") or more work is blocked behind it ("2 more
  tasks blocked since the snooze"), counted against a baseline `kelpie serve` records at
  the first complete tick after the snooze and latched once seen (`kelpie.db` schema 9). Marker snoozes hold only waits on a person, never an
  agent-health alert, and are listed "from marker". `kelpie snooze` warns when the task is
  only parked behind a human task. In the daily report snoozed waits are their own wait
  cause, listed apart and never the biggest wait or in the cost of chaos.
- `kelpie selftest stop --adapter hermes` runs rule 10's stop end to end on a throwaway Hermes
  home and board, with a local mock model (zero tokens), and reports each step
  ([#190](https://github.com/ch8er/kelpie/pull/190)).
  It runs on macOS too, and `kelpie doctor` says how this host ends a stopped run's worker
  ([#231](https://github.com/ch8er/kelpie/issues/231)).
- Snoozes, part 3 ([#176](https://github.com/ch8er/kelpie/issues/176)): snooze from the
  alert itself. Slack shows a ready-to-copy `kelpie snooze <task> --until <date>` line under
  each item a snooze would quiet (rule 13's digest items, rule 4's no-condition and overdue
  alerts, rule 2's overdue wait on one human task; never agent-health alerts). Telegram,
  with `buttons = true` and `allowed_user_ids` (off by default), shows "Snooze 1d / 3d / 7d"
  buttons there: a press from an allowed user in the configured chat snoozes the task
  (`source = "chat"`, at most 7 days; stored under the global dry-run too, like `kelpie
  snooze`), records the press in the ledger in the same transaction (kind `snooze`: who,
  with their user id, what, until; it notes a dry-run), answers with a toast and edits the
  alert ("Snoozed until … by …", that item's buttons removed). A press never shortens a
  longer `kelpie snooze`: it is refused and says until when. A button carries only a short
  HMAC-signed reference (key generated on first use in `kelpie.buttons.db`, 0600) that
  expires after 24 hours or at the alert's next message (any, also when the alert resolves
  unseen below the channel's `min_severity`; the older buttons are then removed), and is
  used once; an expired button says so, with the `kelpie snooze` command that still works.
  A button that snoozes another task than its item's (rule 2: the human task) names it.
  Acknowledgements are built from Kelpie's copy of the message, so quick presses keep each
  other's lines; one that does not fit is a reply. Presses are read with
  `getUpdates` long polling in their own thread (offset persisted, at least a second
  between polls, back-off, shutdown deadline); anything else is ignored and counted in
  `kelpie_chat_rejected_total{reason}`, and strangers and other chats get no answer.
  `kelpie doctor` fails on an empty allow-list, a missing database or a bot with a webhook.
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
- Multi-host: `kelpie agent` on each machine with agents reads its boards and carries out
  the actions routed to it; a central `kelpie serve` with `[hub]` (per-agent tokens) merges
  the agents' boards and routes actions back. Setups for a laptop, a laptop and a cloud VM,
  and two VMs are in `docs/deploy.md` ([#96](https://github.com/ch8er/kelpie/pull/96), [#119](https://github.com/ch8er/kelpie/pull/119), [#124](https://github.com/ch8er/kelpie/pull/124),
  [#127](https://github.com/ch8er/kelpie/pull/127), [#128](https://github.com/ch8er/kelpie/pull/128), [#129](https://github.com/ch8er/kelpie/pull/129)).

### Safety

- `kelpie serve`'s main port (OTLP ingest, `/metrics`, `/healthz`) gets the hub's
  connection guards: header deadline, connection caps per listener and per address,
  slow-client cutoff, body deadline and size limits, configurable in `[server]` (audit M5,
  [#309](https://github.com/ch8er/kelpie/pull/309)).
- Kelpie's data directory is created `0700` and its database, reports and keys `0600`;
  `kelpie doctor` names existing files that are readable by others and never changes them
  itself (audit L5, [#314](https://github.com/ch8er/kelpie/pull/314)).
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
- On macOS too: Kelpie reads the worker's identity with libproc, as Hermes fingerprints it there,
  and checks it again right before each signal (macOS has no pidfd)
  ([#231](https://github.com/ch8er/kelpie/issues/231)).
- `[report] task_url` is checked like `[alerts] task_url`: an http(s) URL with `{id}`
  outside the host and no credentials. A relative or non-http template (`javascript:{id}`,
  `board://{id}`) used to be put into the report's task links as it was; such a config now
  fails to load with an error naming `[report] task_url`. Both keys must also stay within
  600 characters once a 64-character id fills `{id}`: a longer `[alerts] task_url` used to
  load and then drop every "Open task" link.
- The Hermes adapter reads its hook spool the way the Claude Code and OpenClaw adapters do:
  never through a symlink, never blocking on a FIFO, with a byte cap and overlong lines
  skipped. It also reads a worktree's `.git`, `commondir` and `gitdir` files that way. Before,
  an agent that put a FIFO at the spool or at its worktree's `.git` stalled the control loop
  until restart. The Hermes plugin no longer writes its spool through a symlink or blocks on
  a FIFO, and creates a new spool folder with mode 0700
  ([#275](https://github.com/ch8er/kelpie/issues/275)).
- OTLP ingest refuses a gzip body of more than 64 members (exporters send one): many tiny
  members cost quadratic CPU, minutes for one 8 MiB request. `[[traces.forward]]` never
  sends plain http through a proxy from the environment (`http_proxy`), which saw the
  forward headers and the spans in clear (use https to forward through a proxy); it reads at most
  64 KiB of a collector's answer ([#275](https://github.com/ch8er/kelpie/issues/275)).

### Fixed

- `kelpie fix` wrote its errors to stdout; they go to stderr now, as for `report`, `why` and
  `snooze` ([#246](https://github.com/ch8er/kelpie/pull/246)).
- `kelpie doctor` crashed with a traceback when an adapter's `token_env` named a variable
  that was not set (the GitHub Issues and Linear quickstarts before the token is exported);
  it now fails its `config loads` check and names the variable
  ([#249](https://github.com/ch8er/kelpie/issues/249)).
- First-run rough edges ([#267](https://github.com/ch8er/kelpie/issues/267)): `kelpie
  status` writes durations as the config does (`30s`, `30m`, `7d`) instead of
  `7 days, 0:00:00`, and so do the startup warnings that compare durations; `kelpie serve` warns that checkpoints makes no WIP commits without
  `[connectors.github.pulls]` only when a configured adapter makes WIP commits (or on a
  hub), not on every farm; `kelpie doctor` builds the adapters from the environment it
  checks. The package lists Python 3.13, which installs and runs.
- A `kelpie serve` refused for its config (a missing token, header or admin key variable, an
  alert channel's token or buttons, a `[traces] token_file` others could have written, a
  `[hub]` TLS cert or key that does not load) no longer leaves an empty `kelpie.db` behind:
  these are checked before any database is opened. A port already in use (the hub's or the
  main listener's) is still found only when it is bound, after the database is opened
  ([#244](https://github.com/ch8er/kelpie/pull/244)).
- A connector step that failed for one tick (human tasks, GitHub conditions) dropped the
  conditions it had attached and checked, so waits looked unchecked and blocks bare until
  the next good tick: one hiccup could alert, then resolve. What the step last found unmet
  or unknown for waits and blocks is now carried forward (for up to 6 hours) with its
  original check time and goes stale on its own after `max_condition_age`; what it found
  met, and anything for a ready task, is not carried, so a failed step never wakes,
  unblocks or parks anything. A block whose connector keeps failing stays linked to its
  stale condition: rule 4 alerts on stale evidence instead of an unlinked block. New:
  `kelpie_connector_failing_seconds` and the Prometheus alert `KelpieConnectorFailing`
  ([#242](https://github.com/ch8er/kelpie/issues/242)).
- The cost of chaos counts a stuck task-hour once: rule 13 listing a task as waiting on a
  person while rule 4 holds a stale block on it counted the same hours twice, inflating the
  headline (incident 20). Each hour counts under one cause, in a fixed order: a person's
  inbox first, then a stale block, then the rest. The other causes' overlapping hours are
  listed as "also seen", not added; before/after checks count the same way
  ([#228](https://github.com/ch8er/kelpie/issues/228)).
- Kelpie's blocks, parks and wakes on a Hermes board showed up as written by the profile
  Kelpie runs under (`default`): they are signed `kelpie` now, like its comments, and the name
  is `[adapters.hermes] author` ([#189](https://github.com/ch8er/kelpie/pull/189)).
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
- `kelpie doctor` refuses a `--prometheus` / `$KELPIE_PROMETHEUS_URL` with credentials, a
  query or a fragment instead of printing it whole, and no longer follows a redirect.
  Reading the store read-only (`kelpie status`, `why`, `snooze`) escapes the path: a `#`,
  `?` or `%` in it used to open another file, read-write. The OpenClaw hook's peer key is
  read only from a regular file, so a directory or FIFO there no longer fails every tick.
  The simulator's fake GitHub tokens no longer look like real ones, and CI's actions are
  pinned by commit like the release's ([#275](https://github.com/ch8er/kelpie/issues/275)).

### Internal

- The sdist carries the source, docs, deploy files and scripts, not the tests, their
  fixtures, CI or agent settings; the release build checks that neither file carries tests,
  a database, a config or `.env` ([#249](https://github.com/ch8er/kelpie/issues/249)).
- Alert wording is now structured fields passed through the `Alerter` port ([#135](https://github.com/ch8er/kelpie/pull/135)).
- One hold-labels helper shared by the app and the rules ([#99](https://github.com/ch8er/kelpie/pull/99)).
- A simulated Hermes farm with end-to-end scenarios against `kelpie serve`, including act
  mode and the daily report ([#62](https://github.com/ch8er/kelpie/pull/62), [#73](https://github.com/ch8er/kelpie/pull/73), [#74](https://github.com/ch8er/kelpie/pull/74), [#81](https://github.com/ch8er/kelpie/pull/81)).
- A code health and security audit plan was written before the first release ([#92](https://github.com/ch8er/kelpie/pull/92)).

[Unreleased]: https://github.com/ch8er/kelpie/compare/v0.1.0a3...HEAD
[0.1.0a3]: https://github.com/ch8er/kelpie/releases/tag/v0.1.0a3
[0.1.0a2]: https://github.com/ch8er/kelpie/releases/tag/v0.1.0a2
[0.1.0a1]: https://github.com/ch8er/kelpie/releases/tag/v0.1.0a1
