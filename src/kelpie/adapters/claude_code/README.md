# Claude Code adapter

Lets Kelpie watch a farm of [Claude Code](https://code.claude.com/docs) sessions. Kelpie reads
the shared task lists (`~/.claude/tasks/<list>/`) and a spool written by a command hook;
Claude Code's own OpenTelemetry spans arrive through OTLP ingest and join the same sessions.
The one action carried out is `stop` (see [Stopping a session](#stopping-a-session)).
Claude Code has no operator command for the others: `requeue`, `unblock`, `comment` and
`wip_commit` raise `UnsupportedAction`, so in `act` mode the engine alerts a human instead
("not executed, needs a human").

What was checked against a real Claude Code (2.1.301, in a cloud session): the hook input
fields of every event below (read from its hook schema), the task file format and location
(a task list written by Claude Code itself), and how a list id becomes a directory name. The
hook has not yet run inside a live Claude Code session, and nothing here has run on a farm.

First live run, step by step: [docs/quickstart-claude-code.md](../../../../docs/quickstart-claude-code.md).
`kelpie doctor` checks the task lists, the spool, the hook registration in
`~/.claude/settings.json` and the stop directory.

## Config

```toml
[adapters.claude_code]
task_lists = ["farm"]                     # CLAUDE_CODE_TASK_LIST_ID values to read; [] = none
tasks_dir = "~/.claude/tasks"             # Claude Code's config dir + /tasks
spool = "~/.claude/kelpie/spool.jsonl"    # written by the hook below; false = do not read one
lookback = "24h"                          # sessions and completed tasks older than this are left out
stop_dir = "~/.claude/kelpie/stop"        # where stop markers go; false = refuse stop (alerts instead)
enabled = true                            # default when the section exists
```

Unknown keys stop Kelpie at startup. `kelpie status` shows whether every list can be read
and how old the newest spool line is. Kelpie only reads these files; it must run on the host
(and as a user) that can read them.

**Task lists.** Claude Code keeps a task list per session unless sessions share one: an agent
team shares its team's list, and `CLAUDE_CODE_TASK_LIST_ID=<name>` makes any set of sessions
share `<name>`. A farm should set `CLAUDE_CODE_TASK_LIST_ID` on every session and name that
list in `task_lists`; sessions' private lists are not read. Set it for team members too:
the team name is not visible to hooks, so the hook records a session's list as
`CLAUDE_CODE_TASK_LIST_ID`, else its session id (Claude Code's default list outside a team),
and task updates of a team member without the variable are not tied to the team's tasks.

## Hook

`hook.py` in this directory is a self-contained command hook (standard library only, no
syntax newer than Python 3.8, so the host's `python3` will do; tested on 3.12 only). It reads
the hook input from stdin, appends one JSON line to the spool, writes to stdout only to
enforce a stop and always exits 0. Install it on each host that runs Claude Code:

```bash
mkdir -p -m 700 ~/.claude/kelpie
cp src/kelpie/adapters/claude_code/hook.py ~/.claude/kelpie/hook.py
```

and register it in `~/.claude/settings.json` (or the project's `.claude/settings.json`):

```json
{
  "hooks": {
    "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "PostToolUseFailure": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}]
  }
}
```

Each hook starts a Python process (tens of milliseconds per tool call). Environment (all
optional), read from the session's environment, which hooks inherit:

| Variable | Default | Meaning |
| --- | --- | --- |
| `KELPIE_CLAUDE_SPOOL` | `~/.claude/kelpie/spool.jsonl` | spool file |
| `KELPIE_CLAUDE_SPOOL_MAX_BYTES` | `20971520` | at this size the spool moves to `<spool>.1` |
| `KELPIE_CLAUDE_SPOOL_COMMANDS` | `1` | `0` stops recording the command prefix |
| `KELPIE_CLAUDE_STOP_DIR` | `~/.claude/kelpie/stop` | where the hook looks for stop markers; must match `stop_dir` |
| `KELPIE_CLAUDE_STOP_TTL` | `86400` | seconds a stop marker holds |
| `CLAUDE_CODE_TASK_LIST_ID` | (none) | the shared task list (see above) |
| `KELPIE_TASK_ID` | (none) | a task id the launcher assigned (e.g. an issue), when the task is not on a list |
| `KELPIE_AGENT` | (none) | the agent's name on the farm |

### Spool format

```json
{"type":"session_start","at":1772463000.1,"session_id":"0b5c…","agent_id":null,"task_list":"farm","kelpie_task":null,"agent":"builder-1","source":"startup","model":"…","agent_type":null}
{"type":"tool","at":1772463010.5,"session_id":"0b5c…","agent_id":null,"task_list":"farm","kelpie_task":null,"agent":"builder-1","tool":"TaskUpdate","tool_call_id":"toolu_01…","args_hash":"3f2a9c01d4e5b678","command":null,"outcome":"ok","duration_ms":12,"output_chars":17,"task":"3","task_status":"in_progress","task_owner":"builder-1"}
{"type":"session_end","at":1772463100.1,"session_id":"0b5c…","agent_id":null,"task_list":"farm","kelpie_task":null,"agent":"builder-1","reason":"other"}
```

Other types: `prompt` (`UserPromptSubmit`, with its `source`), `turn_end` (`Stop`, and
`StopFailure` with its `error` kind), `stopped` (the hook refused a tool call or prompt because
of a stop marker, with the `event`).

### Privacy

Prompts, tool arguments and results, assistant messages, the transcript path and the working
directory are never written. A tool call is its name, the first 16 hex digits of a SHA-256 of
its canonical JSON input (the same hash OTLP ingest and the Hermes plugin compute), the size of
its result in characters and, for tools with a `command` input (`Bash`), a redacted prefix of
the command, redacted by the code shared with OTLP ingest and the Hermes plugin (one block,
kept byte-identical by a test). It is an allowlist: at most 4 arguments, only the
executable's name, the plain lowercase words and numbers right after it, flag names and pipe
stages; everything else is `***`. So `curl -u admin:pw https://x` is `curl -u ***`, `cat
~/.secrets/prod.env` is `cat ***` and `gh pr checks 5` stays as is (details in
[docs/privacy.md](../../../../docs/privacy.md#hermes-hooks-plugin)). A secret written as a
plain lowercase word right after the executable is still kept (`redis-cli auth hunter2`);
`KELPIE_CLAUDE_SPOOL_COMMANDS=0` records no command. A spool written by an older hook is cleaned with `kelpie spool scrub`. For `TaskUpdate` calls only the task id, status and owner are kept, never
subjects or descriptions. The spool is created with mode `0600` (not through a symlink); keep
its directory `0700` as above.

## Stopping a session

`loop_breaker` in `act` mode (runaway loops, error streaks, hard budgets) stops a session with
the `stop` action. Claude Code has no command to stop another process's session, and Kelpie
does not signal processes, so the stop goes through the hook, which Claude Code consults
before every tool call:

1. The adapter writes `<stop_dir>/<session_id>.json` (`{"at": ..., "reason": ...}`, mode
   `0600`, atomically).
2. On the session's next `PreToolUse` the hook answers `{"continue": false, "stopReason": ...}`
   with `permissionDecision: "deny"`: the tool does not run and Claude Code ends the turn,
   showing the reason and how to resume to the human (the model only reads "Stopped by
   Kelpie: <reason>", not how to lift it). Every later tool call of the session (subagents included) gets the same
   answer while the marker holds, and so does a prompt submitted by a machine
   (`UserPromptSubmit` with `source` other than `user`: `claude -p`, the SDK, `/loop` and
   scheduled wakeups, peer messages; a missing `source` counts as a machine).
3. The stop is lifted when a human types a prompt in the session (the hook removes the marker),
   when the session ends, when someone deletes the marker file, or after
   `KELPIE_CLAUDE_STOP_TTL` (24 hours). A headless `claude -p` run exits at the end of the
   stopped turn, and its `SessionEnd` lifts the stop: a launcher that starts the work again
   (a new session, or `--resume`) is not held back, unlike a Hermes task, which stays blocked.

Limits:

- **Cooperative.** A session in the middle of a long model response stops when that response
  asks for a tool, or at the end of the turn; spend stops then, not when Kelpie decides. A
  session that never calls a tool again is not interrupted, but also no longer loops on tools.
- **Same host, same user.** The marker directory is `0700` and markers `0600`, owned by the
  user Kelpie runs as, so the sessions must run as that same user on the same host (or with
  `stop_dir` shared), and `~` must mean the same home for both, or set `stop_dir` and
  `KELPIE_CLAUDE_STOP_DIR` explicitly to the same path.
- **Hook required, and current.** The hook must be registered for `PreToolUse` and be this
  version (spool lines carry `"v": 1`). A session whose hook predates stop support is refused
  with `UnsupportedAction` (a human is alerted). After upgrading Kelpie, copy `hook.py` again
  and add the `PreToolUse` entry. A stop that is written but not enforced (tool calls go on
  more than 2 minutes after the marker, and the hook never refused one) is logged once per
  session and shows as `exit_reason = "stop not enforced"`.
- **Routing.** A stop reaches this adapter when the session claimed a task on a watched list,
  or when Claude Code is the only platform adapter. With Hermes configured as well, a stop of a
  session without a watched task has no route and becomes an alert, and one whose
  `KELPIE_TASK_ID` names a Hermes task goes to Hermes, which fails it (a human is alerted).
- **Cost.** `PreToolUse` starts `python3` before every tool call (tens of milliseconds). If the
  hook times out, Claude Code runs the tool anyway: that call is not stopped, nothing hangs.
- **Repeats.** A hard budget stays exceeded, so `loop_breaker` stops the session again after
  each action cooldown (30 minutes by default); a human prompt lifts the stop until then.
- **Known sessions only.** A session that is not in the spool (no Kelpie hook runs there, or it
  is older than `lookback`) is refused with `UnsupportedAction`, so a human is alerted
  instead; so is every stop with `stop_dir = false`. An ended session is left alone.
- The stop holds for the whole session id, and the task keeps its status: rule 1 later
  reports the task if nobody resumes it.

Verified against Claude Code 2.1.301's hook output schema: `continue`, `stopReason`,
`decision`/`reason`, and for `PreToolUse` `hookSpecificOutput.permissionDecision` (`allow`,
`deny`, `ask`) with `permissionDecisionReason`; `continue: false` sets Claude Code's
`preventContinuation`. Not yet seen live: how the interactive UI shows the stop; that a prompt
typed in the interactive UI carries `source: "user"` (the field is optional; without it the
hook treats the prompt as a machine's and keeps the stop, so the human must delete the marker
file); and that `claude -p` prompts carry a source other than `user`.

Stopping is idempotent: a second stop rewrites the marker with the new reason. The session's
`exit_reason` reads `stopped by kelpie` once the hook has refused something, until a human
prompt resumes it.

## What Kelpie sees

| Claude Code | Kelpie |
| --- | --- |
| task `pending`, every `blockedBy` task completed or gone | `ready` |
| task `pending`, a `blockedBy` task still open | `backlog` |
| `in_progress` / `completed` | `in_progress` / `done` |
| deleted task (its file is removed); `completed` file older than `lookback` | left out |
| task id `3` on list `farm` | task `farm/3` (ids are only unique within a list) |
| `owner` | `owner_id` and `role` |
| `blockedBy` | `Dependency(task, depends_on=blocker)` within the list |
| latest `TaskUpdate` that set the status (spool), else the file's mtime | `status_since` |
| a `session_id` in the spool | `Session` with that id (the `session.id` of its OTLP spans) |
| spool `tool` lines | `Span(kind=tool)` via `kelpie.traces.hooks`; with OTLP export on, each call is kept once (matched by `tool_use_id`) |

Claude Code has no blocked or review status, so these tasks never carry a block and rules 4
and 5 have nothing to do on them.

**Trust.** Agents write the task-list directories, so Kelpie reads them defensively: only
files named `<id>.json` whose `id` matches, opened without following symlinks and without
blocking (a FIFO is skipped), regular files of at most 1 MB, at most 5000 per list; subjects
are cut to 200 characters, owners to 128, `blockedBy` to 100 entries. The spool lives under
`~/.claude` too, so it is read the same way: the spool and `<spool>.1` only as regular files
(no symlinks, a FIFO is skipped), at most 64 MiB each per snapshot, lines over 16 KiB skipped.
Every snapshot reads the whole spool within `lookback` (normally two files of at most
`KELPIE_CLAUDE_SPOOL_MAX_BYTES`). The hook likewise never follows a symlink or waits on a
FIFO at the spool path.

Sessions:

| `Session` field | Source |
| --- | --- |
| `started_at`, `last_heartbeat` | first and latest spool line of the session (lines dated more than 5 minutes ahead are dropped) |
| `ended_at` | `SessionEnd`, unless the session has later lines (it was resumed) |
| `exit_reason` | `SessionEnd` reason (`clear`, `resume`, `logout`, `prompt_input_exit`, `other`), plus the error kind of a failed last turn |
| `task_id` | `KELPIE_TASK_ID`; else `<list>/<id>` of the watched-list task the session last set `in_progress`; else `session:<session_id>` (not a board task) |
| `final_status` | `done` when the session set its own task `completed` and then ended; else none |
| `owner_id` | `KELPIE_AGENT`; else the owner set on the task the session claimed (owners it gives other tasks do not count); else the `--agent` type; else `claude-code` |

Subagent tool calls count for their parent session (the spool keeps their `agent_id`).

**Liveness.** A headless session (`claude -p`) ends with `SessionEnd` when the process exits;
a killed one fires no hook, so it counts as ended once its heartbeat is older than the
rules' `session_stale_after` (30 minutes). An interactive session waiting at its prompt fires
no hooks either and looks the same: with a task still `in_progress`, rule 1 reports it.

**Routing.** Actions on a session without a board task (`session:<id>`) reach this adapter only
when it is the only platform adapter configured; with Hermes as well, they become alerts.
