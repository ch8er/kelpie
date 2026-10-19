# Install and run Kelpie

Kelpie is one process, `kelpie serve`. It runs the control loop and serves `/metrics`,
`/healthz` and OTLP trace ingest on one port. You can run it from a source checkout with uv,
or as a container next to Prometheus and Grafana with docker compose. Both start safe:
global dry-run on and every rule in `observe` mode, so nothing leaves Kelpie.

Kelpie is pre-alpha. Both ways below build from the repository; once a version is
released (see [release.md](release.md)), `pipx install kelpie-sre` and the image
`ghcr.io/ch8er/kelpie:<version>` work too.

- Configuration reference: [configuration.md](configuration.md)
- First live run on a Hermes farm: [quickstart-hermes.md](quickstart-hermes.md)
- Moving rules from observe to act: [act-checklist.md](act-checklist.md)
- What Kelpie stores: [privacy.md](privacy.md)

## Where things run

| Component | Runs on | Notes |
| --- | --- | --- |
| `kelpie serve` | any host | Must reach the platform's data (Hermes: read `kanban.db`), Prometheus and the external APIs you configure. |
| Platform plugin (Hermes hooks) | each agent host | Writes a spool file that the adapter reads. |
| node_exporter | each agent host | Optional. Scraped by Prometheus; Kelpie reads Prometheus. |
| Prometheus, Grafana | any host | Optional. The compose file runs both next to Kelpie. |

The adapters read their platforms' files locally, so `kelpie serve` runs on the agents'
machine. For agents on several machines (a laptop and a cloud VM, two VMs), run
`kelpie agent` on each and one central `kelpie serve` with a hub: [deploy.md](deploy.md).

