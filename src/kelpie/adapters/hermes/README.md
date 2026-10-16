# Hermes adapter

Lets Kelpie watch a [Hermes](https://github.com/NousResearch/hermes-agent) farm. Kelpie
reads the Kanban board and a spool of tool calls; rules 1–4 run in `observe` (or `suggest`)
mode. The actions implemented are `stop` (see [Stopping a run](#stopping-a-run)), `wait`
(park a task on conditions), the wake of a task Kelpie parked, and `comment` (see
[Parking, waking, commenting](#parking-waking-commenting)). Unblock, reassign and WIP commit
are not, so a rule other than `loop_breaker`, `wait_condition`, `conflict_warning` and
`dispatch_gate` in `act` mode with `dry_run = false` stops Kelpie at startup when Hermes is
configured.

## Config

```toml
[adapters.hermes]
db_path = "~/.hermes/kanban.db"          # named boards: ~/.hermes/kanban/boards/<slug>/kanban.db
spool = "~/.hermes/kelpie/spool.jsonl"   # written by the plugin below; false = do not read one
lookback = "24h"                         # runs, spans and finished tasks older than this are left out
                                         # (the rules' window; kelpie report reads as far back as its day needs)
enabled = true                           # default when the section exists
git_state = true                         # read git state of live task workspaces (see below)
git_timeout = "5s"                       # limit for each git call; 2x per snapshot in total
cli = "hermes"                           # Hermes executable used by stop, park, wake, comment
cli_timeout = "10s"                      # limit for each command (at most 60s)
# wait_key = "/data/hermes-wait.key"     # signs Kelpie's parks; default: next to the ledger
human_owners = ["alice"]                 # assignees that are people, not agents (optional)

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

**Human owners.** Hermes assigns tasks to profiles and has no notion of a person, so
`human_owners` names the assignees that are people (compared case-insensitively, as
Hermes lowercases profile names). Their tasks get an owner of kind `human` in the snapshot,
which the human task connector (`[connectors.human_tasks]`) needs: a `human_task`
condition holds once such a task is `done`. Hermes registers no conditions itself, so on a
Hermes board they come from `[[connectors.human_tasks.block_patterns]]` (see
`kelpie.example.toml`): a blocked agent task whose reason names a human's task id gets one.

How a human's task sits on a Hermes board (read from the dispatcher, not tried live): assign it
to a name that is **not** a Hermes profile and leave it in `ready` (or `blocked`) until the
person completes it with `hermes kanban complete`. The dispatcher skips a ready task whose
assignee is not a profile (`skipped_nonspawnable`, with a diagnostic event) instead of
starting an agent for it, so it never crashes or loops. If the name *is* a profile, the
dispatcher spawns an agent for the task: do not list such a name in `human_owners`. Hermes has
no failed or cancelled status in Kelpie's mapping and archived tasks leave the board, so an
archived human task makes its condition unknown (`not_found`), never satisfied.

Side effects of `human_owners` on other rules: `ping_pong` escalates to `escalate_to`, else to
the first human owner by id, so setting `human_owners` changes where its escalations go; set
`escalate_to` explicitly. `bottlenecks` and `orphan_status` stop counting these owners as idle
agents.

Kelpie must run on a host that can read `db_path` (the Hermes host, or a mounted copy).
SQLite WAL needs the `-shm` file next to the database to be readable.

## Stopping a run

`loop_breaker` in `act` mode stops a runaway run with the `stop` action; the Hermes adapter
implements it (`SupportsStop`) with Hermes' own operator command, not with signals and not by
writing `kanban.db`:

```
hermes kanban block --kind needs_input -- <task> "Stopped by Kelpie: <reason>. ... hermes kanban unblock <task>"
```

The command is pinned to exactly the run Kelpie looked at: `HERMES_KANBAN_TASK` and
`HERMES_KANBAN_RUN_ID` name that task and run, so Hermes passes the run as `expected_run_id` and
`block_task` adds `AND current_run_id = ?`. A task re-claimed between Kelpie's check and the
command (a new run) is not blocked; the command fails and Kelpie treats the old run as over.
(The CLI writes the reason comment before it tries the block, so a lost race can leave one stray
"BLOCKED: Stopped by Kelpie" comment.) `HERMES_KANBAN_DB` is pinned to the configured `db_path`
(so it acts on the board Kelpie watches). The command gets an allow-listed environment only
(`PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `LC_*`, `TMPDIR`, `XDG_*`, `VIRTUAL_ENV` and `HERMES_*`
except `HERMES_KANBAN_BOARD`, `HERMES_KANBAN_HOME` and `HERMES_DELEGATED_CHILD_CONTEXT`), so
Kelpie's tokens never reach it; run Kelpie as the user (and with the `HERMES_HOME`) that owns the
board. It runs in its own process group with a timeout (`cli_timeout`, default 10 s, because a
stop runs inside the control loop tick); on timeout the whole group is killed. In one
transaction Hermes moves the task `running` -> `blocked` with kind `needs_input` (parked for a
human, never re-queued by Hermes itself), releases the claim, ends the run and adds the reason
as a `BLOCKED:` comment that also says how to resume. A human resumes the task with
`hermes kanban unblock <task>`.

The worker process is not signalled by `block`. Hermes' dispatcher reaps a worker that
outlived its closed run on a later tick (`reap_terminal_workers`, after a 120 s grace period)
and signals it only when the run's recorded start fingerprint (`worker_started_at`) still
matches, so a reused pid is never touched. Kelpie never signals a process itself. If no Hermes
dispatcher is running, the task is still blocked and not re-queued, but the worker process
lives until it exits by itself.

So on Hermes `stop` means "block the task now, reap the worker after about two minutes, and
never without a dispatcher". A budget hard-stop (`[[budgets]]`) therefore ends the *task* at once
but the spend of a still-running worker only when the dispatcher reaps it.

Why not `hermes kanban reclaim`: it terminates the worker but puts the task back in the queue,
where the dispatcher can respawn it before a second command blocks it. `archive` is terminal.

`stop` is idempotent: a run that has already ended, or is no longer the task's current run,
is left alone (no command is run). It fails, and the engine logs it, when the id is not a
`hermes-run-<id>` session, the run is unknown, the CLI is missing, times out (`cli_timeout`) or exits
non-zero, or the task is still running afterwards. Stopping and unblocking the same task
repeatedly makes Hermes route it to `triage` (its unblock-loop breaker); it still is not
re-queued. The CLI contract was read from the Hermes sources (`hermes_cli/kanban.py`,
`kanban_db.py`, `kanban_db_dispatch.py`) and is exercised here against a fake executable only:
try one stop on a test board before enabling `act`.

## Parking, waking, commenting

Kelpie parks a task (rule 12's conflict wait, the dispatch gate) with
`hermes kanban schedule <task> <reason>`: Hermes' `scheduled` status, which is not dispatched,
pages nobody and is left only through `hermes kanban unblock`. The reason carries the
conditions in a signed marker, `[kelpie-wait:v2:...]`, which the adapter reads back from the
`scheduled` event; a task parked this way shows as `waiting` on those conditions, and rule 2
(`wait_condition`) wakes it with `hermes kanban unblock` once they hold. Only a ready task, or
a running task pinned to the run the rule saw, is parked; only a task Kelpie parked is woken.

The marker is signed with a key Kelpie creates on first use (`wait_key`, by default next to
the ledger database, mode 0600). Keep it with the ledger (a container's data volume) and out
of the agents' reach: losing it leaves Kelpie's parks unrecognised (logged; wake those tasks
by hand), and an agent that can read it can forge parks. A marker copied from an earlier park
(it is visible in the `SCHEDULED:` comment) does not verify. Comments are written with
`hermes kanban comment --author kelpie`. Every text Kelpie writes has GitHub PR URLs
rewritten to `owner/repo#N`: a PR URL in a comment holds a ready task back for 24 hours in
Hermes.

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
{"type":"tool","at":1772463000.5,"task_id":"t_1","run_id":"42","session_id":"...","tool":"terminal","tool_call_id":"call_1","args_hash":"3f2a9c01d4e5b678","command":"gh pr checks 5","outcome":"ok","duration_ms":250}
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
| spool `tool` lines | `Span(kind=tool)` linked to the session and task, built by `kelpie.traces.hooks` with a task span and a session span around them; `tool_call_id` kept as `gen_ai.tool.call.id` |

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

### Progress note and git state

`Session.summary` is `task_runs.summary` of the run (empty means none). The `checkpoints`
rule needs more than that: the last commit, the branch and the uncommitted files of the
session. Hermes does not store those, so Kelpie asks git (`git_state = true`, the default).

Git describes the workspace as it is now, so it is attached only to the **latest run of a
task that is not finished** (not `done`, not `archived`), and only when the task has
`workspace_kind = worktree` and a `workspace_path` that is an existing absolute directory on
the Kelpie host with its own `.git` (git is never left to search parent directories). `dir`
workspaces are skipped (they may point to a shared or human checkout), so are `scratch`
workspaces and older runs. Paths are resolved (symlinks, `..`); when the latest runs of two
or more live tasks resolve to the same directory, none of them gets git fields (a warning
is logged once).

| `Session` field | Source |
| --- | --- |
| `changed_files`, `dirty_worktree` | entries of `git status --porcelain=v2 --branch --untracked-files=normal --ignore-submodules=all` (changed, renamed, unmerged and untracked; an untracked directory counts once); dirty when above 0 |
| `branch` | `tasks.branch_name`, else `# branch.head` of that `status` (a detached `HEAD` gives none) |
| `last_commit` | `# branch.oid` of that `status`, first 7 hex digits (none before the first commit) |
| `last_commit_at` | committer time of that commit, from `git cat-file commit <oid>`, in UTC |

`Task.files` (rule 12, conflict warning) are what the task touches: the uncommitted entries
of that `status` (both sides of a rename; a new untracked directory as one `dir/` entry)
plus the files changed between `HEAD` and its merge-bases with `base_refs` (default
`["origin/main", "main"]`; refs that do not resolve are skipped, with a warning once when
none does). They are read for every unfinished task with a worktree, also one with no run
inside `lookback` (a task waiting in review is what new work conflicts with). `Task.repo` is
the repository's common git directory: tasks are compared only within one repository, and
all linked worktrees of a repository share it (separate clones do not).

Failures (not a repository, git missing, timeout, unreadable config) leave the git fields
unset and never fail the snapshot; `branch` from the task is kept. If only the committed
files cannot be read (no base ref, shallow history, timeout), the other fields and the
uncommitted files are kept. Set `git_state = false` to turn all of it off.

#### Trust model

Workspaces are **controlled by the agents**, and so is everything git reads there:
`.git/config` and the files it includes, `.gitattributes`, `.git/info/attributes`, the
objects. Git runs programs named in that config (filter drivers, `core.fsmonitor`,
`gpg.program` for signed commits), so Kelpie asks git only what it can answer without them:

- Only these commands per workspace: `git config -z --name-only --get-regexp '^filter\.'`
  (lists filter drivers, runs nothing), the `status` above (with `-z`), `git cat-file commit
  <oid>` (no `git log`: with `log.showSignature` it runs gpg), and for `Task.files`
  `git rev-parse --verify` per base ref, `git merge-base --all` on the oids it printed, and
  `git diff-tree -r --name-only --no-renames -z` per merge-base (plumbing that compares trees
  by object id: no textconv, no external diff, no filters). Ref names are validated (no
  option, no revision syntax) and passed after `--end-of-options`.
- Only two `.git` layouts are read: an ordinary `.git` directory without a `commondir` file,
  and a linked worktree whose `.git` file names `<common>/worktrees/<id>`, whose `commondir`
  and `gitdir` files lead back to `<common>` and to this `.git`, with `<common>` outside the
  workspace, and neither borrowing objects or refs (`objects/info/alternates`, or a symlinked
  `objects`, `refs`, `packed-refs` or `HEAD`). Anything else (a `.git` symlink, separate git
  dirs, submodule checkouts) is refused with one warning ("untrusted .git layout").
  `GIT_WORK_TREE` is always the workspace, so `core.worktree` in its config cannot make git
  list another directory. This narrows what an agent can make Kelpie report, but Kelpie is
  not a boundary between repositories: an agent that can read another repository can copy
  its objects into its own and have their file names reported (in rule 12's comments, to its
  own tasks). Give Kelpie git access no wider than the agents' own.
- Output is read up to a cap (8 Mi characters of `status`, 2 Mi per `diff-tree`) and git
  is killed beyond it; at most 1000 paths are kept per task.
- Every filter driver found is emptied for the `status` call (`-c filter.<name>.clean=`,
  `smudge=`, `process=`, `required=false`); a driver name that cannot be written as `-c`
  (it contains `=`) means `status` is not run for that workspace.
- Every call also gets `-c core.fsmonitor=false -c core.attributesFile=/dev/null
  -c core.hooksPath=/dev/null -c status.showUntrackedFiles=normal -c submodule.recurse=false`,
  and `--attr-source=<empty tree>` when the installed git has it (2.40 or later, checked once
  per process; the SHA-256 empty tree is used for SHA-256 repositories), so the worktree's
  `.gitattributes` is not read at all.
- Minimal environment: only `PATH` and `HOME` are passed from Kelpie's environment, plus
  `LC_ALL=C`, `GIT_TERMINAL_PROMPT=0`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_ATTR_NOSYSTEM=1` and
  `GIT_OPTIONAL_LOCKS=0` (so `status` never rewrites the index under a running worker), and
  `GIT_NO_LAZY_FETCH=1` with `GIT_ALLOW_PROTOCOL=none`: git never fetches, so a "partial
  clone" with a missing `HEAD` cannot make it start the transport the workspace config names
  (`remote.<name>.uploadpack`, `core.sshCommand`, `ext::`). `GIT_CEILING_DIRECTORIES` is the
  workspace's parent, so an empty or broken `.git` never makes git use a repository further
  up (such as a human's checkout).
  Kelpie's tokens and any inherited `GIT_*` (`GIT_DIR`, `GIT_INDEX_FILE`, ...) never reach git.
  Git still reads the global config of the user Kelpie runs as (`$HOME/.gitconfig`).
- Each call runs in its own process group with `git_timeout`; on timeout the whole group is
  killed.
- Cost bound: workspaces are read by up to 4 threads, each directory at most once per
  snapshot, and all git calls of one snapshot end within `2 × git_timeout` (10 s by
  default); workspaces not reached by then get no git fields in that snapshot. A workspace
  whose git call timed out is skipped for 5 minutes (snapshot time).

Residual risk: git itself still parses hostile repositories (config, index, objects), so a
bug in git is reachable from a workspace. The filter list is read just before `status`, so
an agent that adds a driver in between could still get it run once (a narrow race). Run
Kelpie as a separate unprivileged user that cannot read the secrets of other services and
has nothing in its own `~/.gitconfig` that runs programs; set `git_state = false` if that
is not good enough.

Operations:

- The Docker image built from this repository has no git, so the feature yields nothing
  there (one warning: `git not found on PATH`); install git in a derived image if you need
  it, and run the container with `--init` (or tini): processes killed on a git timeout are
  re-parented to PID 1, which must reap them.
- Git refuses repositories owned by another uid ("detected dubious ownership"); when Kelpie
  runs as a different user than the agents, git fields stay empty without a warning. Run
  Kelpie with debug logging (`kelpie.adapters.hermes`) to see git's error. Whether to add the
  workspaces to `safe.directory` in Kelpie's global git config is the operator's call: it
  makes git trust config written by the agents, which the measures above are meant to
  contain.
- Timeouts, shared workspaces and unusable filter drivers are logged once per workspace and
  cause; a timed out `git version` once per process.

Notes:

- The Hermes crash sweep closes a crashed run and puts the task back to `ready` or `review`
  in one transaction, so rule 1 rarely sees a crashed run with its task still `running`; it
  fires on workers that vanished before the sweep (stale heartbeat, expired claim) and on
  bad blocks.
- `on_session_end` fires per turn in Hermes, not once per worker process; Kelpie takes
  session ends from `task_runs`, not from the spool. `session_end` lines only close the
  run's session span when no tool call follows them.
- If Hermes also exports OTLP spans to Kelpie (for example through the hermes-otel plugin),
  a tool call reported both ways is kept once: the OTLP span, completed with the hook's
  `args_hash`, redacted `command` and session so rules 2 and 10 still see them. Pairs are
  matched one to one by `tool_call_id` alone (whatever session the OTLP span names), else by
  session, tool, `args_hash` and a start or end within 2 seconds
  (`kelpie_snapshot_hook_spans_merged` shows how many were folded in the last tick).

### hermes-otel

Read from the hermes-otel source (`briancaffey/hermes-otel` at `297569e`, 2026-10-14), not
from a live run:

- **Session id.** Spans carry `session.id` (and `session_id`) = Hermes' own chat session
  id, not the Kanban run; `gen_ai.conversation.id` is set on log events only. So OTLP spans
  never carry Kelpie's `hermes-run-<run id>`, and no environment setup is needed for
  de-duplication: matching uses the tool call id.
- **Tool call id.** Tool spans carry `gen_ai.tool.call.id` = Hermes' `tool_call_id`. On an
  older Hermes core that does not pass one, hermes-otel falls back to the hook's `task_id`
  (the same for every call in the session), while Kelpie's hook has no id; those calls then
  fall back to session/tool/`args_hash` matching, which fails because the sessions differ,
  so they are counted twice. Use a Hermes core that passes `tool_call_id`.
- **Task and run.** In a Kanban worker the resource carries `hermes.kanban.task_id`,
  `hermes.kanban.run_id` and `hermes.kanban.board` (from `HERMES_KANBAN_*`). To link OTLP
  spans to board tasks, set `[traces] task_attribute = "hermes.kanban.task_id"`. The resource
  is built with `Resource.create`, so `OTEL_RESOURCE_ATTRIBUTES` is merged in as well.
- **Trace ids.** One trace per turn; in a Kanban worker the turn root's parent is the
  dispatcher's claim span, passed through `TRACEPARENT`.
- **Exporting to Kelpie.** A `type: otlp` backend in `~/.hermes/hermes_otel.yaml` with
  `endpoint: http://<kelpie>:9464/v1/traces`, `headers: {Authorization: "Bearer <token>"}`,
  and `metrics: false`, `logs: false` (Kelpie accepts traces only).
- **Privacy.** hermes-otel sends tool argument previews (`input.value`,
  `gen_ai.tool.call.arguments`) and message content; Kelpie hashes or drops them on ingest,
  but anything forwarded from Kelpie keeps them (see the traces README).
- Hook spans live in the snapshot only, not in the span store, so `span_totals` (per-session
  calls and tokens over 24 hours) count OTLP spans only.
