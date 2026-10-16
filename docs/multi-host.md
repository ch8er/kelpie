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
 │  claude_code         │  agent →   │  merge → rules → engine     │   └──────────────┘
 │  local actions       │  central   │  ledger, report, alerts,    │
 │  OTLP relay (opt.)   │  only      │  connectors, /metrics :9464 │
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
  adapters run in-process as today. Both can be mixed: `kelpie serve` on one agent machine
  with local adapters, plus agents elsewhere.

## Transport

Agents connect to the server; the server never connects to an agent.

- It works when agent hosts sit behind NAT (a laptop) and the server has a stable address.
- **No agent host listens on the network.** The side that can run the `hermes` CLI and
  commit to worktrees opens no port. (The optional OTLP relay binds loopback only.)
- OTLP already flows agent host → server, so the firewall story stays one-directional.

HTTP/1.1 with JSON bodies on a dedicated listener (`[hub] listen`, default port 9465), so
TLS, size limits and authentication for agents do not change `/metrics` or the existing
OTLP port. Two calls:

| Call | Body (agent → server) | Reply |
| --- | --- | --- |
| `POST /v1/agent/report` | protocol version, agent version, `sent_at`, per adapter: name, the action kinds it carries out, and its snapshot or the error it failed with | `{"interval": seconds}` |
| `POST /v1/agent/actions` | results of actions carried out since the last call | actions for this agent; held open up to 20 s until one is queued (long poll) |

`report` runs every `[agent] interval` (default 15 s). `actions` is a loop: the agent
waits on it, executes what comes back one at a time, and calls again with the results, so
an action reaches the agent within a second of the engine routing it.

The optional **OTLP relay** listens on `127.0.0.1` on the agent host and forwards trace
exports to the hub's `/v1/traces` under the agent's token. Exporters on that host keep
pointing at localhost, as in single-host mode.

### TLS

- The hub serves HTTPS from `[hub] tls_cert` / `tls_key`. The agent verifies the server
  certificate against the system trust store or `[agent] ca_file` (a private CA or the
  self-signed certificate itself). There is no option to skip verification.
- Plain HTTP is only for private networks and must be asked for twice:
  - `[hub] allow_plain_http = true`, and the listen address must be loopback or private
    (RFC 1918, 100.64/10 as used by Tailscale, IPv6 ULA);
  - `[agent] allow_plain_http = true`, and the server name must resolve to such addresses
    only.
  Both sides log a warning at startup: the token crosses the network in clear text.

### Authentication

- **One token per agent**, at least 32 characters, read from an environment variable on
  both sides (`[[hub.agents]] token_env`, `[agent] token_env`), never from the config file.
  `Authorization: Bearer <token>`, compared in constant time.
- **The token is the identity.** The server takes the agent's name from the token that
  matched, never from the body; a body naming another agent is refused. One leaked token
  lets an attacker feed one host's snapshot and read that host's actions, nothing more.
- Revoking an agent: remove its `[[hub.agents]]` entry or rotate the variable and restart.

### Limits

- Request bodies: `Content-Length` required, at most `[hub] max_body_bytes` (default
  16 MiB) after decompression. `Content-Encoding: gzip` is accepted and inflated with the
  same bound, so a small compressed bomb is refused. 411/413 otherwise.
- Socket timeout and whole-body deadline as on the OTLP endpoint (slowloris), and at most
  `[hub] max_concurrent_requests` (default 32) in flight.
- The agent caps what it reads back (1 MiB per reply) and refuses a reply that is not the
  expected JSON.
- An unknown field anywhere in a snapshot is refused (strict decoding, as for fixtures);
  agent and server must run the same release. The version is in every report and a
  mismatch is logged.

## What an agent may be asked to do

The agent executes **only the action kinds its adapters implement**, through the same
`PlatformExecutor` that runs them in-process today. There is no command, path or script
in the protocol.

- Per adapter the agent derives the kinds from its capabilities (`requeue`, `wake`,
  `unblock`, `comment`, `wip_commit` from `PlatformAdapter`; `stop`, `wait`, `reassign`,
  `close` only with `SupportsStop`, `SupportsWait`, `SupportsReassign`, `SupportsClose`),
  optionally narrowed by `[agent] actions = [...]`. It reports that set; the server routes
  nothing else to it, and the agent refuses anything else anyway.
- An action on the wire is `{id, adapter, kind, rule, reason, task_id, params}`. `params`
  are checked per kind (`stop`: `session_id`; `wip_commit`: `branch`, `draft_pr`;
  `reassign`: `owner_id`; `wait`: `conditions` as `{id, kind, params}`); other keys are
  dropped, sizes are capped (reason 4 KiB, ids 256 characters).
- The adapter's own guards still apply: an `UnsupportedAction` comes back as "not
  executed", and the server tells a human, as in single-host mode.

## Snapshot merge and host-namespaced ids

