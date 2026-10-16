# Privacy: what Kelpie stores

Kelpie reads traces, hook records and task boards to decide what to do. It keeps as little
of them as its rules need. This page lists what it keeps, where, for how long, and how to
keep less. It describes the code as it is; the files named below are the reference.

## Summary

| Data | Kept | Not kept | Where | How long |
| --- | --- | --- | --- | --- |
| OTLP spans | ids, timing, model, token counts, tool name, outcome, allowlisted attributes, a hash of tool arguments, output size, a command prefix | message content, prompts, completions, reasoning, tool arguments and results, error messages, user identity | `kelpie.db`, table `spans` | `[traces] retention` (7 days), at most `max_rows` (1,000,000) |
| Hermes hook records | tool name, ids, hash of arguments, a redacted command, outcome, duration, output size | raw arguments and results | spool file on the Hermes host | until rotated: one backup file at 20 MiB |
| Session owners, task labels | owner id and task id per session, labels per task | | `kelpie.db` | `[traces] retention` |
| Action ledger, alert states | each action Kelpie proposed or took, with its reason text | | `kelpie.db` | not pruned |
| Recorded fixes | the note, rule, command prefix and role of `kelpie fix` | | `kelpie.db` | not pruned |
| Daily reports | the report as Markdown | | `[report] dir` | not pruned |
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

The redacted `command` is longer than the OTLP prefix: up to four words of the first line,
so `GH_TOKEN=abc gh pr checks 5` is stored as `gh pr checks 5`. Redaction masks:

- the value after `Bearer`, `Authorization:`, `--token`, `--password`, `--passwd`,
  `--secret`, `--api-key`, `--apikey`, `--access-token`, `--auth-token`, `-p` (and
  `-pSECRET`);
- values of `key=value` pairs whose key contains `token`, `secret`, `passw`, `key`, `auth`,
  `credential` or `cookie`, and such parameters in URL query strings;
- URL userinfo (`https://user:pass@host`);
- any run of 24 or more token characters;
- the content of quoted arguments.

This is best effort, and it only masks secrets. Unlike the OTLP prefix, the redacted command
keeps ordinary words, so host names, database names and paths in the first four words stay
readable: `ssh db-1.example.org ls` and `psql -h db.example.org` are stored as they are. To
record no command at all, set this in the environment of every Hermes process that loads the
plugin:

```bash
export KELPIE_HERMES_SPOOL_COMMANDS=0
```

The spool file is created with mode `0600`. It is rotated to `<spool>.1` when it reaches
`KELPIE_HERMES_SPOOL_MAX_BYTES` (20 MiB), so at most about twice that is on disk. The adapter
reads it into snapshots; hook spans are not written to the span store, but when a hook call
and an OTLP span describe the same call, the snapshot's merged span carries the hook's
`command`.

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
  text. Those services keep them under their own terms.
- **Daily reports** (`kelpie report`) contain task ids, owner ids, roles, command prefixes
  and token counts. Written to `[report] dir` when set; with `--send` a short summary goes to
  the alert channels.
- **Recorded fixes** (`kelpie fix`) keep the note you type.

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
| Keep spans for a shorter time | `[traces] retention = "2d"` (budgets and reports that look further back then count less) |
| Store no spans | `[traces] enabled = false` (no budgets, no loop breaker on traces) |
| Read no git state | `[adapters.hermes] git_state = false` |
| Send nothing to chat services | no `[[alerts.channels]]` |
