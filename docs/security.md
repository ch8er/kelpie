# Security

How Kelpie handles the secrets it holds and the endpoints it opens. How to report a
vulnerability: [SECURITY.md](../SECURITY.md). What Kelpie stores from the farm:
[privacy.md](privacy.md).

The whole-codebase security audit before the first public release
([#275](https://github.com/ch8er/kelpie/issues/275)) fixed its high findings; what it left
open and what to do meanwhile is under [Known residual risks](#known-residual-risks).

## Secrets Kelpie holds

Tokens come from environment variables whose names the config holds (`token_env`,
`webhook_env`, `headers_env`, `admin_key_env` and the like) or, for the traces token, a
token file; the config holds names, not values. Where Kelpie says which secret it uses
(logs, `kelpie status`, `kelpie doctor`, error messages) it names the variable or the file.
The audit checks that no value reaches logs, `/metrics`, errors or reports.

Secrets never go to a proxy in clear: plain-http requests that carry one (GitHub and Linear
on loopback, `[[traces.forward]]` with `headers_env`, `kelpie agent` on a private network)
are sent directly, never through `http_proxy`; https requests may use `https_proxy` through
a CONNECT tunnel.

## Traces token

The OTLP ingest token (`[traces] token_env`, `KELPIE_TRACES_TOKEN`) is the one secret the
agents' side needs too: every exporter sends it. That makes it the one most likely to end
up copied into many files, or printed where an agent can see it. Keep it rotatable:

- **One copy.** Set `[traces] token_file` to an env file (mode 600) that holds
  `KELPIE_TRACES_TOKEN=<token>`, read by `kelpie serve`, and on the agents' side read the
  token from there (into the exporter's header variable) rather than writing it into each
  profile's config, where that is safe
  ([quickstart-hermes.md](quickstart-hermes.md#4-otlp-traces-optional)).
- **Rotation without dropped spans.** `kelpie traces rotate-token` writes a new token to
  the file and keeps the old one valid for `[traces] token_grace` (default 2h), so sessions
  that started with it keep reporting while new ones start with the new token. The old
  token is kept only as a SHA-256 hash with its expiry, never in clear. `kelpie serve` reads
  the file again within a second, without a restart. The command prints no token.
- **Revoking at once.** `kelpie traces rotate-token --grace 0s` writes a new token and
  accepts no previous one, including tokens still in an earlier grace period. Deleting or
  breaking the file revokes nothing: `kelpie serve` keeps the last tokens it read (and logs
  that it cannot read the file) until it restarts.
- **Who may write it.** The command runs only as the user who owns the file's folder
  (the user `kelpie serve` runs as; root only in a folder root owns), refuses a symlink,
  another user's file and a folder others may write to, and writes mode 600, replaced
  atomically under a lock, after checking the file was not swapped since it was read. So
  it cannot be led to write a file someone else chose. `kelpie serve` trusts only such a
  file: a regular file of its own user (or root), not writable by others, in a folder not
  writable by others; whoever could write it could choose the token.
  The folders above it count too: every folder of its real path must belong to that user
  or root and be closed to others' writes (a sticky folder such as `/tmp` excepted), so
  nobody else can swap a folder on the way for a link elsewhere; a symlink on the way is
  followed only when that user or root owns it (another user's link in `/tmp` is not).
  `kelpie_traces_token_file_ok` on `/metrics` is 0 when serve keeps old tokens because it
  cannot read or trust the file: a rotation or revocation has not taken effect. A setup
  where root owns the file and serve reads it through its group (640) cannot be rotated:
  the rotation writes mode 600. Give the file to the user serve runs as.
- **Other lines.** Lines of the file other than the token's are kept as they are (minus
  commented-out copies of the token lines): keep nothing else secret there.
- **Who still uses the old one.** `kelpie_traces_grace_requests_total` on `/metrics` counts
  requests that came with a previous token; `kelpie_traces_grace_tokens` how many are still
  accepted.
- **Stray copies.** `kelpie doctor` warns when the token is in a backup file (`*.bak*`,
  `*~`, `*.orig`, `*.old`, `*.save`, `*.tmp`) or a file others can read, under the token
  file's folder, the Hermes home or `~/.claude` (three levels deep, files up to 64 MiB,
  256 MiB in all), and lists the files that hold a copy, which a rotation must update.
  Older tokens are known only by their hash, so it also warns about backups with a
  `<token_env>=` line. It names files, never the token.
- **The agents' side.** Setting `OTEL_EXPORTER_OTLP_TRACES_HEADERS` once is convenient but
  goes to every OTLP exporter without headers of its own in that process and is inherited
  by the agents it starts; see [quickstart-hermes.md](quickstart-hermes.md#4-otlp-traces-optional)
  for when to prefer per-backend headers.

When the token leaked (printed into a tool output, a log, a chat), rotate it, restart the
exporters so new sessions use the new token, delete the backups doctor lists, and watch
`kelpie_traces_grace_requests_total` stop growing before the grace period ends. If the
leaked token must stop working now, rotate with `--grace 0s` (running sessions then get
401 until they restart).

The comparison of a presented token takes constant time, for the current token and for the
hashes of previous ones.


## Known residual risks

What 0.1.0a1 ships knowing, from the audit's open medium findings. Each has a follow-up;
this section shrinks as they land.

### Rule 10 in act trusts every holder of the traces token (M1)

`loop_breaker` (rule 10) stops a run that repeats identical tool calls, hits an error
streak or exceeds a hard budget (`[[budgets]] hard`), and it reads all of that from spans.
Kelpie cannot tell who wrote a span:

- **OTLP spans.** A span's session id is whatever the sender writes in it, and the ingest has
  one token that every agent's exporter holds. Whoever has the token can send a few identical
  tool spans tagged with another agent's session id; without a token (loopback, or
  `allow_unauthenticated`), any process that reaches the port can.
- **Hermes spool lines.** A line names its `run_id`, and every agent on the host can write
  to the spool, so a line can name another agent's run.

With rule 10 in `act`, either makes Kelpie stop the other agent's run, and on Hermes end
its worker processes; forged spend can do the same through a hard budget (with
`stop_on_shared_budgets`, through a shared one). The stop looks like Kelpie's own decision.
In `observe` and `suggest` no run is stopped. Forged spans can also skew alerts and the
daily report's figures; rule 10 is the only rule that stops a run.

**Who is affected.** Farms that run rule 10 in `act` and whose agents are untrusted or
sandboxed (in a container, as another user, on another machine reporting straight to
`kelpie serve`) while sharing the one traces token. An unsandboxed agent running as the same
user as its peers gains nothing new: it could already signal their processes or block their
tasks. The multi-host hub is not affected: it namespaces every span and run by the agent
whose token sent it, so a `kelpie agent` cannot reach another host's runs.

**What to do meanwhile.**

- On such farms keep `loop_breaker` in `observe` or `suggest`
  ([act-checklist.md](act-checklist.md)); a person decides each stop.
- Or give agents that do not trust each other separate farms: their own `kelpie serve` (or
  their own `kelpie agent` behind a hub) and their own traces token.
- Always set a traces token; keep the ingest on loopback or a private address, and do not
  hand the token to processes that are not agents' exporters.

**The fix.** Per-agent ingest tokens, each allowed to report only for its own owners and
sessions, and a pid check for Hermes spool lines
([#302](https://github.com/ch8er/kelpie/issues/302)). This section goes when it lands.

### `kelpie serve`'s HTTP server has no connection guards (M5)

The port of `kelpie serve` (OTLP ingest, `/metrics`, `/healthz`; 9464 by default) has no
deadline before authentication and no cap on connections, so a slow client can hold its
threads without a token. The hub's port (`[hub]`) has these guards. Keep serve on loopback
(the default; docker compose publishes it on `127.0.0.1` only), or put a reverse proxy in
front that limits connections and request time. Do not publish it with a bare
`docker run -p 9464:9464`. Fix: [#304](https://github.com/ch8er/kelpie/issues/304).

### Other open medium findings

- **M4:** the Linear client has no overall deadline: a server that answers one byte at a
  time can stall the control loop. Only farms with `[adapters.linear]` are affected.
  Follow-up on [#275](https://github.com/ch8er/kelpie/issues/275).
- **M6:** one authenticated `kelpie agent` can grow the hub's executors, metric series and
  alerts without bound by reporting new adapter names. Give hub tokens only to agents you
  run. Follow-up on [#275](https://github.com/ch8er/kelpie/issues/275).
- **M7:** "one Kelpie acts on a board" is documented, not enforced. Two acting instances
  on one board (two `serve`, or an `agent` and a local `serve`) duplicate comments and
  requeues. Run one ([install.md](install.md#two-hermes-boards-on-one-host)). Follow-up on
  [#275](https://github.com/ch8er/kelpie/issues/275).
