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

## Listeners

`kelpie serve` listens on the main port (`/v1/traces`, `/metrics`, `/healthz`; loopback by
default) and, with `[hub]`, on the hub port. Both go through one set of connection guards
(`kelpie.httpguard`, configured by [`[server]`](configuration.md#server)), so nobody needs
a token to hold its threads:

- **Header deadline.** From accept to the end of the headers (the hub's TLS handshake
  included) a connection has `header_timeout` (3s) in all, however slowly it sends; then
  its socket is shut down. A slowloris that drips header bytes is cut at the deadline. A
  `GET` (`/metrics`, `/healthz`) has the same 3s for its reply too, so a client that asks
  and never reads is cut as well.
- **Connection cap.** Connections not yet admitted (an OTLP export or hub request until its
  token checks out, a `GET` throughout) are capped per listener
  (`[server] max_connections`, `[hub] max_connections`, 64 each) and per address
  (`max_connections_per_address`, 16, per /64 for IPv6). Over the per-address cap a
  connection is closed at accept, before a thread starts. Behind a reverse proxy or NAT
  every client shares the proxy's address and its 16 slots; raise the cap there.
- **Slow-client cutoff.** When every slot of a listener is taken, the oldest connection not
  yet admitted is cut to make room, so a real client (through in milliseconds) still gets
  in. Someone with many addresses who opens connections faster than a client sends its
  headers can still crowd it out; firewall the port to the networks that need it where
  that matters.
- **Bodies.** With a token configured, a body is read only after the token checks out.
  Every body is read within `read_timeout` (30s) per read and `body_timeout` (60s) in all,
  and only up to the endpoint's limit (`[traces] max_body_bytes`, `[hub] max_body_bytes`),
  checked before reading and again after gzip. Ingest decodes at most
  `[traces] max_concurrent_requests` exports at once (a tokenless loopback ingest lets any
  local process take one of those for `body_timeout`); the hub holds a few requests per
  agent. One request per connection.

These guards bound threads and memory; they do not stop a flood from a fast client, which
is a firewall's job. `/metrics` and `/healthz` stay unauthenticated: keep the main port on
loopback or a private network, or behind an authenticating proxy, when it carries anything
you would not publish.

### Hub names

What a `kelpie agent` reports names its adapters and, for an adapter with several boards,
the boards it could not read. The hub keeps an executor, a metric series and alerts per
name, so it bounds them (audit M6): it takes only Kelpie's own adapter names (`hermes`,
`claude-code`, `openclaw`, `github-issues`, `linear`), narrowed per agent by
`[[hub.agents]] adapters`; a report's other adapters are refused, never merged or given
an executor, logged at most once a minute, alerted once per agent (until its reports carry
only adapters the hub takes) and counted, once per report, in
`kelpie_hub_refused_total{kind="adapter"}`. Of an adapter's unreadable boards, the first 16
names (first seen first, until the hub restarts) get alerts and series; a later new name
still keeps its tasks out of the rules but is only counted, once per report
(`kind="board"`), with no alert of its own. A fixed list was chosen over "the first names an agent reports": it holds
across hub restarts and gives an agent no first report to abuse. Host sleeps an agent
reports are kept only when they ended before its report, and at most 64 per agent. Role
names on `/metrics` come from the agents' boards too; they are bounded too ([Text from agents](#text-from-agents), audit L10).

## Text from agents

Task titles, comments, block reasons, return reasons, exit reasons, progress notes and
role names come from the agents and may be as long as they like. Kelpie keeps the first
8000 characters of each title, comment, reason and note (as far as acceptance criteria and
evidence are searched; cut with "…") wherever it reads them, from a board, a spool or a
`kelpie agent` report. In a reason it cut, a quote left open counts as quoted to the end,
so a cut never turns a quoted `until:` into a snooze marker. In the ledger it stores at
most 16,000 characters of an action's reason and executor detail and of an alert's
summary; an action's params over 32,000 characters of JSON have each text cut to 500, and
when that is not enough only their numbers, flags and short texts are kept, with
`"truncated": true`. A huge reason
cannot swell the ledger, the daily report or `kelpie why` (audit L9). Task descriptions
(`body`) are not cut: their acceptance criteria are read whole.

On `/metrics` a role label is cut to 64 characters, and only the first 32 roles Kelpie sees
(since it started) get series of their own; later ones are summed under the role `other`
(with a role really named `other`), so the series stay bounded however many roles agents
make up (audit L10). `unknown` and the configured `[report] review_roles` never need a
place. A farm with more than 32 roles sees the later ones as `other` until `kelpie serve`
restarts. A pre-work platform status label is cut to 64 characters too.

## Files Kelpie writes

What Kelpie stores names tasks, block reasons, roles and commands ([privacy.md](privacy.md)),
so it is kept for Kelpie's own user:

- `kelpie.db` is created mode 0600, and SQLite gives its `-wal` and `-shm` the same mode.
  Its folder must exist (a mistyped `[storage] path` fails at startup rather than starting
  an empty database). Report files in `[report] dir` are created 0600 and a missing report
  folder 0700, never through a symlink or into a FIFO. Keys and stores that hold secrets
  were already private: the traces token file, the Hermes park key (`kelpie serve` refuses
  one others may read) and the button store (0600).
- The umask can only narrow these modes: `umask 077` keeps them as above; a looser umask
  never widens them. The umask must leave the owner read, write and execute (up to
  `umask 077`): under `umask 277` a new database is read-only, under `umask 177` a new
  report folder cannot be entered.
- Kelpie never chmods `kelpie.db` or a report file that already exists. One created by an
  older version (0644 under the usual umask) keeps its mode, and `kelpie doctor` warns
  ("data files private"), naming the files others may read (through a symlink, the
  target's mode counts); `chmod 600` them, and `chmod 700` their folder, if other users of
  the machine should not read them. The one exception is the button store, which holds a
  key: it is tightened to 0600 with a warning in the log.
- Put `kelpie.db` in a folder only Kelpie's user can enter. The container image creates
  `/data` 0700, owned by the `kelpie` user; a named volume takes that mode when it is
  first created, an existing volume or bind mount keeps its own.

## What Kelpie can write to GitHub

Reading GitHub (pull requests, checks, Actions runs) needs only `[connectors.github]
token_env`, a read-only token. Every write is off by default, runs only through the engine
(so the rule's mode, `observe` by default, and the global `dry_run` apply; see
[act-checklist.md](act-checklist.md)) and is limited to an allow-list of repositories. The
propose token is mandatory and must be a token of its own; the cancel token is recommended,
and without it the read token does the cancel (and then needs Actions: write):

| Write | Turned on by | Allow-list | Token (minimum permissions) | What it may touch |
| --- | --- | --- | --- | --- |
| `cancel_build` (rule 9) | `[connectors.github.ci] allow_cancel` | `cancel_repos` (and `cancel_workflows`, `cancel_branches`) | `cancel_token_env` (optional; else `token_env`): Actions: write | Cancels one Actions run its last scan offered as superseded, after a fresh re-check; never on the default branch |
| `propose_ci_config` (rule 9) | `[engine.rules.ci_economy] propose_config` and `[connectors.github.ci] allow_propose` | `propose_repos` (required) | `propose_token_env` (required): Contents, Pull requests and Workflows: write on those repositories only | Creates the branch `kelpie/ci-config/<file>`, commits one file under `.github/workflows/` on it, opens a **draft** pull request; deletes only that branch: when its own attempt failed, or when it is still at the head of an earlier merged or closed proposal of Kelpie's |
| Issues adapter actions | `[adapters.github_issues]` and the rule in `act` | the adapter's `repos` | `[adapters.github_issues] token_env`: Issues: write | Labels, assignees and signed comments on issues |

For `propose_ci_config` in particular:

- **Never the default branch.** Kelpie commits only on its own `kelpie/ci-config/` branch,
  made from the default branch's head, with the read file's blob sha (GitHub refuses the
  commit if the file changed meanwhile). It refuses when that branch name would be the
  default branch.
- **Never merged by Kelpie.** The pull request is a draft; Kelpie calls no merge or
  auto-merge endpoint, and its description says a human reviews and merges it.
- **One open proposal per repository**, and idempotent re-runs: while any pull request from
  a `kelpie/ci-config/` branch of the repository is open, a proposal is nothing to do. A
  change a human turned down is never proposed again for that file.
- **Nothing from the repository in what Kelpie writes**, beyond the workflow file itself:
  the pull request's title, description and commit message hold Kelpie's own words, the
  rule's numbers and the workflow file's path (letters, digits, `.`, `_`, `-` only), and
  only fixed text, the default branch's name (checked) and the docs globs (checked,
  quoted) are written into the workflow.
- **A token of its own.** Kelpie refuses to start when `propose_token_env` names, or holds
  the same value as, the read, cancel, billing or issues-adapter token, so a leaked read
  token cannot change workflows and the propose token can be revoked alone. Use a
  fine-grained token limited to `propose_repos`; the Workflows permission is what lets it
  change `.github/workflows/` at all.
- **Branches it deletes**: only `kelpie/ci-config/<file>`, and only while no pull request is
  open from it or into it and it still points at Kelpie's own commit (made in the same
  attempt) or at the head of an earlier closed proposal. GitHub has no conditional delete:
  a push in the moment between that check and the delete goes with the branch.
