# Extending Kelpie

How to add a rule, a platform adapter, a connector and a daily report section. Read
[CLAUDE.md](../CLAUDE.md) for the invariants and [CONTRIBUTING.md](../CONTRIBUTING.md) for
the PR workflow first. What to build, and in what order, is decided in [plan.md](plan.md).

## Layout and import boundaries

```text
src/kelpie/
  core/         model, engine, ports (protocols), serde, storage: platform-agnostic
  rules/        rules: pure functions Snapshot -> [Action]
  adapters/     platform adapters (hermes/)
  connectors/   external conditions and actions (github/, human_tasks, llm_quotas/)
  traces/       OTLP ingest, span mapping, hook spans, forwarding
  resources/    host readings from Prometheus
  alerts/       Telegram and Slack delivery
  report/       daily report and its sections
  app.py        wiring: reads the config, builds adapters, connectors and the engine
  cli.py, server.py, doctor.py, metrics.py, net.py
```

The import boundaries are checked by import-linter (`[tool.importlinter]` in
`pyproject.toml`, run with `uv run lint-imports`) and by `tests/test_architecture.py`.

| Package | Must not import |
| --- | --- |
| `kelpie.core`, `kelpie.rules` | `adapters`, `connectors`, `alerts`, `traces`, `app`, `server`, `cli`, `metrics` |
| `kelpie.traces` | `adapters`, `connectors`, `rules`, `app`, `server`, `cli`, `metrics` |
| `kelpie.resources` | the above plus `traces` |
| `kelpie.connectors` | `adapters`, `rules`, `traces`, `resources`, `app`, `server`, `cli`, `metrics` |
| `kelpie.alerts` | `adapters`, `connectors`, `rules`, `traces`, `resources`, `app`, `server`, `cli`, `metrics` |
| `kelpie.net` | `core`, `rules`, `adapters`, `connectors`, `traces`, `resources`, `app`, `server`, `cli`, `metrics` (keep it free of any `kelpie` import) |

`tests/test_architecture.py` also checks that `core`, `rules`, `traces`, `resources`,
`alerts`, `server.py` and `app.py` import no LLM client. Only `report/` may use one, and
Kelpie must work without it.

Integrations depend on `kelpie.core.ports`, never the other way round. Only `app.py` knows
every concrete class; that is where a new adapter or connector is wired.

## Writing a rule

A rule is a pure function over a `Snapshot`: it reads the snapshot and returns a list of
`Action`s. It does no I/O, reads the time only from `snapshot.now`, uses no randomness and
does not mutate the snapshot. The engine decides what happens to the actions, according to
the rule's mode and the global dry-run.

### 1. Plan it

The rule's name must be in `PLANNED` (`src/kelpie/rules/__init__.py`), which mirrors
[plan.md](plan.md). A registered rule that is not in `PLANNED` stops Kelpie at startup. If
the rule is new, the plan changes first, in its own PR.

### 2. Write the module

Create `src/kelpie/rules/<name>.py`. Every module in the package is imported
automatically, so `@register` is all the wiring a rule needs.

```python
"""Long reviews: a task sits in review longer than ``max_age`` (example)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Snapshot, Status
from kelpie.core.serde import parse_duration
from kelpie.rules import register

NAME = "long_review"


@dataclass(frozen=True, slots=True)
class LongReview:
    max_age: timedelta = timedelta(hours=4)
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is not Status.REVIEW:
                continue
            age = snapshot.now - task.status_since
            if age < self.max_age:
                continue
            actions.append(
                Action(
                    ActionKind.ALERT,
                    NAME,
                    f"Task {task.id} has been in review for {age.total_seconds() / 3600:.1f}h "
                    f"(limit {self.max_age}).",
                    task_id=task.id,
                    severity=Severity.WARNING,
                    state=f"{NAME}:{task.id}",
                    since=task.status_since,
                )
            )
        return actions


@register(NAME)
def build(config: dict[str, Any]) -> LongReview:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "max_age" in config:
        value = parse_duration(config.pop("max_age"))
        if value <= timedelta(0):
            raise ValueError("max_age must be positive")
        kwargs["max_age"] = value
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return LongReview(**kwargs)
```

The factory receives the rule's `[engine.rules.<name>]` table without `mode`. It validates
every key, raises `ValueError` on a bad value or an unknown key (Kelpie then refuses to
start and names the rule), and keeps thresholds as fields with defaults, never as module
globals. Durations go through `parse_duration`.

