# Contributing to Kelpie

Thanks for helping. Kelpie is pre-alpha, so expect interfaces to move. Small, focused
pull requests are the fastest way to get changes merged.

## Development setup

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/ch8er/kelpie.git
cd kelpie
uv sync            # creates .venv and installs runtime + dev dependencies
```

Before pushing, run the same checks CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run lint-imports                     # import contracts (core must not import integrations)
uv run pytest --cov=kelpie
uv run coverage report --include='src/kelpie/rules/*' --fail-under=90
```

`uv run ruff format .` and `uv run ruff check --fix .` fix most style issues automatically.

## Invariants

These are non-negotiable; reviews will block on them.

1. **`kelpie.core` never imports `kelpie.adapters` or `kelpie.connectors`.** Integrations
   implement the protocols in `kelpie.core.ports` (`PlatformAdapter`, `ConditionConnector`,
   `Alerter`).
2. **No LLM in the control loop.** Decisions are deterministic rules. An LLM may only be
   used for root-cause text in the report, and Kelpie must work without one.
3. **A rule is a pure function over a `Snapshot`.** No I/O, no network, no clock reads
   (use `snapshot.now`), no randomness, no mutation of the snapshot.
4. **Actions only via the adapter.** Rules return `Action`s; the engine executes them
   through a `PlatformAdapter` or `Alerter`.
5. **Every rule has fixture tests.**
6. **No coupling to a specific CI, to iOS, or to a single machine.** CI is just one kind
   of condition; the server and the agent hosts can be different machines.
7. **Python 3.12, uv, ruff, pytest, full type hints.**

## Adding a rule

1. Create `src/kelpie/rules/<rule_name>.py` with a class or object that satisfies
   `kelpie.core.rule.Rule`: a `name: str` and `evaluate(snapshot) -> Sequence[Action]`.
   Keep thresholds (N minutes, K rounds, ...) as constructor parameters with sensible
   defaults, not module globals.
2. Emit `Action`s from `kelpie.core.actions` with a human-readable `reason`. The reason
   ends up in task comments, alerts and the daily report, so say what happened and why.
3. Add fixtures to `tests/fixtures/incidents/`, named after the incident they reproduce,
   e.g. `02-exit-without-status.yaml`. A fixture is a snapshot (loaded with
   `kelpie.core.serde.snapshot_from_dict`; datetimes must be timezone-aware, durations
   like `"1h30m"`) plus the actions you expect. Include at least:
   - a positive case (the incident: the rule fires),
   - a negative case (a healthy look-alike: the rule stays silent),
   - an edge case around the threshold.
4. Register the rule with `@register("<rule_name>")` from `kelpie.rules`. Every fixture
   whose `rule:` equals that name is then run by `tests/test_incidents.py` automatically
   (until then it is validated and skipped). Add `tests/rules/test_<rule_name>.py` for
   edge cases; `kelpie.rules` must stay at >= 90% coverage.
5. Document the rule in the README rules table with its incident numbers.

## Adding a platform adapter

1. Create `src/kelpie/adapters/<platform>/` implementing `kelpie.core.ports.PlatformAdapter`:
   `list_tasks`, `get_task`, `requeue`, `unblock`, `comment`, `wip_commit`, `events`.
2. Map platform statuses onto `kelpie.core.model.Status` in one place, and document the
   mapping.
3. Anything that must run on the agent host (hooks, plugins) lives inside the adapter
   package and only sends events and spans to the Kelpie server.
4. Test against a recorded or synthetic copy of the platform's data (for Hermes, a
   throwaway `kanban.db`), never against a live board.

## Adding a condition connector

1. Create `src/kelpie/connectors/<name>.py` implementing
   `kelpie.core.ports.ConditionConnector`: `name`, the `kinds` it handles, and
   `check(condition) -> Condition` returning an updated copy with `satisfied`,
   `satisfied_at` and `checked_at` filled in.
2. Connectors do the I/O; rules only read the resulting `Condition` from the snapshot.
3. Respect rate limits; cache results and never call an external API from inside a rule.
4. Mock HTTP in tests. No test may require network access or real credentials.

## Pull requests

- One logical change per PR; aim for well under 400 changed lines.
- Link the roadmap issue (`Closes #123`).
- Include tests for behavior changes and keep CI green.
- Update docs (README rules table, `docs/`) when behavior or interfaces change.

## Commit style

Use short, imperative [Conventional Commits](https://www.conventionalcommits.org/)
subjects, scoped by area:

```text
feat(rules): add exit-contract rule
fix(adapters/hermes): map "accepted" status correctly
test(rules): add stale-block fixtures for incident 8
docs: describe fixture format
chore(deploy): pin prometheus image
```

Keep the subject under 72 characters and explain the "why" in the body when it is not
obvious.

## License

By contributing you agree that your contributions are licensed under the Apache License 2.0.