- **A branch of that name it did not make**: one with no commits of its own (Kelpie cannot
  tell it from a branch its own earlier attempt left) is fast-forwarded and committed on;
  any other refuses.
- **Known limits.** "An earlier proposal of Kelpie's" is a pull request from its branch whose
  description holds Kelpie's hidden mark; anyone who can open pull requests from that
  branch could write the mark. Two Kelpie instances with `allow_propose` on one repository
  can each open a proposal (for different workflow files) at the same moment: enable it on
  one instance only.
- `kelpie ci-config` previews the same change with the read token only (`Contents: read`)
  and writes nothing.

A workflow change runs with the repository's Actions permissions once merged, so review it as
you would any change to CI. Revoke the propose token, or set `allow_propose = false`, to stop
proposals at once.

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

When the ingest listens off loopback, `kelpie serve` refuses a weak token (audit L11), from
`$KELPIE_TRACES_TOKEN` or the token file: under 16 characters, with spaces, or of fewer than
6 different characters. The token file always required 16 characters and no spaces. With
`allow_unauthenticated` (the network is trusted on purpose, as in the shipped docker
compose file) a weak token is taken as no token would be. `kelpie doctor` warns about a
weak token wherever serve listens ("traces token kept private"). `kelpie traces
rotate-token` and `openssl rand -hex 32` give strong ones.

