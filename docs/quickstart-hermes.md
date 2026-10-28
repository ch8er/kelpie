# Quickstart: Kelpie on a Hermes farm (observe only)

First live run, read only: Kelpie reads your Hermes Kanban board and a spool of tool calls,
runs its rules in `observe` mode with global dry-run on, and changes nothing. Of the Hermes
actions only `stop` exists (`loop_breaker` in `act` mode), so nothing else could change the
board anyway.

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
# reviewer_roles = ["reviewer"]   # the roles (profiles, see [adapters.hermes.roles]) that
#                                 # review; set it to get "in review with nothing driving it"
# ignore_tasks = ["t_parked*"]    # Hermes has no labels, so holds cannot skip a parked task
[engine.rules.wait_condition]
session_stale_after = "60m"
```

Rule 3's review stop pattern needs to know which roles review: Hermes roles are profile
names unless `[adapters.hermes.roles]` maps them, so set `reviewer_roles` to the role your
reviewer profiles have, or leave it out to keep the pattern off. Hermes tasks carry no
labels, so hold labels (and recovery labels) never match there: list parked tasks in
`ignore_tasks` and recognise recovery tasks by `recovery_titles`.

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
Otherwise keep the token in one file, so it can be rotated in one place. In
`kelpie.toml`:

```toml
[traces]
token_file = "~/.config/kelpie/traces.env"   # holds KELPIE_TRACES_TOKEN=...
```

and create the file as the user `kelpie serve` runs as (mode 600; the command prints no
token):

```bash
uv run kelpie traces rotate-token
uv run kelpie serve        # 127.0.0.1:9464; POST /v1/traces, /metrics, /healthz
```

Then add an OTLP backend to `~/.hermes/hermes_otel.yaml` (OTLP over HTTP). When Kelpie is
the only OTLP backend there that sets no headers of its own, you can give hermes-otel the
token through the environment, read from the same file, so no profile holds a copy:
OpenTelemetry's Python exporters read `OTEL_EXPORTER_OTLP_TRACES_HEADERS` when a backend
sets no headers. Start the Hermes gateway or dispatcher with only that variable set, read
in a subshell so the token itself is not exported (this needs Hermes to run as the same
user as `kelpie serve`, since the file is mode 600):

```bash
if t=$(. ~/.config/kelpie/traces.env && printf %s "$KELPIE_TRACES_TOKEN") && [ -n "$t" ]; then
  export OTEL_EXPORTER_OTLP_TRACES_HEADERS="Authorization=Bearer%20$t"
else
  echo "no traces token in ~/.config/kelpie/traces.env" >&2
fi
unset t
```

Every OTLP exporter in that process without headers of its own sends this header, and the
agents Hermes starts inherit the variable (an agent that prints its environment prints the
token). If another backend (Langfuse, Phoenix) has no headers set, or your agents run
untrusted tools, give each backend its own `headers` instead and keep the list of files
that hold the token short (`kelpie doctor` lists them). Leave `headers` out of the Kelpie
backend when using the variable:

```yaml
backends:
  - type: otlp
    endpoint: http://127.0.0.1:9464/v1/traces
    metrics: false
    logs: false