### 3. Choose the actions

`Action` (`src/kelpie/core/actions.py`) describes a side effect; the rule never carries it
out.

| Kind | Effect in act mode | Carried out by |
| --- | --- | --- |
| `alert` | message to the alert channels | alerter |
| `escalate` | message to the alert channels | alerter |
| `requeue`, `wake` | task back to the ready queue | platform adapter `requeue` |
| `unblock` | clear the block | platform adapter `unblock` |
| `comment` | comment on the task | platform adapter `comment` |
| `wip_commit` | commit and push the worktree | platform adapter `wip_commit` |
| `reassign` | new owner (`params.owner_id`) | adapter with `SupportsReassign` |
| `wait` | park in `waiting` on `params.conditions` | adapter with `SupportsWait` |
| `stop` | stop the session `params.session_id` | adapter with `SupportsStop` |
| `cancel_build` | cancel a CI build | the connector named in `executor` |

| Field | Use |
| --- | --- |
| `reason` | Human-readable text. It ends up in comments, alerts, the ledger and the report: say what happened and why. |
| `task_id` | The task the action is about, if any. Also used for routing. |
| `severity` | `info`, `warning` or `critical`; alert channels filter on it. |
| `executor` | Name of the executor that must carry it out. Empty: route by the task's `source`. |
| `key` | Identity for de-duplication when `params` hold values that change every tick (a percentage, a count). Without it every param counts. |
| `params` | Structured detail for executors, fixtures and the report. |
| `state`, `since` | Mark an alert about a lasting condition. The engine then alerts on transitions only (new, escalated, changed, resolved), measured from `since`. Use a stable `state` per condition. |
| `once` | Carry the action out once per fingerprint within `[engine] once_window`, for comments that must not pile up. |