## Per-agent traces tokens

A span says which session and task it belongs to, so with one ingest token every agent's
exporter can report for every other agent too: identical tool calls, an error streak or
spend in another agent's session, enough for rule 10 to stop that run (audit M1).
`[[traces.tokens]]` ([configuration.md](configuration.md#tracestokens)) gives each agent a
token of its own, limited to the owners, roles, sessions or boards it may report for, the
way the multi-host hub limits each `kelpie agent` to its own host:

- **Who a span belongs to comes from the board.** Ingest looks the span's session up in
  what the last snapshots showed (for a session the board no longer shows, in the owners
  Kelpie remembers for budgets), and its task too: a session of another owner, a task other
  than the session's own, or (without a known session) a task of another owner is outside
  the token's scope, and the span is rejected and counted
  (`kelpie_spans_rejected_total{reason="out_of_scope"}`).
- **Ids the board does not show yet are bound.** A session too new to be on the board, or a
  task not on it, belongs to the first token that reports it, and other tokens' spans for it
  are rejected. Every tick checks the bound ids against the board; once it places one
  outside the token's scope, every stored span of that token naming it is deleted (whatever
  its times), and the totals and budget usage are read again, before rules and budgets see
  it. That check runs alone: a request judges and stores its spans with no check in
  between, so no span judged before a revocation is stored after it. A binding lasts for
  the span retention after its last use; bindings are rebuilt from the store after a restart
  (for the tokens still configured). Spans carry
  their token's name (`kelpie.ingest.token`, set by Kelpie; whatever a sender put there is
  replaced or removed), and a span takes a missing session or task from its parent only
  when both came with the same scoped token. Hook spans are folded into OTLP spans before
  the snapshot check, so a fold that moves a span into another session is caught too.
- **What a forger can still do.** Claim a session id before its agent starts (Hermes run ids
  are sequential) and report spans for it until the board shows the run; from then on they
  are deleted, and the agent's own token reports for it. Hold up to 10,000 such ids used
  within a day, after which its spans naming new ones are refused (other tokens are not
  affected).
- **Every token is compared**, each in constant time; two tokens with the same value (or a
  scoped token equal to the shared one at startup) refuse to start, so a request maps to one
  scope. Scoped tokens are matched first, so a scoped token that later equals a rotated
  shared one still reports only for its scope.
- **Rotation per token.** An entry with `token_file` rotates like the shared token
  (`kelpie traces rotate-token --token <name> [--grace 2h]`), with the same file rules,
  grace period and revocation (`--grace 0s`); the other tokens are untouched.