Each agent sends one snapshot per adapter (`tasks`, `owners`, `dependencies`,
`conditions`, `sessions`, `spans`; anything else is refused — builds, PRs, providers,
resources and budgets come from the server's connectors). The server rewrites every id
into the agent's namespace before merging:

- `id` → `<agent>:<id>` for task ids (`Task.id`, `subtasks`, `Dependency`, condition
  `task_id` and the `task_id` param of `task_status` conditions), session ids and agent
  owner ids (`owner_id`, `assignee`, `Owner.id`, `Session.owner_id`). Human owners
  (`OwnerKind.HUMAN`) keep their id: a person is the same person on every board.
- `Task.source` and `Session.source` become `<agent>/<adapter>`, the name of the executor
  that carries out actions on them. `Task.host` becomes the agent name.
- Agent names are `[a-z0-9-]`, so neither `:` nor `/` is ambiguous.

Ids that reach the server from elsewhere are mapped the same way:

- **Spans over OTLP** sent with an agent's token (directly or through the relay) get that
  agent's namespace on `task_id` and `session_id`; spans with the shared `[traces]` token
  keep raw ids (single-host and mixed mode).
- **Pull requests** link to tasks by task code in the title or branch: with `Task.host`
  set, the id without the `<agent>:` prefix is also a code, so `hermes/t_1a` still links
  to `a:t_1a`.

Each tick uses the latest report of every agent that is not silent. Agent clocks are
used as is (status times are absolute); a report whose `sent_at` differs from the
server's clock by more than a minute logs a skew warning, because rules compare those
times with the server's `now`.

## Action routing

- An action is routed as today: to the executor named by the task's or session's `source`.
  For a remote task that is a **remote executor** `<agent>/<adapter>` registered in the
  engine next to local adapters and connectors.
- The remote executor strips the namespace from `task_id`, `params.session_id`,
  `params.owner_id` and wait conditions (an id from another agent's namespace is refused:
  not executed), queues the action for the agent and waits up to `[hub] action_timeout`
  (default 20 s) for its result: `done` (with the adapter's detail), `nothing_to_do`,
  `not_executed` (the adapter refused) or `failed` (with the exception's `public_reason`
  only). Those map onto the engine's existing outcomes, so ledger, alerts and the report
  need no change.
- The engine's last-resort routing ("the only platform adapter") is off once remote
  executors exist, so an action on a host Kelpie cannot see never lands on a local
  adapter by accident.

**At most once per action.** A queued action carries an id derived per attempt. If the
result does not arrive in time, the engine records a failure and retries after
`retry_after`; the retry reuses the same id while the first attempt is unresolved, and the
agent remembers the results of recent ids, so a slow confirmation is not carried out twice.
A late result is kept and answers the retry. An agent restart forgets its ids; the actions
Kelpie sends are idempotent on the platform except `comment`, which may then repeat once.

## Heartbeats and silent agents

- Every authenticated call counts as a heartbeat; the long-poll keeps one open all the
  time. An agent is **silent** when nothing arrived for `[hub] silent_after` (default
  90 s, at least twice the agent interval).
- On the transition to silent the server alerts once (warning: "agent a silent since
  …; its N tasks are not evaluated"), and sends a short "resolved" when it reports again.
  `/metrics`: `kelpie_agent_up{agent}`, `kelpie_agent_last_seen_timestamp_seconds{agent}`.
- **Its tasks are not treated as abandoned.** A silent agent's snapshot is left out of the
  merge rather than kept stale (a stale board with an advancing `now` is exactly what
  rules read as an orphaned task or a dead session). The ids of the tasks it last
  reported go into `Snapshot.unobserved_tasks`, and the engine neither resolves nor
  escalates open alert states on those tasks while they are unobserved: they continue
  where they were when the agent reports again.
- An adapter that fails on a live agent is treated like a failing in-process adapter
  today, but its last task ids are also kept unobserved.
- Actions routed to a silent agent fail fast with "agent a is silent" (a human is told,
  the ledger retries later). After an action times out, further actions to that agent in
  the same tick fail fast until it polls again, so one hung host cannot stall the loop.

## Failure modes

| Failure | What happens |
| --- | --- |
| Agent host down or network split | Agent silent → one alert; its tasks unobserved, their states frozen; actions to it fail fast and are retried by the ledger after it is back. |
| Server down | Agents keep retrying with backoff (1 s → 60 s); nothing runs on agent hosts without the server, which is the point of a central watchdog. Platforms keep working. |
| Slow agent (action runs past `action_timeout`) | Failure recorded, human told once; the retry reuses the id, the late result answers it; never executed twice. |
| Adapter fails on an agent | That adapter's part is left out for the tick, its tasks unobserved; the error string is logged on the server and counted in `kelpie_adapter_errors_total{adapter="a/hermes"}`. |
| Wrong or missing token | 401, logged with the peer address; nothing is merged. |
| Oversized or malformed body | 411/413/400; the agent logs the reply and keeps its loop going. |
| Clock skew | Logged when over a minute; times are not corrected. |
| Version mismatch | Logged; an unknown field fails that report with 400 naming the field. |
| Agent restarted | Re-sends everything on the next report; recent action ids are forgotten (see above). |

## Out of scope for the first release

- Agents discovering the server, or the server pulling from agents.
- mTLS (the per-agent token over verified TLS covers the threat model above).
- `kelpie why` and `kelpie doctor` across hosts: run them on the agent host with the
  agent's config; they read the local adapters as in single-host mode.
- Relaying hook spools or Prometheus: spools are read by the agent's adapters, node_exporter
  is scraped by Prometheus as before.

## Delivery

1. This document.
2. `kelpie agent` and the transport: `kelpie.remote.protocol` (wire format, validation,
   namespacing; builds on core only), the agent loop, the hub endpoints with auth and
   limits.
3. Central merge and routing: remote executors in the engine, `unobserved_tasks`, silent
   alerts, OTLP namespacing and relay, metrics.
4. End-to-end: two simulated hosts in `tests/sim`, one farm; an agent going silent; an
   action routed to the right host.
5. Setup guides: laptop only, laptop + cloud VM, two VMs.
