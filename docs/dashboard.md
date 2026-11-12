# The Grafana dashboard

`kelpie serve` exposes Prometheus metrics on `/metrics` (`127.0.0.1:9464` by default).
The **Kelpie** dashboard ([`deploy/grafana/dashboards/kelpie.json`](../deploy/grafana/dashboards/kelpie.json))
shows all of them, and the alert rules in
[`deploy/prometheus/rules/kelpie.yml`](../deploy/prometheus/rules/kelpie.yml) watch Kelpie
itself ([runbook](runbook.md)). With docker compose both are provisioned for you
([install](install.md#option-2-docker-compose)); this page is for running them without it.

## What is on it

| Row | Shows |
| --- | --- |
| Health | ledger, dry run, last tick age, budget data stale, span store errors, ticks and their duration, adapter, rule and connector errors |
| Hosts | Kelpie's version and uptime; with `kelpie agent` hosts, which are up and how old their last report is |
| Actions, Open states | actions per rule, kind and outcome; the loaded rules and their modes; open alert states |
| Tasks & agents, Throughput | tasks by status and stage, idle agents and idle agent-hours per role, cards before work, done per hour, returns and reopened tasks per role |
| Budgets & spend, Spend check | spend, remaining budget, burn rate and run-out forecasts; the providers' bills against the spend priced from traces |
| Traces ingest, Token rotation | OTLP requests and spans, the span store, forwarding; the traces token file and requests on a rotated-out token |
| Connectors, LLM providers | GitHub requests and rate limit, block conditions, how long a connector has failed; provider reachability, quota and limit hits |
| Resources, Builds & disk | reads of host resources; tool slots, toolchain versions, processes per user, the sizes and growth of watched directories (the textfile scripts in `scripts/`) |
| Alerts, Chat controls | alerts delivered, lost and dropped; snooze buttons pressed and ignored, chat API errors |

Rows of features you have not configured stay empty. The variables at the top filter by
**Board** (with `[[adapters.hermes.boards]]`), **Agent host** (multi-host) and **Node**
(node_exporter targets, for the textfile metrics); All matches farms without those labels.

**Traces in Langfuse.** When `[[traces.forward]]` sends spans to Langfuse, set the
**Langfuse traces URL** variable to your project's traces page (for example
`https://langfuse.example/project/<id>/traces`). The dashboard's "Traces in Langfuse"
link and the trace panels' links (spans received, spans in the snapshot, forwarding,
unattributed tokens) then open it in a new tab. The variable is empty by default, so the
dashboard assumes no Langfuse host. Set it for one view in the variable bar, keep it in a
bookmark with `&var-langfuse=<url>`, or make it the default by editing the variable's
`query` and `current` in `deploy/grafana/dashboards/kelpie.json`. The provisioned
dashboard cannot be saved from the UI.

## Without docker compose

Four things differ from the compose setup: Prometheus scrapes Kelpie at `127.0.0.1:9464`
rather than `kelpie:9464` and reads the rules from the clone rather than
`/etc/prometheus/rules/`, Grafana's data source points at `http://127.0.0.1:9090` rather
than `http://prometheus:9090`, and the dashboard is loaded from wherever you keep the
repository rather than `/var/lib/grafana/dashboards`. Make local copies with those changes
and leave the committed files as they are. Everything stays on `127.0.0.1`.

### macOS with Homebrew

```bash
brew install prometheus grafana
cd ~/src/kelpie                         # your clone
KELPIE_MON=~/.config/kelpie-monitoring  # local copies, outside the clone
mkdir -p "$KELPIE_MON"/grafana/{datasources,dashboards}

# Prometheus: Kelpie on 127.0.0.1, the rules from the clone.
sed -e 's|"kelpie:9464"|"127.0.0.1:9464"|' \
    -e "s|/etc/prometheus/rules/|$PWD/deploy/prometheus/rules/|" \
    deploy/prometheus/prometheus.yml > "$KELPIE_MON/prometheus.yml"
# Remove or edit the `node` job if node_exporter does not run on node-exporter:9100.

# Grafana provisioning: the data source on 127.0.0.1, the dashboard from the clone.
sed 's|http://prometheus:9090|http://127.0.0.1:9090|' \
    deploy/grafana/provisioning/datasources/prometheus.yml \
    > "$KELPIE_MON/grafana/datasources/prometheus.yml"
sed "s|/var/lib/grafana/dashboards|$PWD/deploy/grafana/dashboards|" \
    deploy/grafana/provisioning/dashboards/kelpie.yml \
    > "$KELPIE_MON/grafana/dashboards/kelpie.yml"

# The admin password lives in a file only you can read, never in a committed file.
umask 077 && openssl rand -base64 24 > "$KELPIE_MON/grafana-admin-password"
```

Point Homebrew's Prometheus at the copy. Its flags are in
`$(brew --prefix)/etc/prometheus.args` (the service splits it on whitespace, so the paths
must not contain spaces):

```text
--config.file=/Users/you/.config/kelpie-monitoring/prometheus.yml
--web.listen-address=127.0.0.1:9090
--storage.tsdb.path=/opt/homebrew/var/prometheus
--storage.tsdb.retention.size=2GB
```

(`/usr/local/var/prometheus` on an Intel Mac: keep the path the file already has.)

Then set these in Homebrew's Grafana config, `$(brew --prefix)/etc/grafana/grafana.ini`
(write the paths out in full: the file does not expand `~`). `$__file{...}` reads the
password from the file when Grafana starts:

