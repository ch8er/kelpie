# Hermes adapter

Lets Kelpie watch a [Hermes](https://github.com/NousResearch/hermes-agent) farm. Today this
is the read side only: Kelpie reads the Kanban board and a spool of tool calls, so rules 1–4
run in `observe` (or `suggest`) mode. Hermes actions (requeue, unblock, comment, WIP commit)
raise `NotImplementedError` until a later step.

## Config

```toml
[adapters.hermes]
db_path = "~/.hermes/kanban.db"          # named boards: ~/.hermes/kanban/boards/<slug>/kanban.db
spool = "~/.hermes/kelpie/spool.jsonl"   # written by the plugin below
lookback = "24h"                         # runs and spans older than this are left out
enabled = true                           # default when the section exists
```

Unknown keys stop Kelpie at startup. `kelpie status` lists the adapter and whether the
database is readable. One section watches one board; Kelpie opens `kanban.db` read-only,
with a short-lived connection per snapshot, and never writes to it.

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
description: Records tool-call metadata for Kelpie (hashes only, no arguments or results).
EOF
```

Hermes plugins are opt-in: add `kelpie` to `plugins.enabled` in your Hermes `config.yaml`
(or use the `hermes plugins` command of your Hermes version), then restart the gateway or
dispatcher so workers pick it up.

Environment (all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `KELPIE_HERMES_SPOOL` | `~/.hermes/kelpie/spool.jsonl` | spool file |
| `KELPIE_HERMES_SPOOL_MAX_BYTES` | `20971520` | at this size the spool moves to `<spool>.1` |
| `KELPIE_HERMES_SPOOL_COMMANDS` | `1` | `0` stops recording the command prefix |

Task and run ids come from the worker environment that the Hermes dispatcher sets
(`HERMES_KANBAN_TASK`, `HERMES_KANBAN_RUN_ID`); outside a kanban worker they are `null` and
the span is not tied to a run. The `task_id` keyword of Hermes hooks is used only as a
fallback.

### Spool format

```json
{"type":"tool","at":1772463000.5,"task_id":"t_1","run_id":"42","session_id":"...","tool":"terminal","args_hash":"3f2a9c01d4e5b678","command":"pytest -q","outcome":"ok","duration_ms":250}
{"type":"session_end","at":1772463100.1,"task_id":"t_1","run_id":"42","session_id":"...","completed":true,"interrupted":false}
```

### Privacy

Raw tool arguments and results are never written. A tool call is stored as the tool name,
the first 16 hex digits of a SHA-256 of its canonical JSON arguments, and, for tools with a
`command` argument, its first 200 characters (turn off with
`KELPIE_HERMES_SPOOL_COMMANDS=0`). The spool file is created with mode `0600`. Hooks never
raise and do no network I/O.

## What Kelpie sees

| Hermes | Kelpie |
| --- | --- |
| task `triage`, `todo`, `scheduled` | `backlog` (`scheduled` is a timed queue, not a Kelpie wait) |
| `ready` / `running` / `blocked` / `review` / `done` | `ready` / `in_progress` / `blocked` / `review` / `done` |
| `archived` | skipped |
| `assignee` | `owner_id`, and `role` (Hermes routes work by profile) |
| `task_events` (latest status-changing event) | `status_since` |
| latest block event (`kind: reason`) | `Block(reason, since)` |
| `task_links` (parent, child) | `Dependency(child, depends_on=parent)` |
| `task_runs` row | `Session` `hermes-run-<id>` |
| spool `tool` lines | `Span(kind=tool)` linked to the session and task |

Run outcome to the session's `final_status`:

| Run `outcome` | `final_status` |
| --- | --- |
| `completed` | `done` |
| `review_requested` | `review` |
| `blocked` | `blocked` |
| `crashed`, `timed_out`, `spawn_failed`, `gave_up`, `reclaimed`, `changes_requested`, `scheduled`, none | none (the session declared nothing) |

Notes:

- Hermes block kinds are `needs_input`, `capability`, `transient` (and `dependency`, which
  waits in `todo`). Rule 1 only accepts the block reasons in `allowed_block_reasons`
  (default `guard`, `waiting_ci`, ...), so a Hermes `needs_input: ...` block looks like a
  bad block to it. Configure `allowed_block_reasons` for the kinds you use before acting
  on rule 1 for blocked tasks.
- `on_session_end` fires per turn in Hermes, not once per worker process; Kelpie takes
  session ends from `task_runs`, not from the spool, and ignores `session_end` lines.
