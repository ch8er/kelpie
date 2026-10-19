# Privacy: what Kelpie stores

Kelpie reads traces, hook records and task boards to decide what to do. It keeps as little
of them as its rules need. This page lists what it keeps, where, for how long, and how to
keep less. It describes the code as it is; the files named below are the reference.

## Summary

| Data | Kept | Not kept | Where | How long |
| --- | --- | --- | --- | --- |
| OTLP spans | ids, timing, model, token counts, tool name, outcome, allowlisted attributes, a hash of tool arguments, output size, a command prefix | message content, prompts, completions, reasoning, tool arguments and results, error messages, user identity | `kelpie.db`, table `spans` | `[traces] retention` (7 days), at most `max_rows` (1,000,000) |
| Hermes hook records | tool name, ids, hash of arguments, a redacted command, outcome, duration, output size | raw arguments and results | spool file on the Hermes host | until rotated: one backup file at 20 MiB |
| Claude Code hook records | session ids, start source, model and agent type, prompt source, turn errors, end reasons; tool name, ids, hash of arguments, a redacted command, outcome, duration, output size; task id, status and owner of `TaskUpdate` calls | prompts, tool arguments and results, messages, transcript path, working directory, task subjects and descriptions | spool file on the agent host | until rotated: one backup file at 20 MiB |
| Claude Code task lists | task id, subject (200 characters), status, owner, `blockedBy` | descriptions | read in place, not copied | |
| OpenClaw hook records | session id, session key (the part naming a chat peer or group replaced by a keyed hash), agent id, run id, session end reasons, the session id a session resumed or was replaced by; tool name, call id, hash of arguments, a redacted command, failed or not, duration, output size; per run (with conversation access) provider, model, token counts, trigger, success | prompts, messages, tool arguments and results, error messages, assistant text | spool file on the Gateway host | until rotated: one backup file at 20 MiB |
| OpenClaw Workboard cards | card id, board, title (200 characters), notes (8000 characters), status, assignee, claim holder, labels, links, session key (reduced like the hook's) and run id, block reason (500 characters); every comment (2000 characters) and attempt error of the cards read is in memory for the tick, only the latest since a block is kept | claim tokens, attachments, proof, worker logs | read in place, not copied | |
| Session owners, task labels | owner id and task id per session, labels per task | | `kelpie.db` | `[traces] retention` |
| Provider cost reports (optional) | per provider and UTC day: the token cost per model (Anthropic) or the day's total (OpenAI), in USD | workspaces, projects, API keys, users, line items | `kelpie.db`, table `cost_reports` | 90 days |
| Action ledger, alert states | each action Kelpie proposed or took, with its reason text; each snooze button press with the person's Telegram user id and name | | `kelpie.db` | not pruned |
| Recorded fixes | the note, rule, command prefix and role of `kelpie fix` | | `kelpie.db` | not pruned |
| Snoozes | task id, until, the reason (500 characters) and who set it (`--by`, default the OS user name; 100 characters) of `kelpie snooze`; for a Telegram button press, the person's Telegram name (40 characters) | | `kelpie.db`, table `snoozes` | 30 days after it expires |
| Snooze buttons (optional) | the HMAC key, the Telegram update offset, and per button reference the alert, the task id, the item number, the message id and when it was issued and used; per alert, the id of its latest message; per message with buttons, its text and formatting as Telegram answered Kelpie (the alert as sent, then with each acknowledgement line, which names the person who pressed) and the keyboard sent | user ids | `kelpie.buttons.db` beside `kelpie.db` (mode 0600) | references, latest messages and message copies 7 days after issue or last edit; the key and offset until the file is deleted |
| Daily reports | the report as Markdown | | `[report] dir` | not pruned |
| LLM root-cause section (optional) | the report's aggregates, sent to the configured model (tasks and people as pseudonyms) | titles, task ids, block reasons, alert texts, commands, tool output, fix notes, roles, names | the provider (or `base_url`) | the provider's terms |
| Forwarded traces | nothing; requests pass through memory | | queue in memory | until sent or dropped |

## OTLP spans

Code: [`src/kelpie/traces/semconv.py`](../src/kelpie/traces/semconv.py).

**Allowlist.** A stored span keeps only these attributes. Everything else is dropped on
arrival, whatever the instrumentation calls it.

- GenAI request parameters and response metadata: `gen_ai.system`, `gen_ai.provider.name`,
  `gen_ai.operation.name`, `gen_ai.output.type`, `gen_ai.request.*` (model, max tokens,
  temperature, top p and k, penalties, seed, choice count, stream, reasoning level),
  `gen_ai.response.id`, `gen_ai.response.model`, `gen_ai.response.finish_reasons`,
  `gen_ai.response.time_to_first_chunk`.
- Usage counters: any numeric `gen_ai.usage.*` attribute.
- Tools and agents: `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`,
  `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.conversation.id`.
- Other: `error.type`, `server.address`, `server.port`, `service.name` (also from the
  resource), `session.id`.
- `kelpie.*` attributes, which an operator sets on purpose (for example `kelpie.task_id`).
  Kelpie keeps them as sent. `kelpie.source` and the four `kelpie.tool.*` values below are
  computed by Kelpie; values an exporter sends under those names are dropped.
- On Claude Code spans only: `success`, `duration_ms`, `ttft_ms`, `attempt`, `status_code`,
  `stop_reason`, `speed`, `effort`, `result_tokens`, `cache_creation_tokens`, `request_id`,
  `span.type`, `query_source`, `decision`, `source`.

Kept values are scalars; strings are cut to 256 characters; at most 64 attributes per span.
Span events are ignored. The span name is kept, cut to 256 characters.

**Dropped.** Message content, prompts, completions, reasoning text, tool arguments and tool
results (under the OpenTelemetry GenAI names and those of OpenLLMetry, OpenInference, Vercel
AI SDK, Langfuse, MLflow and others), error messages, and user or organization identity.

**Tool arguments are reduced to a hash.** Before the arguments are dropped, Kelpie computes
`args_hash`: the first 16 hex digits of a SHA-256 of their canonical JSON
([`src/kelpie/traces/args.py`](../src/kelpie/traces/args.py)). The loop breaker compares
these hashes to find identical calls. The hash is not salted. It does not reveal the
arguments, but someone who can guess the exact arguments of a call (`{"command": "ls"}`)
can confirm the guess.

**Output size, not output.** A tool span records how big its result was, as a number:
`kelpie.tool.output_tokens` (Claude Code's `result_tokens`) or `kelpie.tool.output_chars`
(the length of the result before it is dropped). The huge-tool-outputs analysis and the
daily report use these numbers.

**Command prefix.** For a shell-like tool call, Kelpie keeps a grouping prefix in
`kelpie.tool.command_prefix`. Code:
[`src/kelpie/traces/command.py`](../src/kelpie/traces/command.py).

1. The raw command is redacted first: first line only, leading `VAR=value` assignments
   dropped, at most four words, secrets masked (see the next section).
2. The prefix is the executable without its directory, plus:
   - a subcommand for tools known to take one (`git status`, `npm test`, `cargo build`,
     `xcodebuild`, `docker`, `kubectl` and similar);
   - a script name after `npm run` (and similar) only from a fixed list (`build`, `test`,
     `lint`, `dev`, `start`, `typecheck`, `check`, `format`, `e2e`, `ci`);
   - a `make` or `just` target only from a fixed list (`build`, `test`, `lint`, `check`,
     `install`, `clean`, `all`, `format`, `ci`, `dev`);
   - a module after `python -m` only from a fixed list (`pytest`, `unittest`, `pip`,
     `mypy`, `ruff`, `black`, `build`).
   Every other word is dropped, because it may be a host, a database or a secret:
   `ssh prod-db01` becomes `ssh`. Wrappers (`sudo`, `time`, `env`, `uv run`, `poetry run`,
   `bundle exec`, `rtk`) are skipped with their flags.
3. `kelpie.tool.filter` names an output filter the command already pipes through (`rtk`,
   `xcbeautify`, `xcpretty`, `tail`, `head`, `grep`), so the report can tell filtered calls
   apart.

The command itself is never stored.

**A prefix still says something.** Executable names show which tools a farm uses:
`terraform plan`, `kubectl`, `psql` or the name of an in-house binary is visible in the span
store and in the daily report's largest tool outputs and recommendations. If that is too
much, turn it off:

```toml
[traces]
commands = false
```

With `commands = false` no `kelpie.tool.command_prefix` is stored for new spans (spans
already stored keep theirs until retention removes them). `kelpie.tool.filter` (one of the
filter names above) is still kept. Huge outputs from OTLP spans are then grouped by tool
name instead of by command. Hook records have their own switch, below.

## Hermes hooks plugin

Code: [`src/kelpie/adapters/hermes/plugin.py`](../src/kelpie/adapters/hermes/plugin.py).

The plugin runs inside Hermes on each agent host. It writes one JSON line per tool call and
per session end to the spool (`~/.hermes/kelpie/spool.jsonl`, or `KELPIE_HERMES_SPOOL`).

A tool-call line holds: the tool name, the Hermes task, run and session ids, the tool call
id, `args_hash` (the same hash as above), a redacted `command`, the outcome (`ok` or
`error`), the duration in milliseconds and `output_chars`, the size of the result. Raw
arguments, results and error messages are never written.

The redacted `command` is a short prefix built from an allowlist (`redact_command`, one block
kept byte-identical in `kelpie.traces.command`, the Hermes plugin and the Claude Code hook).
Of the first line (its first 4096 characters), without leading `VAR=value` assignments, at
most four arguments are kept, and only these:

- the executable, without its directory (`/usr/bin/python3` → `python3`);
- plain lowercase words and numbers right after it, the subcommands (`gh pr checks 5`,
  `npm run build`, `xcodebuild test`);
- flag names (`-q`, `--watch`); a cluster or a glued value keeps only its first letter
  (`-la` → `-l***`, `-ppw` → `-p***`), and `--flag=value` keeps only the name;
- a pipe or list separator and the next executable (`pytest -q | tail`), and the module after
  `python -m` from a short list (`pytest`, `pip`, `mypy`, ...);
- after a wrapper that only runs another command (`sudo`, `env`, `nice`, `time`, `nohup`,
  `command`, `exec`, `rtk`), its flag names, `NAME=***` for its assignments, and the wrapped
  executable, read like the first one: `sudo -u root make test` → `sudo -u *** make test`.
  These do not count against the four. A wrapper flag Kelpie does not know may take a value,
  so after one everything is masked (`sudo -X val make` → `sudo -X *** ***`).

Arguments are split on spaces and tabs only, as the shell does; an argument with an escaped
quote (`\"`) masks the rest of the line.

Everything else becomes `***`: flag values, paths, URLs, hosts, `user:pass`, headers,
environment assignments, quoted text, variables and substitutions. Once a flag or a masked
argument has appeared, later plain words are masked too, since they may be its value. So
`curl -k -u admin:pw https://x` → `curl -k -u ***`, `redis-cli -a pw ping` →
`redis-cli -a *** ***`, `curl -H "Authorization: Bearer x" url` → `curl -H *** ***`,
`git clone https://user:pass@host/repo` → `git clone ***`, `cat ~/.secrets/prod.env` →
`cat ***`, and `GH_TOKEN=abc gh pr checks 5` and `PGPASSWORD='a b c' gh pr checks 5` →
`gh pr checks 5`.

What can still get through: a secret written as a plain lowercase word right after the
executable, before any flag (`redis-cli auth hunter2`, `echo hunter2`). To
record no command at all, set this in the environment of every Hermes process that loads the
plugin:

```bash
export KELPIE_HERMES_SPOOL_COMMANDS=0
```

**Spools written before the fix.** Until ch8er/kelpie#82 the redaction masked known secret
patterns and let others through (`curl -u user:pass`, `redis-cli -a pass`, header values,
the rest of a quoted string). `kelpie spool scrub` rewrites existing spools with the current redaction: by default
the configured Hermes and Claude Code spools and their `.1` files, or the paths you give it
(`--dry-run` only counts). It redacts each line's `command` again; an old line that kept a
secret after a masked part (`curl -H *** tok123`, written by an early Claude Code hook) is
cleaned too, because nothing after a masked argument is kept. Lines that are not JSON are
dropped. It runs only as the user who owns the spool (as root: `sudo -u <agent user> kelpie
spool scrub`), so the file and its lock stay writable by the hooks. It holds `<spool>.lock`
(the lock the hooks take to rotate), keeps the file's mode, replaces the file atomically, and
starts over if the spool was rotated meanwhile.
A line a hook appends in the moment of the replace can be lost, so run it while the agents
are idle. It runs where Kelpie is installed, on the spool's host. Copy the new plugin or
hook to the agent hosts first, or new lines keep the old redaction. Copies elsewhere (backups,
hard links, forwarded logs) are not touched.

The spool file is created with mode `0600`. It is rotated to `<spool>.1` when it reaches
`KELPIE_HERMES_SPOOL_MAX_BYTES` (20 MiB), so at most about twice that is on disk. The adapter
reads it into snapshots; hook spans are not written to the span store, but when a hook call
and an OTLP span describe the same call, the snapshot's merged span carries the hook's
`command`.

## Claude Code hook

Code: [`src/kelpie/adapters/claude_code/hook.py`](../src/kelpie/adapters/claude_code/hook.py).

Claude Code runs the hook as a command on each agent host. It writes one JSON line per
session start, prompt, tool call, end of turn and session end to the spool
(`~/.claude/kelpie/spool.jsonl`, or `KELPIE_CLAUDE_SPOOL`). Every line holds the Claude Code
session id, a subagent id when there is one, the task list id, `KELPIE_TASK_ID` and
`KELPIE_AGENT` from the session's environment, the hook's format version `v`, and, for the
liveness check, the Claude Code process id and start time, the host name, the user id, the
PID namespace and the kernel's boot id. Session
start lines add the start `source`, the model and the `--agent` type; prompt lines only the
prompt's `source` (user, sdk, ...), never its text; end-of-turn lines the error kind of a failed
turn; session end lines the end reason. When the hook refuses a call because of a stop, it
writes a `stopped` line with only the session and subagent ids, the event and `v`.

A tool-call line holds: the tool name, the tool use id, `args_hash`, a redacted `command`,
the outcome, the duration and `output_chars`. For a `TaskUpdate` call it also holds the task
id, the status and the owner that were set, never the subject or description. Tool inputs and
results, error messages, assistant messages, the transcript path and the working directory
are never written.

The redacted `command` uses the same shared redaction as the Hermes plugin (see
[above](#hermes-hooks-plugin)): the executable, the plain words right after it, flag names
and pipe stages are kept; values, paths, hosts, URLs and quoted text are masked. `KELPIE_CLAUDE_SPOOL_COMMANDS=0` records no command, and
`kelpie spool scrub` rewrites a spool written by an older hook.

The spool is created with mode `0600` and rotated like the Hermes spool. Stop markers
(`~/.claude/kelpie/stop/<session>.json`, mode `0600`) hold a time and the stopping rule's
reason. The hook deletes a marker when a human prompt resumes the session or the session ends;
an expired marker is ignored but stays on disk until then. Kelpie reads the shared task lists in place
(id, subject cut to 200 characters, status, owner, `blockedBy`) and never reads session
transcripts.

## OpenClaw plugin

Code: [`src/kelpie/adapters/openclaw/plugin/`](../src/kelpie/adapters/openclaw/plugin/).

The plugin runs inside the OpenClaw Gateway. For each forwarded hook it builds a reduced copy
of the event in `index.js` and hands only that to `kelpie_hook.py` (a separate `python3`
process that gets `PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR` and `SYSTEMROOT` of the Gateway's environment,
never its provider keys). Prompts, messages, tool results, error messages and assistant text
are dropped in `index.js` and never leave the Gateway process; tool arguments go to the hook
process only to be hashed and, for `exec`-like tools, reduced to a redacted command with the
shared redaction (see [above](#hermes-hooks-plugin)); "`exec`-like" means any tool whose
arguments have a top-level `command` or `cmd` string. Arguments larger than 1 MiB are not
passed at all: the plugin sends its own hash of them. The hook writes one JSON line per
session start, tool call, run usage, run end and session end to the spool
(`~/.openclaw/kelpie/spool.jsonl`): the OpenClaw session id and key, the agent id, the run id
and the fields listed in the summary. `commands = false` in the plugin's config records no
command. The spool is created with mode `0600` (not through a symlink) and, when the hook
creates its directory, in a `0700` directory; an existing directory or spool keeps its
mode, so create `~/.openclaw/kelpie` with `0700` as the install steps do. It is rotated like
the Hermes spool.

**Session keys name people.** OpenClaw keys a per-peer conversation as
`agent:<id>:<channel>:direct:<peerId>` and a group as `...:group:<groupId>`, where the peer id
is a phone number or a chat user id. The hook keeps `agent:<id>:main`, subagent, cron and OpenClaw's
internal-session keys, and replaces the rest of any other key after the channel and its
`direct`/`dm`/`group`/`channel` marker (thread suffixes included; for a key without a marker,
everything after `agent:<id>`; for `hook:` keys, which come from operator templates, all but
`hook:`) with `~` and 12 hex digits of an HMAC-SHA256 of that whole rest
(`agent:main:whatsapp:direct:~3f2a…`), so the key that reaches the spool, Kelpie's ledger,
reports and alerts (as the task id `session:<key>` of a session without a card) does not carry
the peer. The HMAC key is the install's own secret, `peer.key` next to the spool (32 random
bytes, mode `0600`, created by the hook the first time it needs it): a plain hash of a phone
number could be reversed by hashing every number, this one only by whoever can read
`peer.key`. Without a usable key file the reduced part is `~nokey`. Kelpie reads the same
file (never creates it) to reduce the keys of Workboard cards before matching them; delete it
and both sides start new pseudonyms.

Stop markers (`~/.openclaw/kelpie/stop/<sessionId>.json`, mode `0600`) hold a time and the
stopping rule's reason. The plugin deletes a marker when the operator messages the session or
the session is reset, started anew (`/new`) or deleted, and copies it to the new id when the
session rolls over; an expired marker is ignored but stays on disk until then. The reason
reaches the model of the stopped session (as the refusal of its tool calls) and OpenClaw's
internal block reason; a run the plugin refuses is answered, to whoever sent it, with a fixed
text that names no reason. When
the plugin refuses something for a stop it records a `stopped` line (session, run, which hook);
when it lifts one, a `resumed` line. To abort a run, Kelpie runs `openclaw gateway call
sessions.abort` with the session key or run id, with only `PATH`, `HOME`, `USER`, `LOGNAME`,
`LANG`, `TMPDIR`, `LC_*`, `XDG_*` and `OPENCLAW_*` of its environment.

Kelpie opens the Workboard database read-only and keeps what it reads in memory for one tick.
Of a claimed card's claim it reads only the holder and the expiry; the claim token stays
inside SQLite (the query extracts the two fields). It never reads card attachments, proof or
worker logs, OpenClaw's session database or transcripts.

## Task boards and git

The Hermes adapter opens `kanban.db` read-only and keeps what it reads in memory for one
tick: tasks, titles, assignees, statuses, block reasons, links, runs and run summaries. With
`git_state = true` it asks git for the branch, the last commit id and time, and the number
of changed files of live task worktrees. None of this is stored as such, but rule actions
quote parts of it (see the next section).

## Actions, alerts and reports

- **Ledger and alert states** (`kelpie.db`, tables `actions` and `states`). Every action a
  rule emits is recorded with its reason text and parameters, in every mode, also under
  dry-run. Reasons may quote task ids and titles, block reasons, review return reasons,
  owner ids, branch names and progress notes. These tables are not pruned.
- **Alerts** go to the Telegram and Slack channels you configure, with the same reason
  text, the titles of the tasks they are about and, with `[alerts] task_url`, links to them.
  Those services keep them under their own terms.
- **Daily reports** (`kelpie report`) contain task ids, owner ids, roles, command prefixes
  and token counts. Written to `[report] dir` when set; with `--send` a short summary goes to
  the alert channels.
- **Recorded fixes** (`kelpie fix`) keep the note you type.
- **Snoozes** (`kelpie snooze`) keep the reason you type and who set it (`--by`, by default
  the OS user name), until 30 days after they expire.
- **Snooze buttons** (`buttons = true` on a Telegram channel, off by default). A press
  stores a snooze whose "by" is the person's Telegram name (first and last name, else the
  username; cleaned, at most 40 characters), and a ledger entry with that name and their
  Telegram **user id**, the task and until when. The ledger is not pruned, so these user
  ids stay in `kelpie.db` until you delete the rows. Presses that are refused are counted
  in metrics by reason only; Kelpie's log names the reason, and the user id of a press it
  took (snoozed, refused for a longer snooze, or failed to write). The button store (`kelpie.buttons.db`) holds no user ids. It keeps what a button
  refers to and a copy of each alert message with buttons, as Kelpie sent it and then
  with the acknowledgement lines it added (these name the person who pressed, as the chat
  shows them), because each acknowledgement edits Kelpie's own copy rather than the one a
  press carries; the copies go 7 days after their last edit. Telegram keeps the messages,
  the presses and the edits under its own terms.

## LLM root-cause section

Off unless `[report.llm]` is configured. When it is, each report run sends one request to
the configured provider (or `base_url`) with the report's aggregates: the day, the number
of completed tasks, spend, hours lost per rule and cause, what Kelpie did or would have
done (rule, action, outcome, count), open alerts (rule, severity, hours open, escalation
level), people's queues (agent-hours waited and counts), huge-output offenders (filter,
days, count) and the before/after metrics of fixes (name, unit, values). At most 10 entries
per list. Tasks are named `task-1`, `task-2`... and people `person-1`, `person-2`...; the
real ids stay on the host and are put back into the answer there.

Never sent: task titles, bodies and ids, block reasons, alert and reason texts, commands
and command prefixes, tool arguments and output, fix notes, roles, owner ids and people's
names, any secret. Agents control most of that text, so it could carry instructions to the
model. Rule names, causes, kinds and units are sent only if they are Kelpie's own words
(otherwise `other`). The section ends with what the model saw; a failed call says so too,
since the data may have left the host before the failure.

The API key is read from the environment at report time and sent only to the configured
origin, without redirects. With a `base_url` other than the provider's API, the key
variable must be named explicitly; on localhost no key is needed. The provider keeps the
request under its own terms; point `base_url` at a model on the same host to keep the data
there.

## Forwarding

`[[traces.forward]]` passes every OTLP request Kelpie accepted to another backend
(Langfuse, Phoenix, Datadog) unchanged: the original body, byte for byte, with its content
type and encoding. The allowlist above applies to what Kelpie stores, not to what it
forwards, so destinations receive what your exporters send, including message content and
tool arguments if the exporters include them. Requests wait in a memory queue only and are
never written to disk. The global dry-run does not stop forwarding. Forwarding is off unless
configured.

## Retention

| Data | Rule |
| --- | --- |
| Spans | Deleted once older than `[traces] retention` (default 7 days) and beyond `max_rows` (oldest first), checked about once an hour. Spans that end before the retention are rejected on arrival. |
| Session owners, task labels | Deleted with the same retention. |
| Cost reports | Days older than 90 days are deleted at each read. |
| Ledger, alert states, fixes | Kept until you delete the database. |
| Spool | Rotated at the size limit; one backup. |
| Reports | Kept in `[report] dir` until you delete them. |

SQLite reuses the space of deleted rows but does not overwrite it at once; deleted spans can
remain in the file's free pages until then. Kelpie creates `kelpie.db` with the default file
permissions of its process (umask); keep the database directory readable only by the user
that runs Kelpie. To remove everything Kelpie stored, stop it and delete `kelpie.db`,
`kelpie.db-wal` and `kelpie.db-shm`; cooldowns and open alert states are lost with them.

## Keeping less

| To | Set |
| --- | --- |
| Store no command prefixes from OTLP | `[traces] commands = false` |
| Record no commands in the Hermes spool | `KELPIE_HERMES_SPOOL_COMMANDS=0` on the Hermes hosts |
| Read no hook data at all | `[adapters.hermes] spool = false` and do not install the plugin |
| Record no commands from OpenClaw | `"commands": false` in `plugins.entries.kelpie.config` on the Gateway |
| Record no OpenClaw token usage | leave `plugins.entries.kelpie.hooks.allowConversationAccess` unset |
| Read no OpenClaw data at all | `[adapters.openclaw] spool = false`, `workboard = false`, and do not install the plugin |
| Keep spans for a shorter time | `[traces] retention = "2d"` (budgets and reports that look further back then count less) |
| Store no spans | `[traces] enabled = false` (no budgets; loop detection then relies on Hermes hook spans only) |
| Read no git state | `[adapters.hermes] git_state = false` |
| Send nothing to chat services | no `[[alerts.channels]]` |
