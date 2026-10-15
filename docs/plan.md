# Kelpie: 3-week plan

Goal for the three weeks: a working, self-hosted Kelpie that supervises a real Hermes
farm, applies the MVP rules deterministically, and produces a daily "cost of chaos"
report showing measurable improvement. Published as open source (`kelpie-sre`).

Principles for every week: small PRs, every rule lands with fixture tests, core stays
free of platform and CI imports, no LLM in the control loop, and new actions ship in
dry-run first.

The detailed issue list lives in [roadmap.md](roadmap.md).

## Week 1: foundation and first rules

**Goal:** the control loop runs end to end against Hermes, in dry-run, with rules 1–4.

Deliverables:

- `core/`: domain model, `Snapshot`, rule protocol, actions, engine loop (every N seconds),
  SQLite storage for snapshots/actions/sessions, Slack alerter, config file, `kelpie serve`
  CLI.
- Fixture loader and test harness for `tests/fixtures/incidents/*.yaml` (done in bootstrap).
- `traces/`: OTLP/HTTP ingest following OpenTelemetry GenAI semantic conventions
  (task → session → model/tool call; tokens in/out/cached, duration, outcome), span storage.
- Checkpoints: commit reminder, WIP commit on exit, resume from the last commit.
- `adapters/hermes`: read `~/.hermes/kanban.db`, actions (requeue, unblock, comment,
  wip_commit), plugin on `post_tool_call` and `on_session_end` hooks.
- Rules 1–4: exit contract (incident 2), wait for condition (10), orphan statuses (11),
  stale blocks (1, 8).
- Docker compose skeleton (kelpie, prometheus, grafana), CI (ruff + pytest) (done in bootstrap).

Exit criteria:

- [ ] `uv run kelpie serve` runs the loop against a copy of a real `kanban.db` without errors.
- [ ] Rules 1–4 each pass positive, negative and edge fixtures.
- [ ] Hermes plugin delivers session-end events and spans to Kelpie.
- [ ] Engine in dry-run logs the actions it would take; at least one real incident from the
      farm history is reproduced as a fixture per rule.
- [ ] CI green on `main`; import-boundary check proves core does not import adapters/connectors.

## Week 2: remaining rules, connectors, live run

**Goal:** all control rules run live on the maintainer's own farm.

Deliverables:

- Rules 5–7: ping-pong (12), bottlenecks and idle time by role (9, 13), resources and
  quotas (3, 4, 5, 6).
- Resources via the Prometheus HTTP API (node_exporter metrics); Kelpie's own `/metrics`.
- `connectors/`: GitHub PR/CI, LLM provider quotas and auth expiry, human tasks.
- Condition registration API (agent registers a condition and exits).
- `file_contention` rule design and fixtures (incident 7), implementation if time allows.
- Run on the maintainer's farm: dry-run for 1–2 days, then enable actions rule by rule.

Exit criteria:

- [ ] Rules 1–7 have fixture tests and run in the live loop.
- [ ] At least rules 1, 3 and 4 run with actions enabled on the real farm for 48 h without
      a wrong action (false positives tracked in an issue).
- [ ] Resource alerts fire on a synthetic disk/quota threshold breach.
- [ ] GitHub connector wakes a task when its CI turns green, without in-session polling.
- [ ] Grafana dashboard shows Kelpie metrics and node_exporter metrics.

## Week 3: report, optimization, publication

**Goal:** prove the value with numbers and make the project usable by others.

Deliverables:

- `report/`: daily "cost of chaos" report (idle hours, wasted tokens, returns and blocks by
  cause) and before/after effect of each fix; optional LLM root-cause section.
- Basic token optimization from traces (cache hit ratio, repeated tool calls, oversized
  contexts) surfaced in the report.
- Throughput metrics: lead time, cycle time, WIP, idle time by role.
- Optional Langfuse export.
- Documentation: install guide, configuration reference, writing rules/adapters, fixture
  format.
- Publication: PyPI release `kelpie-sre` 0.1.0a1, container image, announcement.

Exit criteria:

- [ ] Daily report generated automatically for at least 5 consecutive days on the real farm.
- [ ] Report shows before/after numbers for at least three rules.
- [ ] Kelpie works fully with the LLM disabled.
- [ ] A new user can go from `git clone` to a running dry-run stack with `docker compose up`
      by following the docs.
- [ ] `pip install kelpie-sre` installs a working `kelpie` CLI.
