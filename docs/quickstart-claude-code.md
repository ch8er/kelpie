# Quickstart: Kelpie on a Claude Code farm (observe only)

First live run, read only. Kelpie reads your sessions' shared task list and a spool written
by a Claude Code hook, and optionally Claude Code's own OpenTelemetry spans. It runs its
rules in `observe` mode with global dry-run on, and changes nothing. Of the Claude Code
actions only `stop` exists (`loop_breaker` in `act` mode). Claude Code has no command to
requeue, unblock or comment on a task, so in `act` mode those become alerts.

Things marked **unverified** have not been tried against a live Claude Code farm. The hook
input and output formats and the task-list files were checked against Claude Code 2.1.301
(see the [adapter README](../src/kelpie/adapters/claude_code/README.md)).

## 1. Install (on the host that runs Claude Code)

Kelpie reads `~/.claude/tasks` and the hook's spool, and writes stop markers that the hook
must read, so it runs on the agent host as the same user as Claude Code. Python
3.12 and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/ch8er/kelpie.git
cd kelpie
uv sync
```

## 2. One task list for the farm

Claude Code keeps a private task list per session unless sessions share one. Give every
session of the farm the same list, and name each agent:

```bash
export CLAUDE_CODE_TASK_LIST_ID=farm      # the shared list: ~/.claude/tasks/farm/
export KELPIE_AGENT=builder-1             # optional: the agent's name in Kelpie's alerts
claude -p "Take the next ready task from the task list, mark it in_progress, do it, mark it completed."
```

Agent teams share their team's list on their own, but the hook cannot see the team name: set
`CLAUDE_CODE_TASK_LIST_ID` for team sessions too. If tasks come from somewhere else (issues
picked by a launcher script), set `KELPIE_TASK_ID=<id>` in the session's environment instead,
and its spans and alerts carry that id.

## 3. Config

Create `kelpie.toml` in the repository directory (or point `KELPIE_CONFIG` at it):

```toml
[engine]
dry_run = true            # nothing leaves Kelpie
default_mode = "observe"  # no rule suggests or acts

[adapters.claude_code]
task_lists = ["farm"]                   # the CLAUDE_CODE_TASK_LIST_ID values to watch
spool = "~/.claude/kelpie/spool.jsonl"  # written by the hook (step 4)
```

`tasks_dir` defaults to `~/.claude/tasks`; change it to `$CLAUDE_CONFIG_DIR/tasks` if your
sessions set `CLAUDE_CONFIG_DIR`.

## 4. The hook

```bash
mkdir -p -m 700 ~/.claude/kelpie
cp src/kelpie/adapters/claude_code/hook.py ~/.claude/kelpie/hook.py
```

Register it in `~/.claude/settings.json` (`$CLAUDE_CONFIG_DIR/settings.json` if set) for
these events, with `"matcher": "*"` on the tool events. Merge the entries into the
`hooks` you already have; each event takes a list:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "PostToolUseFailure": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "StopFailure": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "python3 ~/.claude/kelpie/hook.py", "timeout": 10}]}]
  }
}
```

The hook records ids, tool names, argument hashes and a redacted command prefix, never
prompts, arguments or results (adapter README, "Privacy"). `KELPIE_CLAUDE_SPOOL_COMMANDS=0`
drops even the redacted command. `PreToolUse` is what enforces `stop`; `kelpie doctor`
expects it unless `stop_dir = false` in `[adapters.claude_code]`. Until a stop marker exists,
the hook checks for one small file per tool call and answers nothing.

Each hook starts a `python3` process. **Unverified** on a live farm: the added latency per
tool call (expected: tens of milliseconds).

## 5. OpenTelemetry (recommended: tokens and spend)

The hook gives Kelpie tool calls and session lifecycles. Model calls, tokens and cost come
from Claude Code's own traces, which budgets and rules 10–11 need. Start Kelpie with a token:

```bash
export KELPIE_TRACES_TOKEN=$(openssl rand -hex 24)
uv run kelpie serve        # 127.0.0.1:9464; POST /v1/traces, /metrics, /healthz
```

