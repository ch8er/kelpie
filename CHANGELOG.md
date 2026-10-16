# Changelog

All notable changes to kelpie-sre. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the release process is in [docs/release.md](docs/release.md).

## Unreleased

First public pre-release (planned as 0.1.0a1). Kelpie is pre-alpha: it starts in global
dry-run with every rule in `observe` mode.

- **Control loop** with deterministic rules over a snapshot of the farm (no LLM): commit
  reminders and WIP commits, exit contract, wait conditions, ownerless statuses, stale
  blocks, review ping-pong, bottlenecks and holds, host resources and LLM provider quotas,
  CI economy, loop breaker and budgets, activity without progress, early conflict
  warning, dispatch gate, the "needs you" inbox per person, and repeated huge tool
  outputs. Each rule runs in `observe`, `suggest` or `act` mode.
- **Platforms**: Hermes (kanban board, hooks plugin, CLI actions) and Claude Code (hooks
  and task lists).
- **Traces**: OTLP ingest (OpenTelemetry GenAI conventions) with redaction, span storage,
  token budgets and forwarding.
- **Connectors**: GitHub pull requests and CI, LLM provider quotas, human tasks.
- **Alerts** to Telegram and Slack; Prometheus metrics and a Grafana dashboard.
- **Daily report**: cost of chaos, what Kelpie did or would have done, what needs a
  human, before/after of recorded fixes (`kelpie fix`), token recommendations, and an
  optional LLM root-cause section (off by default).
- `kelpie serve`, `status`, `doctor`, `report`, `fix`, `why`; docker compose stack.