## Option 1: from source with uv

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ch8er/kelpie.git
cd kelpie
uv sync                              # creates .venv with runtime and dev dependencies
cp kelpie.example.toml kelpie.toml   # optional; every key has a safe default
uv run kelpie status                 # what kelpie serve would load
uv run kelpie serve                  # 127.0.0.1:9464
```

`uv sync --no-dev` installs the runtime dependencies only.

**Config file.** Kelpie reads, in this order: `--config PATH`, else `$KELPIE_CONFIG`, else
`./kelpie.toml` if it exists, else built-in defaults. The config is read once at startup;
restart `kelpie serve` after a change. Unknown sections or keys stop Kelpie with
`kelpie: config error: ...` and exit code 2.

**Command line.**

| Command | What it does |
| --- | --- |
| `kelpie demo [--fast] [--keep] [--web]` | A working day on a simulated Hermes farm, narrated, then its daily report: offline, no config, about a minute (`--fast`: no pauses). `--keep` keeps the farm directory (board, `kelpie.toml`, Kelpie's log, the full report); `--web` writes the report as one self-contained HTML page, prints its path (it stays after the demo, in a new `kelpie-demo-web-*` temporary directory, or in the kept farm) and opens it in a graphical browser when there is one. |
| `kelpie serve [--host H] [--port P]` | Control loop plus HTTP endpoints. Defaults: `$KELPIE_HOST` or `127.0.0.1`, `$KELPIE_PORT` or `9464`. |
| `kelpie status` | Rules with their mode, adapters, traces, connectors, alert channels and budgets. Reads the database without changing it. |
| `kelpie doctor [--prometheus URL]` | Checks a first live run (see below). |
| `kelpie why <task> [--json] [--entries N] [--refresh]` | Why one task is not moving: status and age, owner, labels, sessions, conditions, what it waits for up to the first dead end, the rule findings, open alert states and latest ledger entries. Read-only and offline: `--refresh` also reads network connectors (GitHub). Offline, a block reason that matches `[[block_conditions]]` shows the condition it would attach, marked "not checked"; `--refresh` resolves the PR and shows repo, PR, head commit, last check and result. A `parent-unverified` finding carries a `note` when `[connectors.github.pulls]` is set and serve may judge the parent differently: serve knows the PR lists in full only after two scans in a row, while `why` reads them once or, offline, not at all. Exit 1: not on the board; 3: an adapter could not read its board. `--json` always prints one object (`schema_version`, or `error`). |
| `kelpie report [--date YYYY-MM-DD] [--send]` | The daily report (see [How Kelpie works, "Daily report"](guide.md#daily-report)). |
| `kelpie fix "note" [--rule R] [--command PREFIX] [--role ROLE] [--at TIME]` | Records a fix so the daily report measures its effect; `kelpie fix --list [--days N]` lists them. |
| `kelpie snooze <task> --until <date\|datetime\|duration> [--reason TEXT] [--by NAME]` | Records that the task waits on purpose until then (a decision deferred to a release): a date (`2026-10-25`: through that day, until the next one starts), a date and time (`2026-10-25T09:00`), both in `[report] timezone` unless an offset such as `+02:00` or `Z` follows, or a duration from now (`3d`, `1d12h`). It must lie in the future and at most `[snooze] max_days` (90) ahead. One snooze per task: a new one replaces the old. `--by` defaults to the OS user. The task need not be on the board; when the local boards can be read cheaply (no hub, no issue tracker: GitHub issues, Linear), an unknown task is warned about and snoozed anyway. Prints the stored snooze in `[report] timezone` and UTC. Exit 2: a config error, a bad or out-of-range `--until`, no database yet (`kelpie serve` creates it) or `[storage] path = ":memory:"`; 1: the database cannot be written. See [[snooze]](configuration.md#snooze). |
| `kelpie unsnooze <task>` | Removes the task's snooze. Exit 1: it had none, or the database cannot be written; 2: a config error, no database yet or `:memory:`. |
| `kelpie snoozes [--json] [--all]` | The active snoozes, soonest first; `--all` also lists the expired ones not pruned yet (30 days). Read-only: on a database older than snoozes it prints "no snoozes (the database predates snoozes; kelpie serve upgrades it)" and exits 0. `until:` markers in block reasons are not listed: `kelpie why <task>` shows them. `--json` prints one object (`schema_version`, `snoozes`, and `note` when there is one). Exit 1: the database cannot be read; 2: a config error or `:memory:`. |

Global options go before the command, `kelpie [--config PATH] [--log-level LEVEL] <command>`
(log level default `$KELPIE_LOG_LEVEL` or `INFO`); `kelpie --version` prints the version.
`kelpie status --config x.toml` fails with "unrecognized arguments".

`kelpie serve` handles SIGTERM and SIGINT: it stops the loop, gives queued alerts about two
seconds to go out, stops trace forwarding (requests still queued are dropped and counted) and
closes the database. Run it under any process supervisor you already use.

## Option 2: docker compose

The compose file starts Kelpie, Prometheus and Grafana. The image is built locally from the
`Dockerfile` (`kelpie-sre:dev`); with `KELPIE_IMAGE=ghcr.io/ch8er/kelpie:<version>` and
`docker compose up -d --no-build` it runs a published one instead. Do not combine
`KELPIE_IMAGE` with `docker compose build` or `up --build`: that tags a local build with
the published name.

```bash
cp .env.example .env     # set GRAFANA_ADMIN_PASSWORD
docker compose up -d
docker compose --profile local-agents up -d   # also node_exporter, when agents run on this host
```

| Service | Address | Notes |
| --- | --- | --- |
| `kelpie` | `127.0.0.1:9464` | Runs as uid 10001. Database in the `kelpie-data` volume at `/data/kelpie.db`. |
| `prometheus` | `127.0.0.1:9090` | Scrapes `kelpie:9464` and `node-exporter:9100` (`deploy/prometheus/prometheus.yml`); keeps 30 days, at most 2 GB. |
| `grafana` | `127.0.0.1:3000` | Prometheus data source and the **Kelpie** dashboard are provisioned from `deploy/grafana/`. Anonymous access is off. |
| `node-exporter` | not published | Only with `--profile local-agents`. |

Add the agent hosts' node_exporters to `deploy/prometheus/prometheus.yml`. Without the
`local-agents` profile the `node` job shows as down, which is harmless.

**Config in the container.** The compose service has no config and runs with the defaults.
To change that, mount a config and the platform's files in a `docker-compose.override.yml`
and set `KELPIE_CONFIG`. The Hermes variant, with its caveats (file permissions for uid
10001, SQLite WAL and read-only mounts, no `git` in the image), is in
[quickstart-hermes.md, step 6](quickstart-hermes.md#6-prometheus-and-grafana-docker-compose).

Inside the image `KELPIE_HOST` is `0.0.0.0`, so the exposure check below applies. The compose
file sets `KELPIE_TRACES_ALLOW_UNAUTHENTICATED=1` because it publishes the port on
`127.0.0.1` only. Set `KELPIE_TRACES_TOKEN` and remove that variable before you publish the
port on another address.

## Host resources: node_exporter, Prometheus and directory sizes

Rule 7 sees disks, memory and processes only through node_exporter and Prometheus (Kelpie
has no collector of its own). Without them a disk fills unseen: on a live farm one sat at
94 % and grew 9 GB a day from a simulator cache (incident 19). `kelpie doctor` fails with
"disk is not watched" until `[resources.prometheus]` is set.

**macOS** (Homebrew):

```sh
brew install node_exporter prometheus
mkdir -p /opt/homebrew/var/node_exporter/textfile
# node_exporter's flags live in this file under brew services:
echo '--collector.textfile.directory=/opt/homebrew/var/node_exporter/textfile' \
  > /opt/homebrew/etc/node_exporter.args