- **Limits.** A `sessions` pattern grants every matching session, whoever owns it: keep
  patterns specific. A span that names no session or task counts toward day and project
  budgets only (a hard one stops runs only with `stop_on_shared_budgets`). A scoped token
  may report for every session of its scope, so scope a token to one agent (`owners`) where
  agents of one role do not trust each other. Requests are forwarded (`[[traces.forward]]`)
  as received, rejected spans included. The largest tool outputs and the daily report fold
  stored OTLP tool calls with hook calls by their call id over the whole day, outside the
  snapshot the check sees: a span carrying another agent's (provider-generated) tool call id
  could be counted under that agent's session there, never in rule 10 or budgets. A removed
  (or renamed) entry covers nothing: its spans stay while the board does not show their
  session or task, and are deleted once it does, so removing a misbehaving agent's token
  also removes what it claimed in advance. The shared token, when set, still
  reports for every session: `kelpie doctor` warns while any rule is in `act`.

**Moving from one token.**

1. For each agent (or group of agents that trust each other), generate a token
   (`openssl rand -hex 32`, or `kelpie traces rotate-token --token <name>` with a
   `token_file`) and add a `[[traces.tokens]]` entry naming its owners (or roles, sessions,
   boards). Restart `kelpie serve`; the shared token keeps working meanwhile.
2. Point each agent's exporter at its own token and restart the agents. A span that its
   new token may not report shows up in `kelpie_spans_rejected_total{reason="out_of_scope"}`
   and the log: widen that entry's scope rather than falling back to the shared token.
3. When no exporter uses the shared token any more, unset `$KELPIE_TRACES_TOKEN` (remove
   `[traces] token_env` and `token_file`) and restart `kelpie serve`. From then on a request
   without one of the agents' tokens gets 401, and `kelpie doctor` passes "traces tokens per
   agent".

## CI and release workflows

Every workflow action is pinned to a commit. Checkouts keep no token in `.git/config`
(`persist-credentials: false`), except the CI job that picks which checks a change needs,
whose filter may fetch the base branch with the job's read-only token (audit L14). The
Docker image's base images (`python:3.12-slim-bookworm`, `ghcr.io/astral-sh/uv`) are
pinned by tag, not yet by digest.

## One acting instance per board

Two Kelpie instances acting on one board (two `kelpie serve`, or a `kelpie agent` and a
local `kelpie serve`) would each keep their own ledger and park key: they would comment and
requeue twice and treat each other's parks and stops as foreign. So an instance that may
act holds an exclusive lock (`flock`) per board for as long as it runs, and a second one
refuses to start ("another Kelpie acts on board ...", naming the lock file and the pid
holding it):

- **`kelpie serve`** takes the locks of its boards when `dry_run` is off and a rule that
  acts on boards is in `act` mode (one whose actions an adapter carries out: requeue,
  comment, stop, park and the like; not a rule that only alerts, nor `ci_economy`, which
  cancels CI builds). An instance whose rules all observe or suggest takes none, so it can
  run next to an acting one (it still alerts twice). With `[hub]` serve has no boards of
  its own: each agent locks its own.
- **`kelpie agent`** takes the locks of the boards whose adapters carry out actions,
  whatever the hub's modes are (it cannot know them); with `[agent] actions = []` it only
  reports and takes none.
- **Where.** Hermes: `<kanban.db>.kelpie-act.lock` next to each board's database. Claude
  Code and OpenClaw: `<spool>.kelpie-act.lock` next to the hook spool (instances reading one
  spool would stop the same sessions; its folder is created 0700 when the hook has not run
  yet); none without a spool or with `stop_dir = false`. A Hermes board whose folder does
  not exist stops an acting instance from starting (a mistyped `db_path`), where an
  observing one leaves that board out and watches the others. A board or spool reached through a
  symlink is locked next to the file it points to, so both names find one lock.
  GitHub Issues and Linear: one file per repository or team under
  `$XDG_STATE_HOME/kelpie/locks/` (by default `~/.local/state/kelpie/locks/`, created 0700),
  named by a hash of the API address and the board.
- **Limits.** The lock is advisory and per machine, for instances that can see the same
  file: it keeps apart two instances on one host (Linux and macOS, local file systems), not
  two machines acting on one GitHub repository or Linear team, nor instances under
  different users for a server board (each has its own state folder). The file holds the
  holder's pid only and is readable by all (0644 under the usual umask), so an instance of
  another user takes the lock read-only and is refused too; Kelpie writes the pid only
  into a file of its own user with one link (never through a hard link). The lock goes
  with the process, so a crash leaves nothing to clean up. Where no lock can be taken at
  all (`flock` unsupported on a network file system), an acting instance does not start.

