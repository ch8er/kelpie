# Hermes adapter

Lets Kelpie watch a [Hermes](https://github.com/NousResearch/hermes-agent) farm. Today this
is the read side only: Kelpie reads the Kanban board and a spool of tool calls, so rules 1–4
run in `observe` (or `suggest`) mode. Hermes actions (requeue, unblock, comment, WIP commit)
raise `NotImplementedError` until a later step, and a rule in `act` mode with `dry_run = false`
and Hermes as the only adapter stops Kelpie at startup.

## Config

```toml
[adapters.hermes]
db_path = "~/.hermes/kanban.db"          # named boards: ~/.hermes/kanban/boards/<slug>/kanban.db
spool = "~/.hermes/kelpie/spool.jsonl"   # written by the plugin below; false = do not read one
lookback = "24h"                         # runs, spans and finished tasks older than this are left out
enabled = true                           # default when the section exists

# Hermes tolerates heartbeats up to an hour old; Kelpie's default is 30 minutes.
[engine.rules.exit_contract]
session_stale_after = "60m"
[engine.rules.orphan_status]
session_stale_after = "60m"
[engine.rules.wait_condition]
session_stale_after = "60m"
```

Unknown keys stop Kelpie at startup. `kelpie status` lists the adapter, whether the database
is readable and how old the last spool line is (or that the spool is missing). One section
watches one board. Kelpie opens `kanban.db` read-only (`mode=ro`, `query_only`), one short
read transaction per snapshot, and never writes to it; Hermes writers in WAL mode are not
blocked and uncommitted rows are not seen.

Kelpie must run on a host that can read `db_path` (the Hermes host, or a mounted copy).
SQLite WAL needs the `-shm` file next to the database to be readable.

## Hooks plugin

`plugin.py` in this directory is a self-contained Hermes plugin (standard library only).
It records one JSON line per tool call (`post_tool_call`) and per session end
(`on_session_end`) in the spool file.

Install on each Hermes host (Hermes discovers user plugins in `~/.hermes/plugins/<name>/`):

```bash
mkdir -p ~/.hermes/plugins/kelpie
cp src/kelpie/adapters/hermes/plugin.py ~/.hermes/plugins/kelpie/__init__.py
cat > ~/.hermes/plugins/kelpie/plugin.yaml <<'EOF'
name: kelpie
version: 0.1.0
description: Records tool-call metadata for Kelpie (hashes and redacted commands only).
EOF
```

Hermes plugins are opt-in: add `kelpie` to `plugins.enabled` in your Hermes `config.yaml`
(or use the `hermes plugins` command of your Hermes version), then restart the gateway or
dispatcher so workers pick it up.

**Profiles.** The dispatcher starts workers with `HERMES_HOME=<profile home>`
(`hermes_cli/kanban_db_dispatch.py`), and the Hermes plugin manager is scoped per
`HERMES_HOME` (`hermes_cli/plugins.py`: `PluginManager(scope_key=hermes_home_key(...))`).
So if your workers run under profiles, install and enable the plugin in each profile's home
(`<profile home>/plugins/kelpie/` and that profile's `config.yaml`), not only in
`~/.hermes`. We could not verify from the source we read whether the plugin directory itself
is per home or shared, so check with `kelpie status` that the spool grows once a worker
runs, and point every profile at the same spool with `KELPIE_HERMES_SPOOL`.

Environment (all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `KELPIE_HERMES_SPOOL` | `~/.hermes/kelpie/spool.jsonl` | spool file |
| `KELPIE_HERMES_SPOOL_MAX_BYTES` | `20971520` | at this size the spool moves to `<spool>.1` |
| `KELPIE_HERMES_SPOOL_COMMANDS` | `1` | `0` stops recording the command prefix |

Task and run ids come from the worker environment that the Hermes dispatcher sets
(`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`). Outside a kanban worker `task_id` is `null`
(the `task_id` keyword of Hermes hooks is an internal id, not a kanban task) and the span is
not tied to a run.

### Spool format

```json
{"type":"tool","at":1772463000.5,"task_id":"t_1","run_id":"42","session_id":"...","tool":"terminal","args_hash":"3f2a9c01d4e5b678","command":"gh pr checks 5","outcome":"ok","duration_ms":250}
{"type":"session_end","at":1772463100.1,"task_id":"t_1","run_id":"42","session_id":"...","completed":true,"interrupted":false}
```

### Privacy

Raw tool arguments and results are never written. A tool call is stored as the tool name, the
first 16 hex digits of a SHA-256 of its canonical JSON arguments and, for tools with a
`command` (or `cmd`) argument, a redacted prefix of it:

- first line only, leading `VAR=value` assignments dropped, at most 4 tokens, 200 characters;
- values masked after `Bearer`, `Authorization:`, `--token`, `--password`, `-p` (and `-pSECRET`);
- values of `key=value` pairs whose key looks like a credential (`API_KEY=...`, `--secret=...`)
  and of `?token=...` query parameters masked;
- URL userinfo (`https://user:pass@host`) masked;
- any run of 24 or more token characters masked, and quoted arguments cut to their opening
  quote.

So `GH_TOKEN=abc gh pr checks 5` is stored as `gh pr checks 5`. This is best effort; set
`KELPIE_HERMES_SPOOL_COMMANDS=0` to record no command at all. The spool file is created with
mode `0600`. Hooks never raise and do no network I/O.

## What Kelpie sees

| Hermes | Kelpie |
| --- | --- |
| task `triage`, `todo`, `scheduled` | `backlog` (`scheduled` is a timed queue, not a Kelpie wait) |
| `ready` / `running` / `blocked` / `review` / `done` | `ready` / `in_progress` / `blocked` / `review` / `done` |
| `archived`; `done` older than `lookback` | left out |
| `assignee` | `owner_id`, and `role` (Hermes routes work by profile) |
| `task_events`, latest status-changing event | `status_since` (non-status events such as `heartbeat`, `commented`, `claim_extended` are ignored) |
| `task_links` (parent, child) | `Dependency(child, depends_on=parent)` (links to archived parents dropped) |
| `task_runs` row | `Session` `hermes-run-<id>` |
| spool `tool` lines | `Span(kind=tool)` linked to the session and task |

Blocks (`Block.reason`), from a `blocked` event at or after `status_since`:

| Hermes | Block reason |
| --- | --- |
| typed block `needs_input`, `capability`, `transient` | `waiting_human: <kind>: <reason>` (Hermes parks these for an operator) |
| untyped legacy block | the raw reason |
| circuit breaker park (`gave_up` event, no block event) | `guard: hermes circuit breaker (<failures>/<limit>): <error>` |
| anything else | empty |

`tasks.block_kind` survives an unblock, so it is only trusted together with a current block
event. Rule 1 allows `waiting_human` and `guard` by default. The alternative is to keep the
adapter's wording out of it and list the Hermes kinds yourself:

```toml
[engine.rules.exit_contract]
allowed_block_reasons = ["guard", "waiting_ci", "waiting_pr_merge", "waiting_human", "quota",
                         "needs_input", "capability", "transient"]
```

Run outcome to the session's `final_status`:

| Run `outcome` | `final_status` |
| --- | --- |
| `completed` | `done` |
| `review_requested` | `review` |
| `blocked` | `blocked` |
| `crashed`, `timed_out`, `stale`, `rate_limited`, `spawn_failed`, `gave_up`, `reclaimed`, `changes_requested`, `scheduled`, none | none (the session declared nothing) |

Liveness (`Session.last_heartbeat`): the latest of the run's `last_heartbeat_at`, the task's
`last_heartbeat_at` (for the task's current run) and the newest spool tool call of the run. A
valid Hermes claim counts as a heartbeat: an open run that is the task's `current_run_id` with
`claim_expires` in the future is alive as of the snapshot.

Notes:

- The Hermes crash sweep closes a crashed run and puts the task back to `ready` or `review`
  in one transaction, so rule 1 rarely sees a crashed run with its task still `running`; it
  fires on workers that vanished before the sweep (stale heartbeat, expired claim) and on
  bad blocks.
- `on_session_end` fires per turn in Hermes, not once per worker process; Kelpie takes
  session ends from `task_runs`, not from the spool, and ignores `session_end` lines.
