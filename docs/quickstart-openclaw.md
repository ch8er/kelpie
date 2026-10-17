# Quickstart: Kelpie on an OpenClaw Gateway (observe, then stop runaway loops)

First live run. Kelpie reads a spool written by the Kelpie OpenClaw plugin and, if you use
OpenClaw's Workboard, its cards. It runs its rules in `observe` mode with global dry-run on and
changes nothing. Then, step 8, you let rule 10 (`loop_breaker`) stop runaway loops: OpenClaw
has no loop guard by default, and its own (`tools.loopDetection`) counts per run, so a tool
call repeated by every heartbeat all night is never caught. Of the actions only `stop` exists
on OpenClaw; `requeue`, `unblock` and `comment` become alerts.

Things marked **unverified** have not been tried against a live OpenClaw farm. The plugin, the
hook fields, the Workboard schema and the stop path were checked against OpenClaw 2026.10.4 on
a local Gateway with a mock model (see the [adapter
README](../src/kelpie/adapters/openclaw/README.md)).

## 1. Install (on the Gateway host)

Kelpie reads `~/.openclaw` and writes stop markers the plugin must read, and it aborts runs
with the `openclaw` CLI, so it runs on the Gateway host as the Gateway's user. Python 3.12 and
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/ch8er/kelpie.git
cd kelpie
uv sync
```

If the Gateway sets `OPENCLAW_STATE_DIR`, set `state_dir` (step 4) to it: `openclaw.json` and
the Workboard follow it. The spool and stop directory stay under `~/.openclaw/kelpie` on both
sides unless you set them.

## 2. Tasks: the Workboard (optional)

Kelpie's tasks are the cards of OpenClaw's bundled Workboard plugin. Without it Kelpie sees
sessions only, which is enough for loop and budget stops:

```bash
openclaw plugins enable workboard
```

## 3. The Kelpie plugin

```bash
mkdir -p ~/.openclaw/kelpie/plugin && chmod 700 ~/.openclaw/kelpie
(cd src/kelpie/adapters/openclaw/plugin &&
  install -m 0644 index.js kelpie_hook.py openclaw.plugin.json package.json ~/.openclaw/kelpie/plugin/)
openclaw plugins install --link ~/.openclaw/kelpie/plugin --force --accept-capabilities
openclaw plugins enable kelpie
openclaw config set plugins.entries.kelpie.hooks.allowConversationAccess true
openclaw config set plugins.entries.kelpie.config.python "$(command -v python3)"
openclaw gateway restart
```

Read the four files first: native plugins run inside the Gateway. `--force` acknowledges a
local source outside ClawHub; `--accept-capabilities` accepts that the plugin starts a process
(`python -I kelpie_hook.py`, through `node:child_process`) for each event it records. Use an
absolute `python` the Gateway's agents cannot write to. The directory mode keeps other users
out, not the Gateway's agents: anything that can write as the Gateway's user can change the
plugin, as it can change any of the Gateway's code. Conversation access lets the plugin see
each run's token usage and refuse scheduled runs of a stopped session; the plugin drops
prompts, messages and replies before anything leaves the Gateway (adapter README, "Privacy").
Without it, the Gateway logs `typed hook "llm_output" blocked ...` at start and stops hold
only for tool calls.

After an upgrade of Kelpie, run the `install` line again and restart the Gateway (`kelpie
doctor` says when the loaded copy is not this Kelpie's).

## 4. Config

Create `kelpie.toml` in the repository directory (or point `KELPIE_CONFIG` at it):

```toml
[engine]
dry_run = true            # nothing leaves Kelpie
default_mode = "observe"  # no rule suggests or acts

