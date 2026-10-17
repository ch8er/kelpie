# OpenClaw adapter

Lets Kelpie watch the agents of an [OpenClaw](https://docs.openclaw.ai) Gateway. Kelpie reads
a spool written by the Kelpie OpenClaw plugin (sessions, tool calls, token usage) and, when
the bundled Workboard plugin is enabled, its cards as tasks. The one action carried out is
`stop` (see [Stopping a session](#stopping-a-session)). Task actions (`requeue`, `unblock`,
`comment`, `wip_commit`) raise `UnsupportedAction`, so in `act` mode the engine alerts a human
instead ("not executed, needs a human").

OpenClaw has no loop guard by default (`tools.loopDetection.enabled` is `false`, and when on
it counts per run, so a loop spread over heartbeat runs is never caught). Rule 10
(`loop_breaker`) counts per OpenClaw session across runs.

What was checked against a real OpenClaw (2026.10.4, npm, a local Gateway on loopback with a
mock OpenAI-compatible model that loops on one tool call): the plugin installs with
`openclaw plugins install --link`, loads at Gateway start, and its hooks receive the fields
read below; a looping run wrote one spool line per tool call with the same `args_hash`, the
adapter turned the spool and a real Workboard database into sessions, spans and tasks, and a
stop ended looping runs (see [Stopping a session](#stopping-a-session)).
Nothing here has run on a production farm or with a hosted model.

## Config

```toml
[adapters.openclaw]
state_dir = "~/.openclaw"                  # OPENCLAW_STATE_DIR of the Gateway
workboard = "~/.openclaw/plugins/workboard/workboard.sqlite"  # default: under state_dir; false = not read
spool = "~/.openclaw/kelpie/spool.jsonl"   # written by the plugin below; false = do not read one
lookback = "24h"                           # sessions and finished cards older than this are left out
token_spans = true                         # LLM spans from the plugin; false if diagnostics-otel sends tokens too
stop_dir = "~/.openclaw/kelpie/stop"       # where stop markers go; false = refuse stop (alerts instead)
stop_ttl = "24h"                           # the plugin's stopTtl
cli = "openclaw"                           # aborts a stopped session's run; false = markers only
cli_timeout = "20s"
enabled = true                             # default when the section exists
```

Unknown keys stop Kelpie at startup. `kelpie status` shows the Workboard path and how old the
newest spool line is. Kelpie reads the spool and the Workboard, writes stop markers and runs
the `openclaw` CLI to abort runs; it must run on the Gateway host, as the Gateway's user. Set
`cli` to an absolute path: the CLI gets Kelpie's `OPENCLAW_*` variables, which can hold the
Gateway token.

## Plugin

`plugin/` in this directory is an OpenClaw plugin: `index.js` (plain JavaScript, no build step,
no dependencies), its manifest `openclaw.plugin.json`, `package.json`, and `kelpie_hook.py`, a
self-contained script (standard library only, Python 3.8+) that writes the spool. Install it
on the Gateway host:

```bash
mkdir -p -m 700 ~/.openclaw/kelpie
cp -r src/kelpie/adapters/openclaw/plugin ~/.openclaw/kelpie/plugin
openclaw plugins install --link ~/.openclaw/kelpie/plugin --force --accept-capabilities
openclaw plugins enable kelpie
```

`--force` acknowledges a local source outside ClawHub; review the files first, since native
plugins run in the Gateway process. Restart the Gateway (or `openclaw plugins reload kelpie`)
after copying a new version.

The plugin and its hook are only as trustworthy as the directory they live in. Agents whose
`exec` runs on the host (no sandbox) run as the Gateway's user and can change any file that
user can, this plugin included; with sandboxed agents `~/.openclaw` is out of their reach.
Set `python` to an absolute path; the hook runs isolated (`python3 -I`: no user site
packages, nothing imported from its own directory).

Token usage and run ends come from the `llm_output` and `agent_end` hooks, which OpenClaw only
gives a non-bundled plugin with conversation access. The plugin reads no conversation content
(it drops prompts, messages and assistant text before anything leaves the Gateway), but
OpenClaw cannot know that, so it is an explicit grant:

```bash
openclaw config set plugins.entries.kelpie.hooks.allowConversationAccess true
```

Without it the Gateway logs `typed hook "llm_output" blocked ...` (and the same for
`agent_end`) at start; sessions and tool calls are still recorded, but `no_progress` (rule 11)
sees no tokens from hooks.

Plugin settings (`plugins.entries.kelpie.config`, all optional):

| Key | Default | Meaning |
| --- | --- | --- |
| `python` | `python3` | Python that runs `kelpie_hook.py` (an absolute path is safer than a `PATH` lookup) |
| `spool` | `~/.openclaw/kelpie/spool.jsonl` | spool file; must match `[adapters.openclaw] spool` |
| `commands` | `true` | `false` records no command prefix |
| `maxBytes` | `20971520` | spool size that moves it to `<spool>.1` |
| `stopDir` | `~/.openclaw/kelpie/stop` | where Kelpie's stop markers are; must match `[adapters.openclaw] stop_dir` |
| `stopTtl` | `86400` | seconds a stop marker holds; must match `[adapters.openclaw] stop_ttl` |

Each forwarded hook starts one short Python process (tens of milliseconds), asynchronously: no
hook is awaited, so the agent never waits for Kelpie, and a missing Python only logs a warning
(once, then every 1000th failure). At most 8 hook processes run at once, each killed after 10
seconds; beyond 256 waiting events new ones are dropped and counted in the log. Nothing the
plugin does in a callback can throw into the Gateway (OpenClaw exits on an uncaught
exception), also when the Gateway runs out of file descriptors. Tool arguments over 1 MiB are
not passed to the hook: the plugin hashes them itself (`paramsDigest`), so a loop of huge
identical calls is still one streak.

### Spool format

```json
{"type":"session_start","at":1792191660.1,"session_id":"1ce1fbe0-…","v":2,"plugin":2,"gate":true,"session_key":"agent:main:main","agent":"main","run_id":null,"resumed_from":null}
{"type":"tool","at":1792191669.08,"session_id":"1ce1fbe0-…","v":2,"plugin":2,"gate":true,"session_key":"agent:main:main","agent":"main","run_id":"784417ea-…","tool":"read","tool_call_id":"call_1","args_hash":"a97158125c6af75c","command":null,"outcome":"error","duration_ms":41,"output_chars":null}
{"type":"llm","at":1792191670.4,"session_id":"1ce1fbe0-…","v":2,"plugin":2,"gate":true,"session_key":"agent:main:main","agent":"main","run_id":"784417ea-…","provider":"anthropic","model":"…","trigger":"heartbeat","input":4000,"output":40,"cacheRead":0,"cacheWrite":0}
{"type":"run_end","at":1792191670.5,"session_id":"1ce1fbe0-…","v":2,"plugin":2,"gate":true,"session_key":"agent:main:main","agent":"main","run_id":"784417ea-…","success":true,"duration_ms":994,"trigger":"heartbeat"}
{"type":"stopped","at":1792191671.0,"session_id":"1ce1fbe0-…","v":2,"plugin":2,"gate":true,"session_key":"agent:main:main","agent":"main","run_id":"784417ea-…","event":"before_tool_call"}
{"type":"session_end","at":1792195000.0,"session_id":"1ce1fbe0-…","v":2,"plugin":2,"gate":true,"session_key":"agent:main:main","agent":"main","run_id":null,"reason":"reset","next_session_id":"…"}
```

`v` is the hook's format, `plugin` the version of `index.js` the Gateway has loaded (2 enforces
stops) and `gate` whether its `before_agent_run` has run (conversation access). `stopped` lines
record a refusal (`event`: the hook that refused), `resumed` lines a lifted stop.

### Privacy

Prompts, messages, tool arguments and results, error messages and assistant text are never
written. `index.js` drops them before the event leaves the Gateway, except tool arguments,
which `kelpie_hook.py` only hashes (the same hash OTLP ingest computes) and, for tools with a
`command` argument (`exec`), reduces to a redacted prefix with the code shared with OTLP
ingest and the other hooks (one block, kept byte-identical by a test; details in
[docs/privacy.md](../../../../docs/privacy.md#hermes-hooks-plugin)). A tool call that failed is
recorded as `outcome: "error"`, without its message. Session keys that name a chat peer or a
group (`agent:main:whatsapp:direct:<phone number>`) keep the channel and its kind and lose the
rest to an HMAC keyed with the install's own secret, `peer.key` next to the spool
(`agent:main:whatsapp:direct:~3f2a9c01d4e5`; a plain hash of a phone number could be reversed
by trying every number); keys that name no one
(`agent:main:main`, subagents, cron) are kept, so Workboard cards still match; `hook:` keys,
which come from operator templates, keep only `hook:`. The hook
process gets only `PATH`, `HOME`,
`LANG`, `LC_ALL`, `TMPDIR`, `SYSTEMROOT` of the Gateway's environment, never provider keys. The spool is
created with mode `0600` (not through a symlink); keep its directory `0700` as above.
`kelpie spool scrub` cleans it like the other spools.

## Stopping a session

`loop_breaker` in `act` mode (runaway loops, error streaks, hard budgets) stops a session with
the `stop` action. OpenClaw has no command that stops a session for good: `/stop` and the
Gateway's `sessions.abort` end the run in flight, and the next heartbeat, cron wake or queued
message starts another. So a stop has two parts:

1. **A stop marker.** The adapter writes `<stop_dir>/<sessionId>.json` (`{"at", "reason"}`,
   mode `0600`, atomically). The plugin reads it in the Gateway, without starting Python:
   while it holds, every tool call of the session is refused (`before_tool_call` answers
   `block` with "Stopped by Kelpie: <reason>", which the model reads as the tool result), and,
   with conversation access, every new run that is not a message from the operator is refused
   before the model is called (`before_agent_run`: heartbeats, cron wakes, inter-session
   deliveries, other people's messages). OpenClaw sends a refused run's text to whoever
   started it and keeps it in the transcript, so that text is fixed and names no reason:
   "This session is paused by its operator's monitoring (Kelpie). It resumes when the operator
   sends it a message." The reason goes only where OpenClaw keeps it out of transcripts and
   logs.
2. **An abort of the run in flight.** The adapter runs
   `openclaw gateway call sessions.abort --params '{"key": <sessionKey>, "clearQueued": true}'`.
   That ends the current run and its queued follow-ups at once. Background runs OpenClaw starts
   outside the session's lane answer `no-active-run` to the key; then the adapter aborts the
   session's latest run id instead (`{"runId": ...}`). A key the hook reduced (a chat peer
   replaced by a hash), or one a newer session id has taken since (after `/new` or a
   rollover), is never sent; only the session's own run id is. Both calls share
   `cli_timeout`. When the marker cannot be written (an agent put something in its place),
   the run is still aborted, then the stop fails and a human is told.

"The operator" is what OpenClaw calls the owner (`senderIsOwner`): a client with admin scope,
such as the `openclaw` CLI or the Control UI, not a chat peer. The marker names the session
key Kelpie stopped, and only a run under that key counts; OpenClaw's own background lanes
(`...:internal-session-effects:...`) never do, though they report trigger `user` and
`senderIsOwner`. The stop is lifted when the operator sends the session a message (`before_agent_run` with trigger `user` and
`senderIsOwner`; that message goes through), when the session is reset, started anew (`/new`)
or deleted (OpenClaw lets any sender authorized for `/new` and `/reset` do that, not only the
operator), when someone deletes the marker file, or after the plugin's `stopTtl` (24 hours;
set `[adapters.openclaw] stop_ttl` to the same). The stop belongs to the session id; when
OpenClaw rolls the conversation over to a new id (idle or daily reset, compaction), the plugin
copies the marker to the new id, so the stop follows. A Gateway restart does not lift it.

What the human told about the stop reads: "Stopped run <session>; its current run was aborted;
every tool call and every run but the operator's message is refused until the operator
messages it, the session is reset or the stop expires". Without conversation access it says
instead that only tool calls are refused, scheduled runs still start and a message does not
lift the stop (the plugin marks its lines once its `before_agent_run` has run, which only happens with access).
When the abort fails (no `openclaw` on Kelpie's `PATH`, the Gateway is down or refuses), the
marker still holds and the message says the run could not be aborted: the run then goes on
until the model gives up on the refused tools, spending a model call per refused tool call.

Verified on OpenClaw 2026.10.4 (local Gateway, a mock model that loops on one tool call):

- a looping `openclaw agent` run: the marker refused the next tool calls, `sessions.abort`
  answered `{"ok": true, "status": "aborted"}` in about a second, and the model saw no request
  after it;
- a heartbeat run (`openclaw system event --mode now`) on the stopped session was refused by
  `before_agent_run` before any model call;
- a message sent with `openclaw agent` (an operator client) lifted the stop and went through;
- OpenClaw's own `skill-workshop-review` background run, which starts after a long run with
  trigger `user` and `senderIsOwner` and at times under the main session's id, was refused
  instead of lifting the stop (the marker names the stopped key, and `internal-session-effects`
  lanes never lift);
- an internal `skill-workshop-review` run that OpenClaw started on its own looped on the same
  call under its own session: the key answered `no-active-run`, the run id aborted it.

Not verified: messages from chat channels (whether a channel's operator gets `senderIsOwner`),
a real idle/daily/compaction rollover carrying the marker (tested against the plugin only).

Limits:

- **A guard against runaway agents, not hostile ones.** An agent that runs shell commands as
  the Gateway's user can delete its marker, forge spool lines or call `openclaw` itself.
- **Without conversation access** there is no `before_agent_run`: heartbeats and cron wakes
  still start runs on a stopped session (their tool calls are refused), and nothing but a
  reset, the marker's deletion or `stopTtl` lifts the stop. Grant access (see
  [Plugin](#plugin)).
- **Same host, same user.** Markers are `0600` in a `0700` directory, written by Kelpie's
  user, and the plugin must read and delete them; the `openclaw` CLI authenticates with that
  user's OpenClaw config (`gateway.auth`) or `OPENCLAW_GATEWAY_TOKEN`. Run Kelpie as the
  Gateway's user on its host. (A marker the plugin cannot read does not stop anything; one it
  cannot delete is reported once in the Gateway log and keeps holding.)
- **Plugin required, and loaded.** Every spool line carries the version of the plugin the
  Gateway has loaded (`"plugin": 2` enforces stops); a session whose newest line comes from an
  older plugin, or from a new copy the Gateway has not reloaded yet, is refused with
  `UnsupportedAction` (a human is alerted). A stop that is written but not enforced (tool
  calls go on more than 2 minutes after the marker, and the plugin never refused one) is
  logged once per session and shows as `exit_reason = "stop not enforced"`.
- **Messages that are lost.** `clearQueued` drops messages queued behind the looping run,
  including the operator's; a message from anyone but the operator to a stopped session is
  refused, and OpenClaw keeps only the replacement text. A message steered into a run that is
  already going does not pass `before_agent_run` and does not lift the stop.
- **Known sessions only.** A session not in the spool within `lookback` is refused with
  `UnsupportedAction`; so is every stop with `stop_dir = false`. An ended session is left alone
  (`NOTHING_TO_DO`).
- **Repeats.** A hard budget stays exceeded, so `loop_breaker` stops the session again after
  each action cooldown (30 minutes by default); the operator's message lifts the stop until
  then.

The session's `exit_reason` reads `stopped by kelpie` once the plugin has refused something,
while its marker holds.

## What Kelpie sees

| OpenClaw | Kelpie |
| --- | --- |
| Workboard card `triage`, `backlog`, `todo`, `scheduled` | `backlog` (`triage` and `scheduled` are pre-work) |
| `ready` / `running` / `review` / `blocked` / `done` | `ready` / `in_progress` / `review` / `blocked` / `done` |
| archived card; `done` card completed before `lookback` | left out |
| card `agentId` | `assignee` and role |
| holder of the card's claim (not expired); else the agent of a `running` card | `owner_id` |
| `parent` and `blocked_by` links | `Dependency(card, depends_on=target)`: in Workboard a child waits until every parent is `done` |
| `child` links | nothing more (the same edges from the parent's side; `subtasks` stays empty) |
| latest `created`/`moved` event into the current status, else `started_at`/`completed_at`/`created_at` | `status_since` |
| a `blocked` card | `Block`: the error of its `blocked` attempt that ended with this block, else its latest comment since the block |
| an OpenClaw `sessionId` in the spool | `Session` with that id |
| the card linked to the session's `sessionKey`, or whose worker key it is (dispatched workers use `agent:<agent>:subagent:workboard-<board>-<card>`, or `subagent:workboard-<board>-<card>` behind the default agent for an unassigned card; when several cards match, a running card, then an open one, then the most recently updated) | `Session.task_id`; else `session:<sessionKey>` |
| `tool` lines | `Span(kind=tool)` via `kelpie.traces.hooks` |
| `llm` lines | one `Span(kind=llm)` per run with its tokens (`input_tokens` includes cache reads and writes); `token_spans = false` leaves them out |

Sessions:

| `Session` field | Source |
| --- | --- |
| `started_at`, `last_heartbeat` | first and latest spool line of the session (lines dated more than 5 minutes ahead are dropped) |
| `ended_at` | `session_end` (`new`, `reset`, `idle`, `daily`, `compaction`, `deleted`, `shutdown`, `restart`), unless the session has later lines (seen live: `/new` sent through `openclaw agent` ended the session and started it again under the same `sessionId`) |
| `exit_reason` | the `session_end` reason; `run failed` when the latest run ended unsuccessfully |
| `owner_id` | the owner of the linked card; else the OpenClaw agent id; else `openclaw` |
| `final_status` | none: OpenClaw sessions declare no task status (the card's own status is on the task) |

**One session, many runs.** An OpenClaw session is a conversation between resets. Every run in
it (user messages, heartbeats, cron wakes) belongs to the same Kelpie session, so the same tool
call repeated by successive heartbeat runs is one streak for `loop_breaker`. A session that
waits for its next message or heartbeat writes nothing; once its last line is older than a
rule's `session_stale_after` (30 minutes by default), rules treat it as ended. The default
heartbeat runs every 30 minutes, so set `session_stale_after` above your heartbeat interval
for rules that take it.

**Trust.** Agents can write files under `~/.openclaw` (the `exec` tool), so the spool is read
defensively: the spool and `<spool>.1` only as regular files (no symlinks, a FIFO is skipped),
at most 64 MiB each per snapshot, lines over 16 KiB skipped. The Workboard database is opened
read-only (`mode=ro`, `query_only`, SQLite's defensive mode, `trusted_schema = OFF`), one read
transaction per snapshot, interrupted 10 seconds after it holds the read lock, and no value
over 1 MB leaves SQLite (a larger one fails the read); the tables must be tables (not views)
with the columns Kelpie reads, text is cut in the query (titles 200, notes 8000, comments and
errors 2000 characters), card ids must be short strings, and of a claim only its holder and
expiry leave SQLite. When the board cannot be read (a schema changed by an OpenClaw upgrade:
it is the plugin's internal schema, version 3 in 2026.10.4, not a published interface; a
corrupt or hostile file), Kelpie logs why once and keeps the last board it read for up to 5
minutes, then goes on with sessions only, so loops are still caught; `kelpie status` shows the
last failure.

**Traces.** OpenClaw exports OpenTelemetry through the separately installed
`diagnostics-otel` plugin (OTLP/HTTP protobuf, so it can point at Kelpie's `/v1/traces`). Its
spans carry no session keys (OpenClaw replaces them with `unknown`), so they cannot be tied to
a session by themselves; a tool span that carries `gen_ai.tool.call.id` should merge with the
hook span of the same call (matched by call id). That export has **not** been tried with Kelpie
(the plugin is not bundled with 2026.10.4 and was not installed here). The hook spool alone is
enough for every rule. If `diagnostics-otel` does send token spans to Kelpie, set
`token_spans = false`: the daily report adds OTLP and hook spans, and only tool spans are
matched one to one, so each run's tokens would count twice. Budgets read spend from the span store, which hook spans do not enter:
a farm-wide `day` budget can use OTLP token spans, but per-agent and per-task budgets cannot be
attributed on OpenClaw until its spans carry a session.