Modes are not the rule's business: return the same actions in every mode. The engine logs
them in `observe`, sends them as suggestions in `suggest` and executes them in `act`; see
[configuration.md](configuration.md#modes-and-dry-run). The ledger handles cooldowns and
rate limits, so a rule may return the same action on every tick.

Shared helpers for task and session semantics (live sessions, linked conditions, fresh
evidence) are in `src/kelpie/rules/_tasks.py`, `_calls.py` and `_tokens.py`. Use them
instead of redefining "live" or "fresh".

### 4. Add fixtures

Every rule has incident fixtures in `tests/fixtures/incidents/`, with at least one negative
case. The full format is in
[tests/fixtures/incidents/README.md](../tests/fixtures/incidents/README.md). In short:

```yaml
id: "17-long-review"            # equals the file stem
incident: null                  # incident number, or null for field evidence and foundation rules
title: "A review sits for hours"
description: >
  A task waited six hours in review while the reviewer worked on other tasks. A task that
  entered review an hour ago is normal.
rule: long_review
config:                         # every threshold the scenario relies on
  max_age: "4h"
snapshot:                       # passed to kelpie.core.serde.snapshot_from_dict
  now: "2026-03-02T14:00:00+00:00"
  owners:
    - {id: reviewer-1, role: reviewer}
  tasks:
    - {id: T-101, title: "Add retry to the client", status: review,
       status_since: "2026-03-02T08:00:00+00:00", role: reviewer, owner_id: reviewer-1}
    - {id: T-102, title: "Fix a typo", status: review,
       status_since: "2026-03-02T13:00:00+00:00", role: reviewer, owner_id: reviewer-1}
expected_actions:               # each must match an emitted action
  - kind: alert
    task_id: T-101
    severity: warning
    reason_contains: "in review"
not_expected_actions:           # required; none may match
  - kind: alert
    task_id: T-102
```

- Snapshot keys must match the dataclasses in `src/kelpie/core/model.py` exactly; unknown
  keys are rejected. Datetimes carry a timezone offset; durations are strings like
  `"1h30m"`; enums use their lowercase values.
- A match item may set `kind` (required), `task_id`, `severity`, `executor`,
  `reason_contains` (case-insensitive substring) and `params` (a subset of
  `Action.params`; lists compare in any order).
- A span entry may carry `repeat: {count: N, every: "30s"}` to expand into `N` copies,
  for long runs of identical calls.
- Data is synthetic: tasks like `T-101`, agents like `agent-dev-1`, hosts like `host-a`,
  repos like `example/repo`, URLs on `example.org`.
- Add a row to the fixture index in the fixtures README.

`tests/test_incidents.py` loads every fixture with `tests/incidents.py`, checks the schema
and the negative case, builds the rule with `REGISTRY[rule](config)` and matches the
actions. A fixture for a rule that is not registered yet is validated and skipped; it runs
the moment the rule registers.

### 5. Add unit tests and check coverage

Edge cases (thresholds, bad config, empty snapshots) go to `tests/rules/test_<name>.py`.
Build the rule through its factory, so config validation is tested too.

```bash
uv run pytest tests/test_incidents.py tests/rules/test_long_review.py
uv run pytest --cov=kelpie
uv run coverage report --include='src/kelpie/rules/*' --fail-under=90
```

`kelpie.rules` coverage must stay at 90% or more; CI fails below it.

### 6. Document it

Add the rule's parameters to [configuration.md](configuration.md), a commented table to
`kelpie.example.toml`, and the row in the README rules table.

## Writing a platform adapter

A platform adapter connects Kelpie to an agent platform: it describes the board as a
snapshot and carries out task actions. The Hermes adapter
(`src/kelpie/adapters/hermes/`) is the reference.

### Protocols

All in `src/kelpie/core/ports.py`.

| Protocol | Required | Methods |
| --- | --- | --- |
| `PlatformAdapter` | yes | `name`, `list_tasks()`, `get_task(id)`, `requeue(id, reason)`, `unblock(id, reason)`, `comment(id, body)`, `wip_commit(id, message, *, branch, draft_pr)`, `events(since)` |
| `SnapshotSource` | in practice | `snapshot(now) -> Snapshot`: tasks, owners, dependencies, sessions, spans and conditions as of `now`. Only adapters with it contribute to snapshots. |
| `SupportsReassign` | optional | `reassign(task_id, owner_id, reason)` |
| `SupportsWait` | optional | `wait(task_id, conditions, reason)`: register the conditions and report the task as waiting. |
| `SupportsStop` | optional | `stop(session_id, reason)`: stop or pause a running session; the task keeps its status. |

Capabilities are checked with `isinstance` at runtime. An action that needs a capability
the adapter lacks is not lost: the engine sends it to a human as "not executed". A method
that raises (Hermes raises `NotImplementedError` for actions it does not have yet) is
recorded as `failed`, and a human is told.

### Rules for adapters

- **Status mapping in one place.** Map platform statuses onto `kelpie.core.model.Status`
  and document the mapping (see "What Kelpie sees" in the
  [Hermes adapter README](../src/kelpie/adapters/hermes/README.md#what-kelpie-sees)).
- **`source` routing.** Set `Task.source` to the adapter's `name` on every task. The engine
  routes an action to the adapter named in its task's `source` (or, for `stop`, the task of
  `params.session_id`); with exactly one adapter, everything goes to it. Executor names must
  be unique across adapters and connectors.
- **The waiting contract.** A task Kelpie reports as `waiting` registered a condition and
  exited on purpose. The adapter keeps the platform's dispatcher from declaring it dead
  until Kelpie wakes it.
- **The caller owns the clock.** `snapshot(now)` uses the `now` it is given.
- **Read-only by default.** Hermes opens `kanban.db` read-only and changes the board only
  through the Hermes CLI, for `stop`, re-checking the run before it acts.
- **Agent-host code.** Hooks and plugins that run inside the platform live in the adapter
  package, use the standard library only and import nothing from Kelpie (they run in the
  platform's Python). They never raise, never block on the network and record no raw
  arguments or results; see [privacy.md](privacy.md). Hermes' `plugin.py` writes a spool
  file that the adapter reads.
- **Spans from hooks.** A platform without OpenTelemetry gets spans through
  `kelpie.traces.hooks.spans_from_hook_events`: the adapter turns its hook records into
  `HookEvent`s and gets task, session and tool spans with the same `args_hash` that OTLP
  ingest computes. When the platform also sends OTLP, Kelpie keeps one span per call.

### Wiring

1. Parse `[adapters.<name>]` in `src/kelpie/app.py` (see `_load_hermes`): reject unknown
   keys, validate types, read secrets from environment variables. Add the name to the
   allowed `[adapters]` entries.
2. Build it in `build_adapters`.
3. Optional, for `kelpie status`: a `check()` method that returns a problem string or None,
   a `db_path` attribute and `status_detail(now) -> list[str]`.
4. Check the act-mode guard in `app.py` (`_refuse_unimplemented_actions`,
   `HERMES_ACT_RULES`): Kelpie refuses act-mode rules whose actions the configured
   adapters cannot carry out.

### Testing

Test against synthetic platform data, never a live board: for Hermes, a throwaway
`kanban.db` built in the test. Fake the platform CLI instead of calling it.

## Writing a connector

Connectors bring in external state (PR and CI status, provider quotas, human tasks) and can
carry out actions outside the platform (cancel a CI build). They live in
`src/kelpie/connectors/` and do all the I/O; rules only read the result from the snapshot.

### Shapes

| Shape | Contract | Wired as |
| --- | --- | --- |
| Condition connector | `ConditionConnector`: `name`, `kinds` (the `ConditionKind`s it evaluates), `check(condition) -> Condition` | A snapshot step: a callable `Snapshot -> Snapshot` that returns the snapshot with its conditions checked (and, optionally, new conditions attached) |
| Action connector | `ActionConnector`: `name`, `actions` (the `ActionKind`s it executes), `execute(action) -> ActionResult` | Passed to the engine as an executor |
| Data source | `ResourceReader.read(now)`, `ProviderReader.read(now)`, or a snapshot step that adds `builds` | Called during snapshot assembly |

**Condition results.** Return an updated copy of the condition:

| Field | Meaning |
| --- | --- |
| `satisfied` | `True` holds, `False` does not hold yet, `None` unknown (set `params.error`). |
| `satisfied_at` | When it became true (for example the merge time). |
| `checked_at` | When the source last confirmed the answer. Rules judge freshness by it, so a cached answer keeps its old time. |
| `params.terminal` | `True` when it can never hold (a PR closed without merging). Rules 2 and 4 alert at once. |

**Action results.** `ActionResult.DONE` (or `True`/`None`): carried out. `NOTHING_TO_DO`:
the target no longer needs it. `REFUSED` (or `False`): not allowed by the connector's config,
and a human is told instead. Raise on a transient failure (network, rate limit): the engine
records a failure and the ledger retries after `[engine] retry_after`. Re-check the target
right before a write.

**Rules for connectors.**

- Never raise out of a snapshot step for a single failure; degrade to an unknown result. A
  step that raises anyway is logged, counted in `kelpie_connector_errors_total` and skipped
  for that tick.
- Bound every tick: a request budget and a wall-clock budget per tick, a cache, backoff on
  rate limits.
- Secrets come from environment variables named in the config. Never log them or put them
  in error messages.
- Query only what the config allows (an allow-list of repositories, hosts, providers):
  agents write block reasons, and a reason must not point the token elsewhere.
- Use `kelpie.net` for HTTP with a secret: no redirects, one deadline per request, a size
  cap, safe error details.
- Mock HTTP in tests. No test may need network access or real credentials.

### Example: the GitHub connector

`src/kelpie/connectors/github/`:

| File | Role |
| --- | --- |
| `settings.py` | Parses `[connectors.github]`: repos allow-list, token variable, limits. |
| `client.py` | HTTP client: ETag cache, per-tick request and time budget, rate-limit backoff, no redirects. |
| `connector.py` | `GitHubConnector`, a `ConditionConnector` for `pr_merged` and `ci_status`. Only allow-listed repos are queried; failures become `satisfied = None` with `params.error`. |
| `blocks.py` | `ConditionSync`, the snapshot step: evaluates GitHub conditions and attaches conditions to blocks and ready tasks whose text matches `[[block_conditions]]`. |
| `builds.py` | `GitHubCI`: a snapshot step that adds recent GitHub Actions runs (`Snapshot.builds`) and the CI minutes quota, and an `ActionConnector` for `cancel_build`, off unless `allow_cancel`, with a fresh re-check before each cancel. |

`app.py` wires it in `_github_steps`: the `ConditionSync` and `GitHubCI` steps join the
snapshot steps, and `GitHubCI` joins the engine's connectors when it can cancel. Rule 9
emits `cancel_build` with `executor` set to the build's `source` (`github`), so the engine
routes it to that connector.

The human task connector (`src/kelpie/connectors/human_tasks.py`) is a smaller example: it
reads no service, only the board in the snapshot.

### Wiring

1. Parse the config section in the connector package and add it to `_load_connectors` in
   `app.py` (and to the allowed `[connectors]` entries).
2. Add the snapshot step in `_connector_steps`, and action connectors to the list passed to
   `Engine(connectors=...)`.
3. Show it in `kelpie status` (see `_github_status`) without secrets.
4. Document the section in [configuration.md](configuration.md) and
   `kelpie.example.toml`.

## Adding a daily report section

New analyses join the one daily report as sections instead of starting another report.
Code: `src/kelpie/report/sections/__init__.py`.

### The API

| Name | What it is |
| --- | --- |
| `register_section(name, keys=())` | Decorator that registers a builder under a unique name; `keys` are the `[report.sections.<name>]` keys it reads. |
| `SectionContext` | What a builder may read: `inputs` (the report's `ReportInputs`: ledger entries, alert states, spans, tasks, owners, sessions, prices, fixes, settings), `start` and `end` of the day, the day's `spans`, `counted` (LLM spans whose tokens count, with the model to price them by), `roles` (session id to its owner's role), `price` (token usage to `Spend`), `spend` (the farm's whole day) and `config` (the section's own `[report.sections.<name>]` table, empty without one: only the keys declared with `register_section(name, keys=[...])` reach it, any other key is noted in the report instead; check the values yourself and raise on bad ones, the report notes the failed section; read-only at the top level, nested values are your own copy). |
| `Section(title, lines, advice=())` | The result. `lines` are Markdown rendered under `## <title>` after the top 3; empty `lines` means no heading. |
| `Advice(title, detail, usd=0.0, hours=0.0, tasks=())` | A recommendation worth `usd` and `hours` per day. It competes for the report's top 3 with the built-in ones, ranked the same way. `detail` has no final period. |

A builder returns a `Section`, or `None` when it has nothing to say today.

### Rules for builders

- No I/O, no clock reads (the day ends at `ctx.end`), no LLM.
- The same context gives the same section.
- The context is read-only; do not mutate `ctx.inputs` or its sequences.
- A builder that raises, or returns something other than a valid `Section`, is logged and
  replaced by a one-line note. It never costs the rest of the report.

### Example

Create a module in `src/kelpie/report/sections/`; every module there is imported when the
report is built, and builders run in name order.

```python
"""Spend by role."""

from __future__ import annotations

from collections import defaultdict

from kelpie.core.spans import TokenUsage, usage
from kelpie.report.sections import Section, SectionContext, register_section


@register_section("spend_by_role")
def spend_by_role(ctx: SectionContext) -> Section | None:
    rows: dict[str, list[TokenUsage]] = defaultdict(list)
    for row in usage(ctx.spans, ctx.counted):
        rows[ctx.roles.get(row.session_id or "", "unknown")].append(row)
    if not rows:
        return None
    lines = []
    for role in sorted(rows):
        spend = ctx.price(rows[role])
        lines.append(f"- {role}: {spend.tokens:,} tokens, ${spend.usd:,.2f}")
    return Section("Spend by role", tuple(lines))
```

### Testing

Call `build_report` with your builder only, so other sections do not interfere, and check
the rendered Markdown:

```python
from datetime import UTC, date, datetime

from kelpie.core.model import Owner, Session, Span, SpanKind
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections.spend_by_role import spend_by_role


def test_spend_by_role() -> None:
    span = Span(
        "TR-1",
        "SP-1",
        "chat",
        SpanKind.LLM,
        datetime(2026, 3, 2, 9, tzinfo=UTC),
        session_id="S-1",
        model="model-a",
        input_tokens=1200,
        output_tokens=300,
    )
    inputs = ReportInputs(
        day=date(2026, 3, 2),
        tz=UTC,
        spans=[span],
        sessions=[Session("S-1", "T-101", "agent-dev-1", datetime(2026, 3, 2, 8, tzinfo=UTC))],
        owners=[Owner("agent-dev-1", "developer")],
    )
    report = build_report(inputs, sections={"spend_by_role": spend_by_role})
    assert "- developer: 1,500 tokens" in render_markdown(report)
```

`tests/report/test_sections.py` has more examples: advice in the top 3, a failing builder,
an advice-only section.
