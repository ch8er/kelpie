# Multi-host: `kelpie agent` on agent machines, central `kelpie serve`

Design for the P0 release blocker in [plan.md](plan.md) ("P0 before the first release —
multi-host") and its [roadmap](roadmap.md) entry. Setup guides per case follow in
[install.md](install.md) once the code lands.

## Problem

The Hermes and Claude Code adapters read local files (`kanban.db`, hook spools, Claude Code
task lists, worktrees) and act through local means (the `hermes` CLI, stop markers, `git`).
`kelpie serve` therefore has to run on the agents' machine, cannot watch a farm spread over
several machines, and dies together with the machine it watches.

## Shape

```
 agent host A                         central host                     agent host B
 ┌──────────────────────┐            ┌─────────────────────────────┐   ┌──────────────┐
 │ kelpie agent  "a"    │  HTTPS     │ kelpie serve                │   │ kelpie agent │
 │  adapters: hermes,   │──────────▶ │  hub (:9465)                │ ◀─│   "b"        │
 │  claude-code         │  agent →   │  merge → rules → engine     │   └──────────────┘
 │  local actions       │  central   │  ledger, report, alerts,    │
 │  OTLP relay (lo)     │  only      │  connectors, /metrics :9464 │
 └──────────────────────┘            └─────────────────────────────┘
```

- **`kelpie agent`** runs on every machine with agents. It builds the platform adapters from
  the usual `[adapters.*]` sections, takes their snapshots, sends them to the central server
  and carries out the actions the server routes to it. It has no rules, no ledger, no
  alerts, no connectors and no report.
- **`kelpie serve`** stays what it is: rules, ledger, alert states, connectors, budgets,
  traces, report, `/metrics`. With a `[hub]` section it also listens for agents, merges
  their snapshots into the one it evaluates, and routes actions back.
- **Single-host mode is the default and unchanged.** Without `[hub]` nothing new starts and
  adapters run in-process as today.
- **No mixed mode.** With `[hub]`, `kelpie serve` refuses in-process `[adapters.*]`: a
  machine that runs agents runs `kelpie agent`, also the server's own machine (it connects
  over loopback). Every task in the merged snapshot then belongs to exactly one agent, and
  no raw platform id can be mistaken for a namespaced one.

## Where the code lives

`kelpie.remote` (outside `core` and `rules`, import-linted like the other integrations):
`protocol` (wire format, validation, namespacing; builds on core only), `config`
(`[agent]`, `[hub]`), `agent` (the agent loop; gets its adapters from the caller), `hub`
(the server side: listener, agent registry, remote executors, merge). The engine gains one
method, `add_executor`, called from the merge in the control-loop thread when an agent
reports an adapter for the first time.

## Transport

Agents connect to the server; the server never connects to an agent.

- It works when agent hosts sit behind NAT (a laptop) and the server has a stable address.
- **No agent host listens on the network.** The side that can run the `hermes` CLI and
  commit to worktrees opens no port. (The OTLP relay binds loopback only.)

HTTP/1.1 with JSON bodies on a dedicated listener (`[hub] listen`, default port 9465), so
TLS, size limits and authentication for agents do not change `/metrics`. Three calls, all
`POST`, all authenticated by the agent's token:

| Call | Body (agent → server) | Reply |
| --- | --- | --- |
| `/v1/agent/report` | protocol version, agent version, its `interval`, `sent_at`; per adapter: name, the action kinds it carries out, and its snapshot or the error it failed with | `{}` |
| `/v1/agent/actions` | results of actions carried out since the last call | actions for this agent; held open up to 20 s until one is queued (long poll) |
| `/v1/traces` | OTLP/HTTP, relayed by the agent from local exporters | as the OTLP endpoint |

`report` runs every `[agent] interval` (default 15 s). `actions` is a loop: the agent
waits on it, executes what comes back one at a time, and calls again with the results, so
an action reaches the agent within a second of the engine routing it.

**Traces.** The agent's relay listens on `127.0.0.1` (default port 4318) and forwards
OTLP exports to the hub's `/v1/traces` under the agent's token, so exporters on agent hosts
point at localhost as in single-host mode. The hub namespaces the spans (below) before
they reach the span store. With `[hub]`, `/v1/traces` on the main port is turned off: a
span whose ids are not namespaced by an agent token could be forged into any host's
namespace.

### TLS

- The hub serves HTTPS from `[hub] tls_cert` / `tls_key`, TLS 1.2 at least. The agent
  verifies the certificate and the host name against the system trust store or
  `[agent] ca_file` (a private CA or the self-signed certificate itself). There is no
  option to skip verification.