```ini
[paths]
provisioning = /Users/you/.config/kelpie-monitoring/grafana

[server]
http_addr = 127.0.0.1
http_port = 3000

[security]
admin_user = admin
admin_password = $__file{/Users/you/.config/kelpie-monitoring/grafana-admin-password}

[auth.anonymous]
enabled = false
```

A `provisioning` folder needs both subfolders, `datasources` and `dashboards`, which the
commands above create. Start both services and open <http://127.0.0.1:3000> (Dashboards,
Kelpie):

```bash
brew services restart prometheus
brew services restart grafana
curl -s http://127.0.0.1:9090/api/v1/targets | grep -o '"health":"[a-z]*"'   # kelpie: up
```

`admin_password` only takes effect when Grafana creates its database. If Grafana ran
before, change the password in the UI, or reset it in the database the service uses (its
data path is set on the service's command line, not in `grafana.ini`):

```bash
grafana cli --homepath "$(brew --prefix)/opt/grafana/share/grafana" \
  --config "$(brew --prefix)/etc/grafana/grafana.ini" \
  --configOverrides cfg:default.paths.data="$(brew --prefix)/var/lib/grafana" \
  admin reset-admin-password --password-from-stdin < "$KELPIE_MON/grafana-admin-password"
```

### Linux packages

The same copies work with the distribution's or Grafana's own packages: set the Prometheus
flags in its service's arguments (`/etc/default/prometheus` on Debian and Ubuntu) and the
`[paths]`, `[server]`, `[security]` and `[auth.anonymous]` settings in
`/etc/grafana/grafana.ini`. Grafana also reads them from the environment
(`GF_SERVER_HTTP_ADDR=127.0.0.1`, `GF_AUTH_ANONYMOUS_ENABLED=false`,
`GF_PATHS_PROVISIONING=...`, `GF_SECURITY_ADMIN_PASSWORD__FILE=/path/to/file`), for example
in a systemd drop-in. Keep the password file mode 600 and owned by the user Grafana runs
as.

### Exposing it

Both listen on `127.0.0.1` only. To reach them from another machine, use an SSH tunnel
(`ssh -L 3000:127.0.0.1:3000 monitoring-host`) or put an authenticating reverse proxy with
TLS in front; see [security](security.md). Never publish Prometheus without authentication:
its API reads every metric, including task and budget names.

## The screenshot

The screenshot is the dashboard over the simulated farm's day from `kelpie demo`: synthetic
tasks, agents and repository only. It is made with a throwaway Prometheus and Grafana on
ports of their own (9091 and 3001), so a Prometheus or Grafana you already run, with your
farm's data, is never the one captured. You need Prometheus (with `promtool`), Grafana
and Playwright for Python:

```bash
cd ~/src/kelpie
WORK=$(mktemp -d) && mkdir -p "$WORK"/provisioning/{datasources,dashboards}
uv run python scripts/demo_metrics.py "$WORK/demo.om"     # the demo's day as metrics
promtool tsdb create-blocks-from openmetrics "$WORK/demo.om" "$WORK/data"
printf 'global:\n  scrape_interval: 30s\nscrape_configs: []\n' > "$WORK/prometheus.yml"
prometheus --config.file="$WORK/prometheus.yml" --storage.tsdb.path="$WORK/data" \
  --storage.tsdb.retention.time=10y --web.listen-address=127.0.0.1:9091 &
sleep 3 && curl -sf http://127.0.0.1:9091/-/ready     # must answer before going on

sed 's|http://prometheus:9090|http://127.0.0.1:9091|' \
  deploy/grafana/provisioning/datasources/prometheus.yml > "$WORK/provisioning/datasources/p.yml"
sed "s|/var/lib/grafana/dashboards|$PWD/deploy/grafana/dashboards|" \
  deploy/grafana/provisioning/dashboards/kelpie.yml > "$WORK/provisioning/dashboards/k.yml"
(umask 077 && openssl rand -base64 24 > "$WORK/password")
GF_PATHS_DATA="$WORK/grafana" GF_PATHS_PROVISIONING="$WORK/provisioning" \
GF_SERVER_HTTP_ADDR=127.0.0.1 GF_SERVER_HTTP_PORT=3001 GF_AUTH_ANONYMOUS_ENABLED=false \
GF_SECURITY_ADMIN_PASSWORD__FILE="$WORK/password" \
  grafana server --homepath "$(brew --prefix)/opt/grafana/share/grafana" &
# On Linux: --homepath /usr/share/grafana (packages) or the unpacked release folder.
```

Then log in and capture the dashboard over the demo's day (8:00 to 18:00 UTC on 3 March
2026), with `GRAFANA_ADMIN_PASSWORD="$(cat "$WORK/password")"`:

```python
from playwright.sync_api import sync_playwright
import os

URL = "http://127.0.0.1:3001"
DAY = "from=2026-03-03T08:00:00.000Z&to=2026-03-03T18:00:00.000Z&timezone=utc"
with sync_playwright() as p:
    # Grafana scrolls inside the page: a tall viewport rather than full_page.
    page = p.chromium.launch().new_page(viewport={"width": 1600, "height": 2400})
    page.goto(f"{URL}/login")
    page.fill("input[name=user]", "admin")
    page.fill("input[name=password]", os.environ["GRAFANA_ADMIN_PASSWORD"])
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url)  # the login request has finished
    page.goto(f"{URL}/d/kelpie-overview/kelpie?orgId=1&{DAY}&kiosk")
    page.wait_for_timeout(8000)  # let the panels query
    page.screenshot(path="docs/assets/dashboard.png")
```

Look at the image before committing it: it must show no host, path or name of a real
farm. Stop both processes (`kill %1 %2`) and remove `$WORK` afterwards.