## Known residual risks

What this release ships knowing, from the audit's open findings. Each has a follow-up;
this section shrinks as they land.

### Rule 10 in act on a shared traces token or a Claude Code or OpenClaw spool (M1)

`loop_breaker` (rule 10) stops a run that repeats identical tool calls, hits an error streak
or exceeds a hard budget (`[[budgets]] hard`), and it reads all of that from spans. The
audit found that an agent could make it stop another agent's run by naming that run in
spans or spool lines (M1). Two fixes ([#302](https://github.com/ch8er/kelpie/issues/302))
close that where they apply:

- **OTLP spans:** with [per-agent tokens](#per-agent-traces-tokens) an agent's token may
  report only for its own sessions.
- **Hermes spool lines:** every line carries its writer (pid and start fingerprint), and a
  line whose writer is not the run's recorded worker, or cannot be checked, is marked
  unverified: rule 10 sends its critical alert but does not stop a run on such lines alone
  (the plugin README's "Spool format"). The fingerprint comes from `/proc`, so it keeps out
  agents in another PID namespace (a container, a sandbox), not a process on the same host
  and namespace set on forging, which could already signal the worker or block the run.

What is left:

- **The shared traces token, or ingest without one** (loopback, or `allow_unauthenticated`),
  still reports for every session, as before. `kelpie doctor` warns ("traces tokens per
  agent") while a rule acts.
- **Claude Code and OpenClaw spool lines** name their session themselves, and those
  platforms record no worker process Kelpie could check them against; any agent that can
  write the spool (the same user, or a container with it mounted) can name another agent's
  session. Opt in to `spool_stops = false` in `[adapters.claude_code]` or
  `[adapters.openclaw]` (default `true`, unchanged) and that adapter marks every spool tool
  line unverified: rule 10 sends its critical alert ("Not stopped: …") but never stops a run
  on those lines alone. The platform's own OTLP tool spans still stop it, so pair it with
  per-agent tokens: under the shared token any holder could still send such spans.
  OpenClaw's own OTLP spans carry no session yet, so on OpenClaw rule 10 then only alerts.
  The setting goes where the adapter runs (the agent host's config with `kelpie agent`).
  Budgets are not affected: they read spend from the span store, which spool lines do not
  enter.
- **Hermes spool lines from an older plugin** cannot be checked: they never stop a run
  alone, so rule 10 stops such runs only from OTLP evidence. On macOS that includes every
  plugin of 0.1.0a2 and earlier, which wrote no `pid_start` there (#353); a macOS Hermes without
  psutil records no worker fingerprint, with the same effect.

**Who is affected.** Farms that run rule 10 in `act` and whose agents are untrusted or
sandboxed (in a container, as another user, on another machine reporting straight to
`kelpie serve`) while sharing the one traces token, or one Claude Code or OpenClaw spool. An
unsandboxed agent running as the same user as its peers gains nothing new: it could already
signal their processes or block their tasks. Across hosts the multi-host hub is not
affected: it namespaces every span and run by the `kelpie agent` whose token sent it.
Agents on one host share that host's OTLP relay, which takes spans without a token, so
within a host the risk is the same as on a single-host farm.

**What to do.** Give every agent its own `[[traces.tokens]]` entry and retire the shared
token ([above](#per-agent-traces-tokens)), and update the Hermes plugin on every agent host.
Where agents that do not trust each other share a Claude Code or OpenClaw spool, set that
adapter's `spool_stops = false` with per-agent tokens, or keep `loop_breaker` in `observe`
or `suggest` ([act-checklist.md](act-checklist.md)), or give them separate farms.

### Other open findings

- **M7, what is left:** one acting instance per board is now enforced on one machine
  ([above](#one-acting-instance-per-board)), not across machines for a GitHub repository
  or Linear team, nor across users for those. Run one acting Kelpie per tracker.
- **Lows L4, L13, L14** ([#336](https://github.com/ch8er/kelpie/issues/336)): stop-marker
  writes in the Claude Code and OpenClaw adapters are not relative to a directory handle
  (matters only if Kelpie runs with more privilege than the agents); outbound clients give
  each resolved address the whole remaining connect time, so a host with several
  blackholed addresses can exceed the deadline several times; what is left of L14: the
  Dockerfile pins its base images by tag, not digest.