and in the sessions' environment (span export is a beta feature of Claude Code):

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1
export OTEL_TRACES_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://127.0.0.1:9464/v1/traces
export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer <the KELPIE_TRACES_TOKEN value>"
```

Spans carry the same `session.id` as the hook's lines, so Kelpie joins them per session.
Kelpie accepts traces only, so leave Claude Code's metrics and logs exporters off or pointed
elsewhere.

A tool call reported by both the hook and OTLP should be kept once. **Unverified:** whether
Claude Code's `claude_code.tool` span carries the call's `tool_use_id`. If it does not, Kelpie
matches the pair by session, tool and time, within 2 seconds.

## 6. Run and check

```bash
uv run kelpie serve                # second shell, unless step 5 started it
uv run kelpie doctor               # PASS|WARN|FAIL|SKIP per check, "fix:" under FAIL/WARN; exit 1 on FAIL
uv run kelpie status               # rules, their mode, task lists, spool age
curl -s localhost:9464/metrics | grep -E '^kelpie_(dry_run|engine_ticks_total|actions_total|snapshot_spans)'
```

What `kelpie doctor` checks for Claude Code:

- **task lists**: every list in `task_lists` can be read; how many tasks are in progress.
- **hooks fire**: the spool has a line from the last hour, written by a hook current enough
  to enforce stops (unless `stop_dir = false`). While no watched task is in progress a missing
  or old spool is a SKIP, so run doctor while a session works.
- **hook registered**: the `settings.json` next to `tasks_dir` registers the hook for every
  event above (tool events for all tools), including `PreToolUse` unless `stop_dir = false`,
  and does not set `disableAllHooks`. Hooks kept only in a project's `.claude/settings.json`
  are invisible to doctor, so it skips rather than fails; events split between user and
  project settings show as missing.
- **stop markers**: `stop_dir` is a plain directory Kelpie can write, or can be created.

Plus the generic checks: config and rules load, dry-run on with every rule in observe, OTLP
spans arrived in the last hour (SKIP if none ever did or no watched task is in progress; FAIL
when spans stopped while a task is in progress), the disks are watched (FAIL without
`[resources.prometheus]` and node_exporter on every host: install.md, Host resources), and
Prometheus answers. Run doctor in
the directory, and with the `KELPIE_DB`, that `kelpie serve` uses.

## 7. Try a stop by hand (before any `act`)

On a throwaway session, check that the hook enforces a stop marker the way Kelpie writes one:

```bash
export SID=$(python3 -c "import uuid; print(uuid.uuid4())")
claude --session-id "$SID"                # interactive, with a session id you know
```

In a second shell, with the same `SID`:

```bash
python3 - <<'EOF'
import json, os, time
sid = os.environ["SID"]
d = os.path.expanduser("~/.claude/kelpie/stop")
os.makedirs(d, mode=0o700, exist_ok=True)
with open(os.path.join(d, sid + ".json"), "w") as f:
    json.dump({"at": time.time(), "reason": "manual test"}, f)
EOF
```

Ask the session to run any command. The tool call should be refused with "Stopped by Kelpie:
manual test", and the turn should end. Typing a new prompt yourself should lift the stop; if
it does not, delete `~/.claude/kelpie/stop/$SID.json`. **Unverified**: how the interactive UI
shows the stop, that typed prompts carry `source: "user"` (which is what lifts it), and that a
session in the middle of a long response reacts only at its next tool call (expected).

Then, to let Kelpie stop runaway loops by itself, follow the [act checklist](act-checklist.md)
for `loop_breaker` only (its Claude Code section):

```toml
[engine]
dry_run = false
[engine.rules.loop_breaker]
mode = "act"
```

## Where to look

- `kelpie doctor` and `kelpie status`: setup and what is loaded.
- `http://127.0.0.1:9464/metrics`: `kelpie_dry_run` (must be 1 for observe), `kelpie_actions_total`,
  `kelpie_adapter_errors_total`, `kelpie_spans_received_total`.
- `kelpie.db` (SQLite): the action ledger of what rules proposed.
- The adapter README: the full mapping of tasks and sessions, privacy and limits.

## Known limits

- An interactive session waiting at its prompt fires no hooks. Kelpie checks its process
  instead, so an idle session stays alive and a closed one ends within minutes. That works on
  Linux when Kelpie runs on the agent host itself, as the same user, outside a container.
  Otherwise an idle session counts as ended after 30 minutes (`session_stale_after`), and rule
  1 reports its `in_progress` task (adapter README, "Liveness"). Sessions idle for longer than
  `lookback` stay watched only while Kelpie keeps running.
- Claude Code has no blocked or review status, so stale blocks (rule 4) and ping-pong (rule 5)
  have nothing to work on.
