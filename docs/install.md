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

The Hermes adapter reads `kanban.db` from the local filesystem, so with Hermes, Kelpie runs
on the Hermes host or against a mounted copy of the Hermes directory.

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
| `kelpie serve [--host H] [--port P]` | Control loop plus HTTP endpoints. Defaults: `$KELPIE_HOST` or `127.0.0.1`, `$KELPIE_PORT` or `9464`. |
| `kelpie status` | Rules with their mode, adapters, traces, connectors, alert channels and budgets. Reads the database without changing it. |
| `kelpie doctor [--prometheus URL]` | Checks a first live run (see below). |
| `kelpie why <task> [--json] [--entries N] [--refresh]` | Why one task is not moving: status and age, owner, labels, sessions, conditions, what it waits for up to the first dead end, the rule findings, open alert states and latest ledger entries. Read-only and offline: `--refresh` also reads network connectors (GitHub). Exit 1: not on the board; 3: an adapter could not read its board. `--json` always prints one object (`schema_version`, or `error`). |
| `kelpie report [--date YYYY-MM-DD] [--send]` | The daily report (see the README, "Daily report"). |
| `kelpie fix "note" [--rule R] [--command PREFIX] [--role ROLE] [--at TIME]` | Records a fix so the daily report measures its effect; `kelpie fix --list [--days N]` lists them. |

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
| `prometheus` | `127.0.0.1:9090` | Scrapes `kelpie:9464` and `node-exporter:9100` (`deploy/prometheus/prometheus.yml`); keeps 30 days. |
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
| observe only | `dry_run = true` and every rule is in `observe`. Fails by design once you leave observe. |
| kanban.db | The Hermes board is readable (skipped without `[adapters.hermes]`). |
| hooks fire | The spool has a line from the last hour. |
| OTLP spans arrive | A span arrived in the last hour (skipped if none ever did: OTLP is optional). |
| worktrees readable | git can read the workspaces of live tasks (skipped with `git_state = false`). |
| PR blocks have conditions | No block reason on the board names a PR (`PR #12`, `owner/repo#12`, a `/pull/12` link, `#12` next to PR or merge wording; `guard:` blocks are left out), or every one that does matches a `[[block_conditions]]` pattern whose `pr` names a PR of `[connectors.github] repos`, as `kelpie serve` resolves it. Otherwise a WARN: those blocks would look like stuck tasks or questions for a person (on a live farm, most of the noise of a 24 h review). |
| claude code task lists | Every list in `[adapters.claude_code] task_lists` is readable (only with that section, like the next three). |
| claude code hooks fire | The hook's spool has a line from the last hour, from a hook that can enforce stops (unless `stop_dir = false`). |
| claude code hook registered | `settings.json` next to `tasks_dir` registers the hook for every event, `PreToolUse` included unless `stop_dir = false` (skipped when no Kelpie hook is there: it may be in project settings). |
| claude code stop markers | `stop_dir` is a writable directory, or can be created. |
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
| 9464 | `POST /v1/traces` | OTLP/HTTP trace ingest (protobuf or JSON, optionally gzip; no gRPC) | bearer token, see below |
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