```

Whether your hermes-otel version leaves headers to the environment is **unverified**; if
it does not, the backend needs `headers: {Authorization: "Bearer <token>"}` in each
profile's `hermes_otel.yaml`, and a rotation has to update each of them (`kelpie doctor`
lists the files that hold the token). Restart the Hermes gateway or dispatcher afterwards.
The key layout above follows the hermes-otel source; check it against the version you
installed.

**Rotating the token** (for example after it was printed into a tool output): run
`uv run kelpie traces rotate-token` as the user `kelpie serve` runs as, who owns the
file and its folder (`sudo -u <that user>`, never as root on someone else's folder). It
writes a new token and keeps the old one valid for
`[traces] token_grace` (2h), so running sessions keep reporting; restart Hermes so new
sessions start with the new one, and update any profile that holds the token itself.
`kelpie_traces_grace_requests_total` on `/metrics` shows who still uses the old one; when
it stops growing, or at once if the old token must stop working now,
`kelpie traces rotate-token --grace 0s` rotates again and accepts no previous token.
Delete backups of the configs (`*.bak*`): they keep old tokens, and `kelpie doctor` warns
about backups that hold the token or a `KELPIE_TRACES_TOKEN=` line.

## 5. Run and check

```bash
# Kelpie must be running: `uv run kelpie serve` in a second shell (or step 4's shell).
uv run kelpie doctor               # PASS|WARN|FAIL|SKIP per check, "fix:" under FAIL/WARN; exit 1 on FAIL
uv run kelpie status               # rules, their mode, board readable, spool age
curl -s localhost:9464/metrics | grep -E '^kelpie_(dry_run|engine_ticks_total|actions_total|snapshot_spans)'
```

With the config of step 2 alone, `kelpie serve` logs two warnings at start, both expected
in observe mode: `checkpoints makes no WIP commits` (it needs `[connectors.github.pulls]`)
and, without step 4, that OTLP ingest accepts spans without a token (it listens on
`127.0.0.1` only).

`kelpie doctor` checks: config and rules load; each rule's mode is what the config says
(all observe, dry-run on, after this quickstart); the board
is readable; the spool has a line from the last hour; spans arrived in the last hour (SKIP if
none ever did: OTLP is optional); worktrees of live tasks are readable by git; a stop can
end the stopped run's worker on this host (`/proc` on Linux, libproc on macOS; a WARN when
neither, or with `end_worker = false`); block
reasons that name a PR can be checked by `[[block_conditions]]` (a WARN, not a failure,
when not: those blocks would look stuck); Prometheus
answers (`--prometheus URL` or `KELPIE_PROMETHEUS_URL`, otherwise `localhost:9090` is tried
and skipped if absent). It also FAILs `disk watched` until rule 7 reads the disks from
Prometheus (`[resources.prometheus]`, step 6 and install.md, "Host resources"): on a live farm
a disk filled unseen without it (incident 19). "hooks fire" and "OTLP spans arrive" are SKIP while no task is
running; run doctor while one is, to make them count.
Run it in the directory (and with the `KELPIE_DB`) that `kelpie serve` uses: it reads the span
store from there.

## 6. Prometheus and Grafana (docker compose)

```bash
cp .env.example .env     # set GRAFANA_ADMIN_PASSWORD
docker compose up -d
```

This starts Kelpie, Prometheus (`127.0.0.1:9090`, scrapes `kelpie:9464`) and Grafana
(`127.0.0.1:3000`, a Prometheus data source and the **Kelpie** dashboard are provisioned from
`deploy/grafana/dashboards/kelpie.json`; open Dashboards, Kelpie).

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

Disk and memory alerts and a heavy-task limit: point Kelpie at that Prometheus with
`[resources.prometheus]` and `[engine.rules.resources]` (see `kelpie.example.toml` and
[install.md, Host resources](install.md#host-resources-node_exporter-prometheus-and-directory-sizes)).
Without them the `resources` rule has no data and stays silent, and `kelpie doctor` fails
`disk watched`.

Optional: real-time alerts to Telegram with `[[alerts.channels]]` (`type = "telegram"`,
bot token from an environment variable, see `kelpie.example.toml`); `kelpie status` shows the
channels and whether the token is set.

## Where to look

- `kelpie doctor` and `kelpie status`: setup and what is loaded.
- `kelpie why <task>`: why one task is not moving, from the board and `kelpie.db`, read-only
  and offline (`--refresh` also reads GitHub). Exit 3 means the board could not be read.
- `http://127.0.0.1:9464/metrics`: `kelpie_dry_run` (must be 1), `kelpie_actions_total`
  (what rules would do, by rule and outcome), `kelpie_adapter_errors_total`,
  `kelpie_spans_received_total`, `kelpie_snapshot_spans`.
- `http://127.0.0.1:9090` (Prometheus) and `http://127.0.0.1:3000` (Grafana) with the compose stack.
- `kelpie.db` (SQLite): the action ledger of what rules proposed.

When observe has run quietly, move one rule at a time to suggest and act: [act checklist](act-checklist.md).
