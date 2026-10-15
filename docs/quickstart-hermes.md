# Quickstart: Kelpie on a Hermes farm (observe only)

First live run, read only: Kelpie reads your Hermes Kanban board and a spool of tool calls,
runs its rules in `observe` mode with global dry-run on, and changes nothing. Hermes actions
are not implemented yet, so it could not change the board anyway.

Things marked **unverified** have not been tried against a live Hermes farm.

## 1. Install (on the Hermes host)

Kelpie must be able to read `kanban.db` and its `-shm` file, so run it on the Hermes host
(or against a mounted copy). Python 3.12 and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/ch8er/kelpie.git
cd kelpie
uv sync
```

Docker instead: see [step 6](#6-prometheus-and-grafana-docker-compose).

## 2. Config

Create `kelpie.toml` in the repository directory (or point `KELPIE_CONFIG` at it). Named
boards live in `~/.hermes/kanban/boards/<slug>/kanban.db`; one section watches one board.

```toml
[engine]
dry_run = true            # nothing leaves Kelpie
default_mode = "observe"  # no rule suggests or acts

[adapters.hermes]
db_path = "~/.hermes/kanban.db"
spool = "~/.hermes/kelpie/spool.jsonl"   # written by the plugin (step 3)

# Hermes tolerates heartbeats up to an hour old; Kelpie's default is 30 minutes.
[engine.rules.exit_contract]
session_stale_after = "60m"
[engine.rules.orphan_status]
session_stale_after = "60m"
[engine.rules.wait_condition]
session_stale_after = "60m"
```

`[traces]` is on by default (OTLP ingest, step 4). Git state of task worktrees is read by
default (`git_state = true`); it needs `git` on `PATH` and the same user as the agents, see
the [Hermes adapter README](../src/kelpie/adapters/hermes/README.md#progress-note-and-git-state).
Set `git_state = false` under `[adapters.hermes]` to turn it off.

## 3. Hooks plugin (tool calls from Hermes)

Install on each Hermes host, from the Kelpie checkout:

```bash
mkdir -p ~/.hermes/plugins/kelpie
cp src/kelpie/adapters/hermes/plugin.py ~/.hermes/plugins/kelpie/__init__.py
cat > ~/.hermes/plugins/kelpie/plugin.yaml <<'EOF'
name: kelpie
version: 0.1.0
description: Records tool-call metadata for Kelpie (hashes and redacted commands only).
EOF
```

Then add `kelpie` to `plugins.enabled` in the Hermes `config.yaml` (or use `hermes plugins`
in your Hermes version) and restart the gateway or dispatcher. Workers that run under
profiles need the plugin in each profile's `HERMES_HOME`; point them all at one spool with
`KELPIE_HERMES_SPOOL`. Raw arguments and results are never recorded (see the adapter README,
"Privacy"); `KELPIE_HERMES_SPOOL_COMMANDS=0` drops even the redacted command prefix.

## 4. OTLP traces (optional)

Skip this if you do not use the hermes-otel plugin; the hooks already give Kelpie tool
calls. When a tool call arrives both ways Kelpie keeps it once (matched by tool call id).
Otherwise start Kelpie with a token:

```bash
export KELPIE_TRACES_TOKEN=$(openssl rand -hex 24)
uv run kelpie serve        # 127.0.0.1:9464; POST /v1/traces, /metrics, /healthz
```

and add an OTLP backend to `~/.hermes/hermes_otel.yaml` (OTLP over HTTP; use the same token):

```yaml
backends:
  - type: otlp
    endpoint: http://127.0.0.1:9464/v1/traces
    headers:
      Authorization: "Bearer <the KELPIE_TRACES_TOKEN value>"
    metrics: false
    logs: false
```

Restart the Hermes gateway or dispatcher afterwards. The key layout above follows the
hermes-otel source; check it against the version you installed.

## 5. Run and check

```bash
# Kelpie must be running: `uv run kelpie serve` in a second shell (or step 4's shell).
uv run kelpie doctor               # PASS|FAIL|SKIP per check, "fix:" under each FAIL; exit 1 on FAIL
uv run kelpie status               # rules, their mode, board readable, spool age
curl -s localhost:9464/metrics | grep -E '^kelpie_(dry_run|engine_ticks_total|actions_total|snapshot_spans)'
```

`kelpie doctor` checks: config and rules load; dry-run on and every rule in observe; the board
is readable; the spool has a line from the last hour; spans arrived in the last hour (SKIP if
none ever did: OTLP is optional); worktrees of live tasks are readable by git; Prometheus
answers (`--prometheus URL` or `KELPIE_PROMETHEUS_URL`, otherwise `localhost:9090` is tried
and skipped if absent). "hooks fire" and "OTLP spans arrive" are SKIP while no task is
running; run doctor while one is, to make them count.
Run it in the directory (and with the `KELPIE_DB`) that `kelpie serve` uses: it reads the span
store from there.

## 6. Prometheus and Grafana (docker compose)

```bash
cp .env.example .env     # set GRAFANA_ADMIN_PASSWORD
docker compose up -d
```

This starts Kelpie, Prometheus (`127.0.0.1:9090`, scrapes `kelpie:9464`) and Grafana
(`127.0.0.1:3000`, a Prometheus data source is provisioned; no dashboards ship yet, so use
Explore with the `kelpie_*` metrics).

The container sees neither `~/.hermes` nor a config. Add a `docker-compose.override.yml`
(**unverified**; adjust paths) and use `/hermes/...` paths in `kelpie.toml`:

```yaml
services:
  kelpie:
    environment:
      KELPIE_CONFIG: /etc/kelpie/kelpie.toml
      KELPIE_TRACES_TOKEN: ${KELPIE_TRACES_TOKEN:-}
    volumes:
      - ./kelpie.toml:/etc/kelpie/kelpie.toml:ro
      - ${HOME}/.hermes:/hermes:ro
```

Notes: the container runs as uid 10001, so that user needs read access to the Hermes files;
SQLite WAL needs `kanban.db-shm` readable, and whether a read-only mount is enough is
unverified (drop `:ro` if `kelpie doctor` cannot open the board). The image has no `git`, so
set `git_state = false`, or derive an image with git (the compose service already has
`init: true`, which git timeouts need). Inside the container the spool and `kanban.db` paths
are the `/hermes/...` ones; hooks keep writing to `~/.hermes/kelpie/spool.jsonl` on the host.

```bash
docker compose exec kelpie kelpie doctor --prometheus http://prometheus:9090
```

Prometheus has a ready `kelpie` scrape job; the `node` job targets `node-exporter:9100`
(`docker compose --profile local-agents up`), otherwise it shows as down, which is harmless.

## Where to look

- `kelpie doctor` and `kelpie status`: setup and what is loaded.
- `http://127.0.0.1:9464/metrics`: `kelpie_dry_run` (must be 1), `kelpie_actions_total`
  (what rules would do, by rule and outcome), `kelpie_adapter_errors_total`,
  `kelpie_spans_received_total`, `kelpie_snapshot_spans`.
- `http://127.0.0.1:9090` (Prometheus) and `http://127.0.0.1:3000` (Grafana) with the compose stack.
- `kelpie.db` (SQLite): the action ledger of what rules proposed.
