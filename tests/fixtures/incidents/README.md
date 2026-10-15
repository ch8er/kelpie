# Incident fixtures

Each YAML file here is a synthetic, generalized reproduction of a real failure mode seen
in an AI agent farm. A fixture pairs a board snapshot with the actions one rule must
(and must not) emit for it. They serve as regression tests for the rule engine and as
executable documentation of why each rule exists.

All data is synthetic: generic task ids (`T-101`), agents (`agent-dev-1`, `reviewer-1`),
humans (`human-owner`), hosts (`host-a`) and repos (`example/repo`).

## File schema

```yaml
id: "01-stale-guard-block"   # must equal the filename stem
incident: 1                  # incident number
title: "short title"
description: >               # 2-5 sentences: what happened and why it hurt throughput
  ...
rule: stale_blocks           # name of the rule under test (see table below)
config: {}                   # rule thresholds used by this scenario
snapshot:                    # passed verbatim to kelpie.core.serde.snapshot_from_dict
  now: "2026-03-02T14:00:00+00:00"
  owners: [...]              # Owner
  tasks: [...]               # Task (block, returns, files, labels, estimate, ...)
  dependencies: [...]        # Dependency
  conditions: [...]          # Condition
  resources: [...]           # Resource
  sessions: [...]            # Session
  spans: [...]               # Span
expected_actions:            # must be emitted; subset match, order-insensitive
  - kind: unblock            # ActionKind value
    task_id: T-101           # optional (omit for host/global alerts)
    severity: warning        # optional Severity value
    reason_contains: "..."   # optional substring of Action.reason
    params: {}               # optional; subset of Action.params
not_expected_actions:        # optional; same item shape, none may match
  - kind: unblock
    task_id: T-102
```

Snapshot rules (enforced by `snapshot_from_dict`):

- Unknown fields are rejected, so keys must match the dataclasses in
  `src/kelpie/core/model.py` exactly.
- Datetimes are ISO-8601 with a timezone offset, e.g. `2026-03-02T10:00:00+00:00`.
- Durations are strings like `"1h30m"`, `"2h"`, `"30m"`, `"1d"`.
- Enums use their lowercase values (`in_progress`, `ci_status`, `disk`, `tool`, ...).
- Empty snapshot lists are omitted.

Matching semantics: an expected item matches an emitted `Action` when every key present
in the item agrees with it (`params` is compared as a subset; list values in `params`
compare order-insensitively). Fixtures include distractor tasks (healthy tasks that look
similar) and list them under `not_expected_actions`.

## Incident index

| # | File | Rule | Expected action(s) |
|---|------|------|--------------------|
| 1 | `01-stale-guard-block.yaml` | `stale_blocks` | `unblock` T-101 (CI condition already satisfied) |
| 2 | `02-exit-without-status.yaml` | `exit_contract` | `requeue` T-201 (session ended with no final status), `requeue` T-202 (block reason not allowed) |
| 3 | `03-disk-full.yaml` | `resources` | `alert` critical, `{host: host-a}` |
| 4 | `04-host-overload-heavy-queue.yaml` | `resources` | `alert` warning, `{host: host-a, max_heavy: 1}` |
| 5 | `05-ci-minutes-exhausted.yaml` | `resources` | `alert` critical, `blocked_tasks` listed |
| 6 | `06-llm-quota-and-expired-auth.yaml` | `resources` | `alert` warning (rate limits), `alert` critical (expired auth) |
| 7 | `07-shared-file-conflicts.yaml` | `file_contention` | `alert` with `hot_files`; `comment` on in-progress tasks touching them |
| 8 | `08-block-outlived-cause.yaml` | `stale_blocks` | `unblock` T-801, reason mentions the 3h delay |
| 9 | `09-sequential-chain-idle.yaml` | `bottlenecks` | `alert` suggesting parallelization, idle agents and `critical_path_length` |
| 10 | `10-reviewer-polls-ci.yaml` | `wait_condition` | `comment` T-1001 (register a condition and exit); no `requeue` |
| 11 | `11-orphan-accepted-task.yaml` | `orphan_status` | `alert` critical `{dependents: 7}`, `reassign` T-1101 |
| 12 | `12-review-ping-pong.yaml` | `ping_pong` | `escalate` T-1201 (9 rounds > 3) |
| 13 | `13-human-tasks-block-agents.yaml` | `bottlenecks` | `escalate` with `human_tasks` and `blocked_agent_tasks: 5` |

## Adding a fixture

1. Pick the next number and a short slug: `NN-slug.yaml`; set `id` to the stem.
2. Describe the real incident in generalized terms; no personal or company data.
3. Build the smallest snapshot that reproduces it. Add at least one distractor that the
   rule must leave alone and list it under `not_expected_actions`.
4. Put every threshold the rule needs in `config`, so the scenario does not depend on defaults.
5. Keep `expected_actions` minimal: assert the kind, target and the params that matter;
   use `reason_contains` for a key word rather than the full message.
6. Validate:

   ```sh
   uv run python -c "import yaml,glob; from kelpie.core.serde import snapshot_from_dict; \
     [snapshot_from_dict(yaml.safe_load(open(f))['snapshot']) for f in sorted(glob.glob('tests/fixtures/incidents/*.yaml'))]; print('ok')"
   ```

7. Add a row to the table above.