[adapters.openclaw]
state_dir = "~/.openclaw"
cli = "/usr/local/bin/openclaw"   # the output of `command -v openclaw`
```

`cli` gets Kelpie's `OPENCLAW_*` variables (the Gateway token among them), so give the
absolute path of the executable, in a directory the Gateway's agents cannot write to (a
user-owned npm prefix such as `~/.npm-global/bin` is one they can).

The spool (`~/.openclaw/kelpie/spool.jsonl`), the Workboard database and the stop directory
default to places under `~/.openclaw`; the plugin's own defaults match. If you change one,
change both sides (`[adapters.openclaw]` and `plugins.entries.kelpie.config`).

OpenClaw sessions wait quietly between messages and heartbeats (every 30 minutes by default),
so a session counts as ended once its last line is older than a rule's `session_stale_after`.
Set it above your heartbeat interval for the rules that take it.

## 5. Run and check

```bash
uv run kelpie serve                # second shell
uv run kelpie doctor               # PASS|FAIL|SKIP per check, "fix:" under each FAIL; exit 1 on FAIL
uv run kelpie status               # rules, their mode, Workboard and spool age
```

What `kelpie doctor` checks for OpenClaw:

- **workboard**: the Workboard database can be read with the columns Kelpie needs, how many
  cards there are and how many run. SKIP when the Workboard is off or not enabled.
- **plugin fires**: the spool has a line from the last hour, from a plugin that enforces stops
  (unless `stop_dir = false`). With the Workboard, a missing or old spool is a SKIP while no
  card runs; without it, it is a FAIL. Run doctor within an hour of an agent run.
- **plugin configured**: `openclaw.json` enables the plugin, loads it from a directory holding
  this Kelpie's copy (`index.js`, `kelpie_hook.py` and `openclaw.plugin.json`, byte for byte),
  is not denied or left out of `plugins.allow`, and points it at Kelpie's spool and stop
  directory. Only the `plugins` section is looked at, and only the plugin's load path is
  shown. Relative and `~user` load paths are ignored, so a config with only those fails.
  Without conversation access this is a WARN: stops then hold for tool calls only.
- **gateway**: `openclaw gateway call health` answers (so the CLI can reach the Gateway to
  abort a stopped session's run) and lists `kelpie` among the loaded plugins (so the Gateway
  enforces stop markers). The answer's shape was checked against OpenClaw 2026.10.4.
- **stop markers**: `stop_dir` is a plain directory Kelpie can write, or can be created.

## 6. Watch a loop (still observe)

Rule 10 in `observe` records what it would stop:

```toml
[engine.rules.loop_breaker]
mode = "observe"
```

Its defaults are 8 identical tool calls (same tool, same arguments) within 10 minutes, or 5
tool errors in a row, per OpenClaw session. A session is a conversation between resets, so
heartbeat runs of the main session that repeat the same call count as one streak. Check
`kelpie.db` (the action ledger) and the alerts after a night.

## 7. Try a stop by hand (before any `act`)

On a throwaway agent (`AGENT` below is its id; message it once first so the spool has a
line from it), write a stop marker the way Kelpie does and watch the plugin refuse:

```bash
AGENT=scratch
python3 - "$AGENT" <<'EOF'
import json, os, sys, time
sessions = []
with open(os.path.expanduser("~/.openclaw/kelpie/spool.jsonl")) as spool:
    for line in spool:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("agent") == sys.argv[1]:
            sessions.append(event.get("session_id"))
if not sessions or not sessions[-1]:
    sys.exit(f"no spool line from agent {sys.argv[1]!r} yet")
d = os.path.expanduser("~/.openclaw/kelpie/stop")
os.makedirs(d, mode=0o700, exist_ok=True)
with open(os.path.join(d, sessions[-1] + ".json"), "w") as f:
    json.dump({"at": time.time(), "reason": "manual test"}, f)
print("stopped session", sessions[-1])
EOF
```

Ask that session to run a command: the tool call is refused with "Stopped by Kelpie: manual
test". `openclaw system event --text check --mode now` starts a heartbeat run: it is refused
before the model is called. Send the session a message as the operator (`openclaw agent
--agent <id> --message ...` or the Control UI): the stop lifts and the marker file is gone.
**Unverified**: whether a chat channel's owner counts as the operator (verified only through
`openclaw agent`), and how each channel shows a refused run.

Then, to let Kelpie stop runaway loops by itself, follow the [act checklist](act-checklist.md)
for `loop_breaker` only:

```toml
[engine]
dry_run = false
[engine.rules.loop_breaker]
mode = "act"
```

A stop refuses the session's tool calls and scheduled runs and aborts its run in flight
(`openclaw gateway call sessions.abort`); the operator's next message, `/new`, a reset or 24
hours lift it (adapter README, "Stopping a session").

## Where to look

- `kelpie doctor` and `kelpie status`: setup and what is loaded.
- `http://127.0.0.1:9464/metrics`: `kelpie_dry_run` (must be 1 for observe), `kelpie_actions_total`,
  `kelpie_adapter_errors_total`.
- `kelpie.db` (SQLite): the action ledger of what rules proposed.
- `~/.openclaw/kelpie/spool.jsonl`: what the plugin recorded (`stopped` and `resumed` lines
  show stops being enforced and lifted).
- The adapter README: the full mapping of cards and sessions, privacy and limits.

## Known limits

- Tasks need the Workboard; the Workboard has no API Kelpie writes through, so stale blocks
  (rule 4) and orphaned cards (rule 3) alert instead of acting.
- Token usage comes from the plugin only with conversation access. OpenClaw's
  `diagnostics-otel` export carries no session keys; if you also send it to Kelpie, set
  `token_spans = false` so the report does not count tokens twice.
- Per-agent and per-task dollar budgets need spend in the span store, which hook spans do not
  enter: on OpenClaw only a farm-wide `day` budget from OTLP works (**unverified**).
