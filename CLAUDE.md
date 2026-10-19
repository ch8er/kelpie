# CLAUDE.md

Kelpie: SRE and throughput optimization for AI agent farms. A service with deterministic
rules, not an LLM agent.

**[`docs/plan.md`](docs/plan.md) is the single source of truth** for scope, priorities,
the rule list, reporting and what is out of scope. This file only holds invariants,
workflow and commands; it never restates the plan. If anything disagrees with the plan,
the plan wins.

## Layout

```
src/kelpie/
  core/         model, engine, ports (protocols), serde, storage, alerts — platform/CI-agnostic
  rules/        rules: pure functions Snapshot -> [Action]; REGISTRY, PLANNED
  adapters/     platform adapters (hermes/ first)
  connectors/   external-condition connectors (GitHub/CI, LLM quotas, human tasks)
  traces/       OTLP ingest (OpenTelemetry GenAI semconv), storage, export
  report/       daily report — the only place an LLM may be used, optionally
  remote/       multi-host: kelpie agent, the hub of kelpie serve, the wire protocol
  alerts/       real-time alert delivery: one router over channels (Telegram, Slack)
  resources/    host resource readings from Prometheus/node_exporter (optional)
  selftest/     kelpie selftest: live checks of act paths on a throwaway board, mock model
  sim/          a simulated farm (loopback only) for kelpie demo and the e2e tests
  demo/         kelpie demo: the simulated farm's day, narrated
  cli.py, server.py, app.py, doctor.py, why.py, snooze.py
tests/fixtures/incidents/   synthetic incident fixtures (format: README.md there)
```

## Invariants (do not break)

1. `kelpie.core` and `kelpie.rules` never import `kelpie.adapters` or `kelpie.connectors`.
   Integrations implement protocols from `kelpie.core.ports`. Enforced by import-linter
   and `tests/test_architecture.py`.
2. No LLM in the control loop: `core/`, `rules/` and `server.py` never import an LLM client.
3. A rule is a pure function over a `Snapshot`: no I/O, no clock reads (use
   `snapshot.now`), no mutation. Side effects happen only through actions that the engine
   routes to executors (platform adapters, action connectors) or the alerter, subject to
   the rule's mode (observe | suggest | act) and the global dry-run.
4. Every rule has fixture tests in `tests/fixtures/incidents/`, including a negative case
   (`not_expected_actions`). `kelpie.rules` coverage stays >= 90%.
5. No coupling to a specific platform in core, a specific CI, iOS, or a single machine.
   Hosts, repos, providers and CI commands are config data, not code paths.
6. Python 3.12, uv, ruff, pytest, type hints everywhere. Code, comments and docs in English.
   Fixtures are synthetic: no personal data, real hostnames or repo names.

## Workflow

- **One task — one branch, one PR.** Small PRs; commit after every meaningful step.
- `main` is protected: changes land only through PRs, squash merge only.
- Push work in progress to a **draft** PR after every completed step. Drafts do not run CI.
- Mark a PR ready for review only when the step is done and ruff and pytest pass locally.
  That starts CI; later pushes rebuild and cancel the previous run for the branch.
  Docs-only changes (`docs/`, `*.md`) skip lint and test. `ci-ok` is the only required
  check: it fails if a needed job failed or was cancelled.
- Before marking a PR ready, run the `reviewer` subagent (`.claude/agents/reviewer.md`) on
  the diff against `main` and fix its findings.
- Never wait for CI inside a session: push and move on.
- Adding a rule: module in `src/kelpie/rules/` with `@register("<name>")` (auto-imported);
  the name must be in `PLANNED`; fixtures with `rule: <name>` then run automatically;
  add `tests/rules/test_<name>.py` for edge cases.
- Economy: routine work on a smaller model; at most 2–3 parallel sessions (plan, Budget).
- PR, commit and extension conventions: `CONTRIBUTING.md`.
- Backlog: `docs/roadmap.md`; `scripts/create_issues.sh` turns it into GitHub issues.

## Commands

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run lint-imports
uv run pytest --cov=kelpie
uv run coverage report --include='src/kelpie/rules/*' --fail-under=90
uv run kelpie status                # rules that would load, with their mode
uv run kelpie demo --fast           # the simulated farm's day, narrated (offline)
uv run kelpie serve                 # control loop; /metrics and /healthz on :9464
scripts/create_issues.sh --dry-run
```