- The TLS handshake runs in the connection's own thread under the socket timeout, so a
  client that stalls it cannot hold up `accept()`.
- Plain HTTP is for private networks only and must be asked for on both sides:
  - `[hub] allow_plain_http = true`, and the listen address must be a loopback or private
    IP literal (RFC 1918, RFC 6598 `100.64/10` as used by Tailscale — also carrier-grade
    NAT, so only on a network you control —, IPv6 ULA), never `0.0.0.0`;
  - `[agent] allow_plain_http = true`, and the server name must resolve to such addresses
    only (checked at startup).
  Both sides log a warning at startup: the token crosses the network in clear text.

### Authentication

- **One token per agent**, at least 32 printable characters, read from an environment
  variable on both sides (`[[hub.agents]] token_env`, `[agent] token_env`), never from the
  config file. `Authorization: Bearer <token>`, compared in constant time against every
  configured token; two agents may not share a token.
- **The token is the identity.** The body carries no agent name; everything an agent sends
  lands in the namespace of the token that matched.
- The token is checked from the headers before a request slot is taken or the body read.
- What one token is worth: feeding that host's board and taking that host's actions. A
  process running as the agent's user can read the token, but that user can already edit
  the host's board and run its CLI, so it gains nothing; on a shared machine run the agent
  as its own user.
- Revoking an agent: remove its `[[hub.agents]]` entry or rotate the variable and restart.

### Limits

- **Strangers:** connections that have not presented a valid token are capped at
  `[hub] max_connections` (default 64; when full, the oldest is cut to make room, since an
  agent sends its token at once) and 16 per address (per /64 for IPv6), and get 3 seconds
  in all, TLS handshake and headers included. A connection gives its slot back once its
  token checks out.
- **Per agent:** one report, two polls and two trace exports in flight; more get 409. A
  new poll (its body read) replaces a held one, whose connection may be dead; if that
  keeps happening, or reports come from two agent processes at once, the hub warns (two
  processes share a token, or a stolen one is in use). Poll bodies are capped at 2 MiB.
- **Bodies:** `Content-Length` required, at most `[hub] max_body_bytes` (default 16 MiB)
  before and after decompression. `Content-Encoding: gzip` is inflated with that bound, so
  a small compressed bomb is refused. 411/413/415 otherwise; one request per connection;
  socket timeout and a whole-body deadline as on the OTLP endpoint (slowloris).
- **Counts:** per adapter at most 20,000 tasks, 50,000 sessions, 100,000 spans, 20,000
  conditions and dependencies; ids of at most 256 printable characters; JSON nesting is
  bounded by the decoder (a too-deep body is a 400).
- **Ordering:** each agent process numbers its reports (a random boot id and a counter); a
  report not newer than the last one of the same process is refused, so an old retry never
  overwrites a newer board. The agent's clock plays no part in ordering.
- **Values:** every field is type-checked; times must fall in 2000–2200 and durations in
  0–3650 days; text loses control characters and lone surrogates; ids with them are refused;
  duplicate ids within one board are refused.
- **The agent** caps what it reads back (1 MiB per reply) and refuses a reply that is not
  the expected JSON.
- Agent and server must run the same release: unknown fields are refused (strict decoding,
  as for fixtures) with a 400 naming the field, and the version is in every report.

## What an agent may be asked to do

The agent executes **only the action kinds its adapters implement**, through the same
`PlatformExecutor` that runs them in-process today. There is no command, path or script in
the protocol.

- An adapter declares the kinds it carries out in `actions` (as action connectors do):
  Hermes `stop`, `wait`, `requeue`/`wake` (of a task Kelpie parked), `comment`; Claude Code
  `stop`. An adapter without the attribute falls back to the capability protocols it
  implements. `[agent] actions = [...]` can narrow the set further. The agent reports the
  set; the server routes nothing else to that adapter (the action becomes "not executed"
  and a human is told, as for `UnsupportedAction` today), and the agent refuses anything
  else anyway. This replaces the startup refusal of act-mode rules Hermes cannot carry out:
  an undeclared kind never reaches the adapter, so it cannot loop as a failure.
- An action on the wire is `{id, adapter, kind, rule, reason, task_id, params, ttl}`.
  `params` are checked per kind (`stop`: `session_id`; `wip_commit`: `branch` — a git ref
  name, no leading `-` — and `draft_pr`; `reassign`: `owner_id`; `wait`: at most 32
  `conditions` of `{id, kind, params}` with scalar params); other keys are dropped; the
  reason is cut at 4 KiB and control characters are removed.
