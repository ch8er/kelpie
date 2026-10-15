# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What Kelpie is

Open-source SRE and throughput optimization for AI agent farms (Hermes, OpenClaw,
Paperclip, Claude Code, ...). Kelpie is a **service, not an LLM agent**: control is
done by deterministic rules. An LLM is optional and used only to draft root-cause
notes in the daily report.

## Architecture

```
src/kelpie/
  core/         platform- and CI-agnostic: model, engine, ports, serde, storage, alerts
  rules/        control rules: pure functions Snapshot -> [Action]; REGISTRY
  adapters/     PlatformAdapter implementations (hermes/ first)
  connectors/   ConditionConnector implementations (GitHub, LLM quotas, human tasks)
  traces/       OTLP ingest (OpenTelemetry GenAI semconv), storage, Langfuse export
  report/       daily "cost of chaos" report, before/after effect of fixes
  server.py     `kelpie serve`: control loop, /metrics, /healthz, OTLP endpoint
tests/fixtures/incidents/   synthetic incident scenarios: snapshot + expected actions
deploy/         Prometheus and Grafana config for docker-compose.yml
```

- **Model** (`core/model.py`): Task, Status, Owner, Dependency, Condition, Block,
  Resource, Session, Span, Snapshot. Immutable dataclasses.
- **Ports** (`core/ports.py`): `PlatformAdapter` (list_tasks, get_task, requeue, unblock,
  comment, wip_commit, events), `ConditionConnector`, `Alerter`. Core depends on these
  protocols; adapters/connectors implement them (dependency inversion).
- **Engine** (`core/engine.py`): every N seconds build a Snapshot, run all rules,
  dispatch actions through the adapter. Dry-run by default.
- **Resources**: no custom collector. node_exporter + Prometheus on agent hosts;
  Kelpie reads them through the Prometheus HTTP API and exposes its own `/metrics`.
- **Deploy**: `docker compose up` runs kelpie, Prometheus, Grafana. Adapters and
  node_exporter run where the agents run.

## Invariants (do not break)

1. `kelpie.core` (and `kelpie.rules`) never import `kelpie.adapters` or
   `kelpie.connectors`. Enforced by import-linter (`uv run lint-imports`) and
   `tests/test_architecture.py`.
2. No LLM in the control loop. `core/` and `rules/` never import an LLM client.
3. A rule is a pure function over a Snapshot: no I/O, no clock reads (use
   `snapshot.now`), no mutation. Side effects happen only via actions executed by the
   engine through an adapter. Every rule has tests on fixtures in
   `tests/fixtures/incidents/`. `kelpie.rules` coverage must stay >= 90%.
4. No coupling to a specific CI, iOS, or a single machine. Hosts, repos and
   providers are data, not code paths.
5. Python 3.12, uv, ruff, pytest, type hints everywhere.

## Workflow

- Small PRs. Commit after every meaningful step. Do not wait for CI inside a session:
  push and move on (see rule 2 — we practise what we preach).
- Before every PR run the `reviewer` subagent (`.claude/agents/reviewer.md`) on the
  diff against `main` and fix its findings before opening the PR.
- Local checks before pushing:
  ```
  uv sync
  uv run ruff check . && uv run ruff format --check .
  uv run lint-imports
  uv run pytest --cov=kelpie
  uv run coverage report --include='src/kelpie/rules/*' --fail-under=90
  ```
- Adding a rule: add a module in `src/kelpie/rules/`, register it with
  `@register("<name>")`, make sure fixtures with `rule: <name>` pass (they stop
  being skipped automatically), add unit tests for edge cases.
- Adding an incident: see `tests/fixtures/incidents/README.md`. Synthetic data only,
  never personal data, hostnames or repo names from real farms.
- Code, comments and docs in English.
- Plan: `docs/plan.md`; backlog: `docs/roadmap.md` (`scripts/create_issues.sh`
  turns it into GitHub issues).
