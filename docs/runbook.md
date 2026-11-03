# Runbook: Prometheus alerts about Kelpie

The bundled rules in [`deploy/prometheus/rules/kelpie.yml`](../deploy/prometheus/rules/kelpie.yml)
watch the watchdog: Kelpie down or stuck, a part of the farm it cannot see, and alerts it
could not deliver. Each alert's `runbook_url` points at its section here. Route them
through an Alertmanager of your own to a receiver that does not depend on Kelpie's chat
channels (email, a pager): several of them fire exactly when those channels are the problem.
The [Grafana dashboard](../deploy/grafana/dashboards/kelpie.json) shows the metrics they read.

Two caveats hold for several rules. A counter that appears with its first event (lost and
dropped alerts, refused OTLP requests) is caught by a "new series" branch, which also fires
once for every non-zero series after Prometheus or Kelpie was not scraped for 15 minutes
(a fresh Prometheus, a long outage). And the rules match the scrape job `kelpie` of
`deploy/prometheus/prometheus.yml`: with a job of another name, change `KelpieDown`.

Problems on the farm itself (a full disk, a stuck task, an expired provider key) are
Kelpie's rules, not Prometheus alerts: they reach you through Kelpie's own alerts and the
daily report. Prometheus rules here would only duplicate them. That includes the disk
forecast: rule 7 (`resources`) already alerts on the time to full from the growth rate and
names the directories that grow (incident 19), so there is no Prometheus disk rule.

## KelpieDown

Prometheus has not been able to scrape `kelpie serve` for 2 minutes. Nothing watches the
farm meanwhile. A target removed from the scrape config does not fire it (no `up` series at
all).

1. Is the process running? `docker compose ps kelpie`, `systemctl status kelpie` or
   `launchctl print gui/$(id -u)/<label>`, depending on how you run it.
2. If it restarts in a loop, read its log: a config error names the key
   (`kelpie status` with the same config shows it too).
3. If it runs, check the scrape target in `deploy/prometheus/prometheus.yml` and that
   `curl -s http://127.0.0.1:9464/healthz` answers on its host.

## KelpieNotTicking

`/metrics` answers, but no tick has completed for 10 minutes (20 ticks at the default
`[engine] interval`; with an interval of several minutes, raise the 600 in the rule) and
none raised either: a tick is hung. Rules are not evaluated. The threshold compares
Kelpie's clock with Prometheus': keep both hosts on NTP.

1. Kelpie's log: the last `tick` lines and any `tick failed` traceback.
2. A tick blocked on I/O: an adapter reading a board on a slow or hung filesystem, a
   connector waiting on the network, the ledger database locked by another process.
   `kelpie doctor` checks each of them.
3. Restart `kelpie serve`. If it hangs again at once, run `kelpie status` and
   `kelpie why <task>` (both read-only) to find which part blocks.

## KelpieTicksFailing

Ticks start but every one raises, for 15 minutes (`kelpie_engine_tick_failures_total` grows,
`kelpie_engine_ticks_total` does not). The traceback is in the log as `tick failed`. A
single rule or adapter that raises does not fail a tick (it is counted in
`kelpie_rule_errors_total` or `kelpie_adapter_errors_total` instead), so this is the engine
itself or the ledger: check that the database path is writable and the disk is not full.

## KelpieAgentHostSilent

The hub has had no fresh report from a `kelpie agent` for 5 minutes. Its tasks are left out
of the farm and not evaluated; actions to it wait. The hub has already sent one "silent"
alert through Kelpie's channels; this is the copy that does not depend on them.

1. Is the host up and awake? A laptop that sleeps goes silent on purpose.
2. Is `kelpie agent` running there, and does its log show reports refused (401: token,
   400: version mismatch, a clock-skew refusal)?
3. Can it reach the hub's address? See [multi-host failure modes](multi-host.md#failure-modes).

## KelpieAdapterFailing

An adapter's snapshot (or one board's, with `[[adapters.hermes.boards]]`) raised in more
than half of the ticks for 10 minutes. Kelpie skips it on those ticks, so rules see its
tasks as missing. Read the log (`adapter <name> failed`) and run `kelpie doctor`: usual
causes are a moved or locked board database, a spool file that cannot be read, or a platform
upgrade that changed its schema.

## KelpieConnectorFailing

A connector step has failed for longer than a rule's `max_condition_age` (15 minutes by
default). The conditions it checked are stale: no rule acts on them, waits on them alert as
unchecked and blocks as stale evidence. The log names the error
(`connector <name> failed`). GitHub's own outages do not count: the step serves its last
answers then. Lower the 900 in the rule if you configured a shorter age.

## KelpieSpendGapLarge

For a provider's newest reconciled day, its cost report and what Kelpie priced from traces
differ by at least 25% of the bill and at least $5 (the defaults of
[`[[connectors.cost_reports]]`](configuration.md#connectorscost_reports) `warn_gap` and
`warn_min_usd`; change the rule if you changed them). Dollar budgets of that provider follow
the traces, so they are off by as much. Rule 10 sends the same warning through Kelpie's
channels, with the likely causes:

- traces **lower** than the bill: agents that do not export spans, other users of the same
  key, models without a price, or non-token charges in a bill of everything;
- traces **higher**: prices above the bill's (a discount, batch, credits, a `[prices]`
  entry) or calls of another platform under the provider's `models`.

The daily report's spend section shows the day's figures.

## KelpieOldTracesTokenAfterGrace

Exporters sent a rotated-out traces token within the last 6 hours, no previous token is
accepted any more (`[traces] token_grace` is over), and OTLP requests now get 401: those
exporters lose their spans. Restart the sessions or exporters that still hold the old token
so they read the new one from the token file, and check `kelpie doctor` for copies of the
token that the rotation did not update. See [Traces token](security.md#traces-token).

## KelpieTracesTokenFileUnreadable

`kelpie serve` cannot read or trust `[traces] token_file` and keeps the tokens it read
before: a rotation or revocation has not taken effect. The log says why (owner, mode, a
symlink, a folder others may write to). Give the file to the user `kelpie serve` runs as,
mode 600, in a folder only that user can write.

## KelpieAlertDeliveryFailed

Kelpie gave up delivering an alert to a channel after retrying for `[alerts] retry_horizon`
(15 minutes by default). A human may have missed it. Check the channel's token or webhook
and the chat service's status, and read the log (`delivery failed`). Open alert states are
listed by `kelpie status` and in the daily report.

## KelpieAlertsDropped

Alerts for a channel were dropped before any delivery attempt because its queue was full (a
burst, or a channel that is slow for a long time). Duplicates merged into an alert already
sent are not counted here. Alerts still queued when Kelpie stops are counted after `/metrics`
has stopped answering, so no scrape sees them: they are in the log only. Check the log and, for a
full queue, what produced the burst (`sum by (rule) (increase(kelpie_actions_total[1h]))`).