- The adapter's own guards still apply: an `UnsupportedAction` comes back as "not
  executed".

## Snapshot merge and host-namespaced ids

Each agent sends one snapshot per adapter (`tasks`, `owners`, `dependencies`,
`conditions`, `sessions`, `spans`; anything else is refused — builds, PRs, providers,
resources and budgets come from the server's connectors). The server rewrites **every id**
into the agent's namespace, `<agent>:<id>`, before merging. Agent names are
`[a-z0-9-]`, so a namespaced id has exactly one owner, whatever the raw id holds.

| Field | Namespaced |
| --- | --- |
| `Task` | `id`, `owner_id`, `assignee`, `subtasks`, `repo` |
| `Owner` | `id` — humans too: a person on two boards is two owners, so no host can overwrite another host's owner |
| `Dependency` | `task_id`, `depends_on` |
| `Condition` | `id`, `task_id`, the `task` param of `task_status` conditions; `Block.condition_id` |
| `Session` | `id`, `task_id`, `owner_id` |
| `Span` | `trace_id`, `span_id`, `parent_id`, `task_id`, `session_id`, the `tool_call_id` attribute — so links are only inherited, and hook and OTLP spans only merged, within one host |

`Task.source` and `Session.source` become `<agent>/<adapter>`, the name of the executor
that carries out actions on them; routing uses `source`, never the id. `Task.host` becomes
the agent name (name agents like their node_exporter instances so resource readings and
tasks line up).

`Task.repo` is a local path, so namespacing it means rule 12 does not see a conflict
between tasks of the same repository on two hosts. That is a known gap for the first
release, stated in the docs.

**Pull requests** link to tasks by task code in the title or branch. A task's id without
its `<agent>:` prefix is also a code, but only when exactly one task in the merged
snapshot has that local id; when several hosts have it, only the branch links.

**Config that names ids** (budget `match`, orphan `ignore_tasks`, ping-pong
`escalate_to`, provider owners) compares against namespaced ids; patterns take a
`laptop:` or `*:` prefix.

**Clocks.** Agent clocks are used as is, so rules compare an agent's times with the
server's `now`. The hub measures each report's offset (`sent_at` against its own clock): a
report more than `[hub] max_clock_skew` (default 2 minutes) off is not merged — the agent
counts as unobserved and the hub alerts once — because a clock minutes behind makes every
task on that host look older and could requeue or stop live work.

## Action routing

- An action goes, as today, to the executor named by its task's or session's `source`: a
  **remote executor** `<agent>/<adapter>`, registered in the engine when the agent first
  reports that adapter.
- The remote executor strips the agent's own prefix from `task_id`, `params.session_id`,
  `params.owner_id` and the wait conditions. An id that is not in that agent's namespace
  (a reassign to another host's owner, a wait on another host's task) is not executed and
  a human is told; cross-host references have no local meaning on either board.
- With `[hub]` the engine's last-resort routing ("the only platform adapter") is off.

**Not stalling the loop.** The engine runs actions one after another in the control-loop
thread, so the remote executor bounds the wait:

- An action waits up to `[hub] action_timeout` (default 30 s) for its result.
- Each agent has a per-tick budget of `action_timeout` in total. Once an agent used it (a
  slow answer, a timeout), its further actions in that tick fail fast as "agent busy" and
  the ledger retries them later.
- At most 16 actions per agent per tick; more fail fast the same way.

So one slow or hostile agent costs the loop at most `action_timeout` per tick, and N agents
at most N times that.

**At most once.** An action is queued with an id and a `ttl` (the time left until its
timeout). The agent does not start an action whose `ttl` has run out since it received
it, and remembers the result of every id for an hour. At its timeout the hub withdraws the
action if it was not yet delivered: then it was not carried out, and the recorded failure
is true. If it was delivered and the result is late:

- the action stays pending under its id for an hour; the same action (same adapter, kind,
  target and params) emitted again in that hour reuses the id, so the agent answers with
  the remembered result instead of running it again, and the late result answers that
  retry in the ledger;
- a late `done` that no retry picks up within the engine's `retry_after` is reported to a
  human by the hub ("carried out after all: …"), since the ledger already holds a failure.

A server restart forgets pending ids, and an agent restart forgets remembered results:
then an action delivered right before the restart may run again. Hermes `stop`, `wait`
and `requeue` are idempotent there; `comment` may repeat once.

## Heartbeats and silent agents

- **Fresh** means: the agent's last accepted report is younger than twice its `interval`
  plus 10 s and was not refused for clock skew. Only fresh reports are merged. A poll is
  not a heartbeat for the board: an agent whose report loop hangs while its poll loop runs
  is not fresh.
- **Silent** means: no fresh report for `[hub] silent_after` (default 90 s). On the
  transition the hub alerts once (warning: "agent a silent since …; its tasks are not
  evaluated"), and sends a short "resolved" when it is back. `/metrics`:
  `kelpie_agent_up{agent}`, `kelpie_agent_last_report_timestamp_seconds{agent}`.
- **Its tasks are not treated as abandoned.** A board that is not fresh is left out of the
  merge rather than kept stale (a stale board with an advancing `now` is exactly what
  rules read as an orphaned task or a dead session). An adapter that fails on a live agent
  is left out the same way.
- **The snapshot says it is partial.** `Snapshot.unobserved` lists the agents (and
  `<agent>/<adapter>` parts) not merged this tick, including agents that have not reported
  since the server started. While it is not empty the engine resolves no alert states:
  rules that stop reporting a state may only be blind to it. States still reported are
  escalated as usual; when the host is back its states are reported again and escalate by
  their real age (the condition lasted while the host was away).
- Farm-wide rules (idle farm, role bottlenecks) see a farm without the silent host's tasks
  and owners. Both drop together, so capacity and demand stay consistent; the silence
  alert says the farm is partial.
- Actions routed to a silent agent fail fast with "agent a is silent" (the ledger retries
  them after it is back).

## Failure modes

| Failure | What happens |
| --- | --- |
| Agent host down or network split | Agent silent → one alert; its board left out, alert states frozen; actions to it fail fast and are retried by the ledger. |
| Server down | Agents retry with backoff (1 s → 60 s); nothing acts on agent hosts without the server, which is the point of a central watchdog. Platforms keep working. |
| Slow agent | Its actions time out within the per-tick budget; at most `action_timeout` of loop time per tick; the late result answers the retry, or a human is told. |
| Report loop hangs, poll loop alive | No fresh report → its board is left out → silent after `silent_after`. |
| Adapter fails on an agent | That part is left out and listed as unobserved; the error is logged on the server and counted in `kelpie_adapter_errors_total{adapter="a/hermes"}`. |
| Agent clock off by more than `max_clock_skew` | Its reports are not merged; one alert naming the offset. |
| Wrong or missing token | 401, logged with the peer address; nothing is read. |
| Token used by two processes | Polls replace each other and reports alternate; a warning in the log. |
| Oversized, malformed or out-of-order body | 400/411/413/415; the agent logs the reply and keeps its loop going. |
| Version mismatch | 400 naming the unknown field; the agent logs it. |
| Server or agent restart | Agents re-send everything on the next report; see "at most once" for pending actions. |

## Moving a single-host setup to multi-host

Ids change from `t_1a` to `laptop:t_1a`, so the server sees a new farm:

- open alert states resolve and new ones open once (one burst of messages; start the
  switch with `[engine] dry_run = true` for a tick to skip it);
- ledger fingerprints change, so a `once` action (a resume note) may be sent once more;
- span attribution splits at the switch in the daily report for that day;
- config patterns naming tasks or owners need the `laptop:` (or `*:`) prefix;
- the Hermes park-signing key (`hermes-wait.key`, next to the ledger) moves to the agent
  host with the Hermes adapter: copy it there, or parks made before the switch are not
  recognised as Kelpie's.

## Out of scope for the first release

- Mixed mode (in-process adapters next to agents), server discovery, the server pulling
  from agents, mTLS.
- Rule 12 across hosts (see `Task.repo` above).
- `kelpie why` and `kelpie doctor` across hosts: run them on the agent host with the
  agent's config; they read the local adapters as in single-host mode.
- Relaying hook spools or Prometheus: spools are read by the agent's adapters, node_exporter
  is scraped by Prometheus as before.

## Delivery

1. This document.
2. `kelpie agent` and the transport: protocol, config, the agent loop, the hub listener
   with auth and limits, adapters declaring their action kinds.
3. Central merge and routing: namespacing, remote executors with budgets, `unobserved`,
   silence and skew alerts, the OTLP relay, metrics, the no-mixed-mode check.
4. End-to-end: two simulated hosts in `tests/sim`, one farm; an agent going silent; an
   action routed to the right host.
5. Setup guides: laptop only, laptop + cloud VM, two VMs.