brew services start node_exporter
# Cap what Prometheus keeps, so monitoring cannot fill the disk it watches:
echo '--storage.tsdb.retention.size=2GB' >> /opt/homebrew/etc/prometheus.args
brew services start prometheus
```

On a Mac, node_exporter has no processes collector, and the farm hit `kern.maxprocperuid`
twice (every new process of the agents failed). Copy `scripts/kelpie-process-limit.sh` to
`/usr/local/share/kelpie/` and load `deploy/textfile/com.kelpie.process-limit.plist` as the
user the agents run as: it writes that user's processes and limit every minute, and rule 7
warns as they near it. The agent user must be able to write the textfile directory. A
LaunchAgent stops at logout, so on a headless farm install the plist as a LaunchDaemon
(`/Library/LaunchDaemons`) with a `UserName` key. Kelpie ignores the count once its file is
5 minutes old (by Prometheus's clock: keep the hosts' clocks in sync), so a stopped script
does not freeze it. Give the plist the same `SoftResourceLimits` as the agents' own job, since
the process ulimit it reports is its own. The load alert is off on macOS (`load_on_macos`): its load average
counts waiting threads and runs at 300 to 800 on a healthy Mac; Kelpie knows a Mac from
node_exporter's uname collector (on by default; leaving out the read-only volumes needs it
too). Memory is free + inactive (there is no
MemAvailable; this leaves out active file cache, so it reads a little high), and the disk is the data volume (`/System/Volumes/Data`); the
APFS system volumes and read-only mounts such as simulator runtimes are left out.

**Linux** (Debian/Ubuntu; the same flags with any packaging):

```sh
sudo apt install prometheus-node-exporter prometheus
sudo mkdir -p /var/lib/node_exporter/textfile
# /etc/default/prometheus-node-exporter:
#   ARGS="--collector.textfile.directory=/var/lib/node_exporter/textfile --collector.processes"
# /etc/default/prometheus:
#   ARGS="--storage.tsdb.retention.size=2GB"
sudo systemctl restart prometheus-node-exporter prometheus
```

On Linux the process limit comes from node_exporter's `--collector.processes`
(`node_processes_pids` against `node_processes_max_processes`).

Add each agent host's node_exporter (`host:9100`) to Prometheus' scrape config, and the hosts
to `[resources.prometheus] hosts`.

**What grows.** The time-to-full alert names the fastest-growing directories when they are
measured: copy `scripts/kelpie-path-sizes.sh` to `/usr/local/share/kelpie/` and run it every
15 minutes with node_exporter's textfile directory and the directories that grow (simulator
devices, Xcode DerivedData, caches, `~/.hermes`, Docker). `deploy/textfile/` has a launchd
agent (`com.kelpie.path-sizes.plist`, macOS) and a systemd service and timer (Linux): edit the
paths in them first. Pick a few directories, not the whole disk: `du` walks every file.

## Hermes plugin

On a Hermes farm Kelpie needs the `[adapters.hermes]` section and the Kelpie hooks plugin on
each Hermes host. The plugin is one standard-library Python file
(`src/kelpie/adapters/hermes/plugin.py`) that appends one JSON line per tool call and
session end to a spool file (default `~/.hermes/kelpie/spool.jsonl`). The steps are in the
quickstart:

- [Step 2, config](quickstart-hermes.md#2-config)
- [Step 3, hooks plugin](quickstart-hermes.md#3-hooks-plugin-tool-calls-from-hermes)
- [Step 4, OTLP traces from hermes-otel (optional)](quickstart-hermes.md#4-otlp-traces-optional)

Adapter details (status mapping, blocks, git state, `stop`): the
[Hermes adapter README](../src/kelpie/adapters/hermes/README.md). What the plugin records:
[privacy.md](privacy.md#hermes-hooks-plugin).

## kelpie doctor

`kelpie doctor` checks a first live run and prints one line per check, `PASS`, `WARN`,
`FAIL` or `SKIP`, with a `fix:` line under each failure or warning. It exits 1 if any check
failed; a warning does not fail it. It opens the
board and the database read-only and writes nothing.

| Check | Passes when |
| --- | --- |
| config loads | The config parses and every configured rule is known. |
| rule modes | Lists each mode with its rules and whether `[engine.rules.<name>] mode` or `default_mode` put them there, e.g. `dry_run off; act: loop_breaker (per rule); suggest: needs_you (per rule); observe: 14 rules (default_mode)`. Passes when the rules run as the config says, a deliberate `suggest` or `act` included. WARN when `dry_run = true` holds back rules configured to suggest or act (nothing acts); when a rule in `act` cannot act on this farm, because no adapter or connector carries out its actions (they become alerts) or because the startup check refuses it (`kelpie serve` refuses to start once `dry_run` is off); when `dispatch_gate`, or `conflict_warning` with `serialize`, parks tasks in `act` but `wait_condition` is not in `act` (nothing wakes them, as `kelpie serve` warns at startup); when `dry_run = false` and rules are in `suggest` or `act` but there is no `[[alerts.channels]]` (their alerts, suggestions and action notices are only logged and dropped); or when `act` comes only from `default_mode`. Fails only when `kelpie serve` would refuse to start: `dry_run = false` and a rule in `act` that the startup check refuses. The fixes point to [`act-checklist.md`](act-checklist.md). |
| kanban.db | The Hermes board is readable (skipped without `[adapters.hermes]`). |
| hooks fire | The spool has a line from the last hour. |
| OTLP spans arrive | A span arrived in the last hour (skipped if none ever did: OTLP is optional). |
| worktrees readable | git can read the workspaces of live tasks (skipped with `git_state = false`). |
| PR blocks have conditions | No block reason on the board names a PR (`PR #12`, `owner/repo#12`, a `/pull/12` link, `#12` next to PR or merge wording; `guard:` blocks are left out), or every one that does matches a `[[block_conditions]]` pattern whose `pr` names a PR of `[connectors.github] repos`, as `kelpie serve` resolves it. Otherwise a WARN: those blocks would look like stuck tasks or questions for a person (on a live farm, most of the noise of a 24 h review). |
| disk watched | Rule 7 is loaded and `[resources.prometheus]` is set, and that Prometheus has `node_filesystem_size_bytes` for every configured host (FAIL otherwise: incident 19, a disk filled with no node_exporter). WARN, with every cause at once, when the `disk_growth` query fails (when Prometheus rejects it: its subqueries need 2.7 or later), there is no `kelpie_path_bytes` for a host (the alert cannot say what grows) or the token goes over plain http; also when Prometheus does not answer (doctor may run where Prometheus is out of reach). A host with no growth rate yet is a note, not a warning: a new Prometheus or host has none for half of `growth_window`. |
| process counts fresh | Every macOS host (`node_uname_info`) among `[resources.prometheus] hosts` has a process count from `scripts/kelpie-process-limit.sh` written in the last 5 minutes. WARN otherwise, with when it was last written: rule 7 ignores an older file, so a stopped script leaves the process limit unwatched (keep the hosts' clocks in sync with Prometheus's). With a custom `processes` query the check judges by that query. Skipped without rule 7, Prometheus, a macOS host or an answer. |
| claude code task lists | Every list in `[adapters.claude_code] task_lists` is readable (only with that section, like the next three). |
| claude code hooks fire | The hook's spool has a line from the last hour, from a hook that can enforce stops (unless `stop_dir = false`). |
| claude code hook registered | `settings.json` next to `tasks_dir` registers the hook for every event, `PreToolUse` included unless `stop_dir = false` (skipped when no Kelpie hook is there: it may be in project settings). |
| claude code stop markers | `stop_dir` is a writable directory, or can be created. |
| github ci `<repo>` | One line per repository of `[connectors.github]` (at most 20): which source CI is read from (statuses and check-runs, or the commit's Actions runs where those answer 403), and the permission a 403 means. Probes with the head of the newest pull request (else the newest workflow run; skipped if there is none). `WARN` when commit statuses are refused (Actions cannot see Jenkins, Buildkite or CircleCI). Fails when the Actions runs are refused (`Actions: read`), a rejected token, an organization policy (SAML, IP allow list) or an unknown repository. With `ci_workflows`, a further line `github ci_workflows <repo>` fails for an entry that matches no workflow (or only a disabled one) and warns for a name shared by several files. Only with that section. |
| github pull requests `<repo>` | Fails with `Pull requests: read` when the pull requests answer 403: conditions read the PR first. Shown only then. |
| Prometheus reachable | Prometheus answers on `/-/ready` (`--prometheus URL` or `$KELPIE_PROMETHEUS_URL`, else `localhost:9090` is tried and skipped if absent). |

"hooks fire", "claude code hooks fire" and "OTLP spans arrive" are skipped while no task is
running (in progress, on Claude Code); run doctor while one is. Run it in the directory and with the `KELPIE_DB` that `kelpie serve` uses, because it
reads the span store from there. In the compose stack:

```bash
docker compose exec kelpie kelpie doctor --prometheus http://prometheus:9090
```

## Ports and endpoints

| Port | Path | What | Authentication |
| --- | --- | --- | --- |
| 9464 | `GET /metrics` | Prometheus metrics of Kelpie | none |
| 9464 | `GET /healthz` | `ok` while the server runs | none |
| 9464 | `POST /v1/traces` | OTLP/HTTP trace ingest (protobuf or JSON, optionally gzip; no gRPC); off with `[hub]` | bearer token, see below |
| 9465 | `POST /v1/agent/*`, `/v1/traces` | Hub for `kelpie agent` (only with `[hub]`; HTTPS, or plain HTTP on a private address with `allow_plain_http`) | per-agent token |
| 4318 (agent machine, loopback) | `POST /v1/traces` | `kelpie agent`'s OTLP relay | none (loopback only) |
| 9090 | | Prometheus (compose) | none |
| 3000 | | Grafana (compose) | Grafana login |

`/metrics` and `/healthz` have no authentication. Keep the port on a loopback or private
address, or put an authenticating proxy in front of it (see [SECURITY.md](../SECURITY.md)).

## OTLP token and the exposure check

Trace ingest is on by default (`[traces] enabled = true`).

- When `$KELPIE_TRACES_TOKEN` is set (or the variable named by `[traces] token_env`),
  exporters must send `Authorization: Bearer <token>`. Headers and the token are checked
  before the body is read.
- When `token_env` is written in the config, the variable must be set, or Kelpie refuses to
  start.
- Without a token, the endpoint accepts spans from anyone who can reach it, and Kelpie logs a
  warning.

**Exposure check.** `kelpie serve` refuses to start when all of these hold: traces are
enabled, no token is set, the bind address is not loopback (`127.0.0.1`, `::1`,
`localhost`), and neither `[traces] allow_unauthenticated = true` nor
`KELPIE_TRACES_ALLOW_UNAUTHENTICATED=1` is set. Set one of those only when something else
already restricts access, such as a port published on `127.0.0.1` or a proxy that
authenticates. Disabling `[traces]` also passes the check.

```bash
export KELPIE_TRACES_TOKEN=$(openssl rand -hex 24)
uv run kelpie serve --host 0.0.0.0
```

## Where the data lives

Kelpie keeps its state in one SQLite database.

| Path | Set by | Default |
| --- | --- | --- |
| Database | `$KELPIE_DB`, else `[storage] path` | `kelpie.db` in the working directory; `/data/kelpie.db` in the compose stack (with a plain `docker run`, set `KELPIE_DB=/data/kelpie.db` and mount a volume at `/data`: the image's `/app` is not writable by uid 10001) |
| Report files | `[report] dir` | none: `kelpie report` prints to stdout only |
| Hermes spool | `KELPIE_HERMES_SPOOL` on the Hermes host | `~/.hermes/kelpie/spool.jsonl`, rotated to `.1` at 20 MiB |

The database runs in WAL mode, so `kelpie.db-wal` and `kelpie.db-shm` appear next to it. It
holds the action ledger, open alert states, stored spans, the session-owner and task-label
memory used by budgets, and recorded fixes. `[storage] path = ":memory:"` keeps the ledger
and spans in memory: cooldowns and alert states are then lost on restart, and
`kelpie status`, `kelpie doctor` and `kelpie report` cannot read them.

`kelpie serve` creates the database and upgrades its schema on start. `kelpie status`,
`kelpie doctor` and `kelpie report` only read it. If `kelpie status` says the database
predates a feature, run `kelpie serve` once. Spans are pruned after `[traces] retention`
(7 days); the ledger and alert states are not pruned. See [privacy.md](privacy.md).

To copy a live database, use SQLite's online backup (`sqlite3 kelpie.db ".backup copy.db"`)
rather than copying the file.

## Next steps

1. Point Kelpie at your platform (`[adapters.hermes]`) and check it with `kelpie doctor`.
2. Add connectors and alert channels as needed: [configuration.md](configuration.md).
3. Let it run in observe mode for a day, then follow the [act checklist](act-checklist.md).
