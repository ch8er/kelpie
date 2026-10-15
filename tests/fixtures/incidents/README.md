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
incident: 1                  # incident number, or null (foundation rules, field evidence)
title: "short title"
description: >               # 2-5 sentences: what happened and why it hurt throughput
  ...
rule: stale_blocks           # name of the rule under test (see table below and docs/roadmap.md)
config: {}                   # rule thresholds used by this scenario
snapshot:                    # passed verbatim to kelpie.core.serde.snapshot_from_dict
  now: "2026-03-02T14:00:00+00:00"
  owners: [...]              # Owner
  tasks: [...]               # Task (block, returns, files, labels, estimate, ...)
  dependencies: [...]        # Dependency
  conditions: [...]          # Condition
  resources: [...]           # Resource
  sessions: [...]            # Session
  spans: [...]               # Span; an entry may carry the fixture-only `repeat` key (below)
expected_actions:            # must be emitted; subset match, order-insensitive
  - kind: unblock            # ActionKind value
    task_id: T-101           # optional (omit for host/global alerts)
    severity: warning        # optional Severity value
    reason_contains: "..."   # optional substring of Action.reason
    params: {}               # optional; subset of Action.params
not_expected_actions:        # required, at least one item; same shape, none may match
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

Fixture-only `repeat` key (spans only). Long runs of identical spans, such as 900 heartbeat
calls overnight, would make a fixture unreadable. A span entry may instead carry
`repeat: {count: N, every: <duration>}`. `tests/incidents.py` removes the key and expands
the entry into `count` spans before `snapshot_from_dict` sees it: copy `i` has `start`
(and `end`, if set) shifted by `i × every` and `span_id` suffixed with `-<i>`; every other
field is copied unchanged. Example:

```yaml
  spans:
    - {trace_id: TR-1, span_id: SP-HB, name: "execute_tool heartbeat", kind: tool, tool: heartbeat,
       args_hash: h-1, input_tokens: 80000, start: "2026-03-02T00:00:00+00:00",
       end: "2026-03-02T00:00:02+00:00", session_id: S-1, repeat: {count: 900, every: "30s"}}
```

Matching semantics: an expected item matches an emitted `Action` when every key present
in the item agrees with it. `params` is compared as a subset of `Action.params`; list
values in `params` compare order-insensitively (`[T-502, T-501]` equals `[T-501, T-502]`),
everything else by equality.

Fixture conventions:

- **Several fixtures per incident are allowed.** Positive, negative and edge scenarios for
  the same incident share the number and differ by slug, e.g. `02-exit-without-status.yaml`
  and `02-healthy-exit.yaml`. Each still has a unique `id` equal to its file stem.
- **`incident` may be `null`** for foundation rules that are not tied to a numbered
  incident, such as `checkpoints`. Such fixtures still follow `NN-slug.yaml`, e.g.
  `14-checkpoint-commit-reminder.yaml` with `rule: checkpoints` and `incident: null`.
- **Field-evidence fixtures use `field-NN-slug.yaml`** and `incident: null`. They reproduce
  failure modes reported on other farms (see "Field evidence from other farms" in
  `docs/plan.md`) rather than one of the 13 incidents, e.g. `field-01-heartbeat-runaway.yaml`.
  Numbers are their own sequence (`field-01`, `field-02`, ...).
- **Every fixture must have at least one `not_expected_actions` entry.** Each snapshot
  includes a realistic distractor (a healthy task, host or resource that looks similar)
  that the rule must leave alone, and the negative expectation pins that down.

## Incident index

| # | File | Rule | Expected action(s) |
|---|------|------|--------------------|
| 1 | `01-stale-guard-block.yaml` | `stale_blocks` | `unblock` T-101 (CI condition already satisfied) |
| 2 | `02-exit-without-status.yaml` | `exit_contract` | `requeue` T-201 (session ended with no final status), `requeue` T-202 (block reason not allowed) |
| 3 | `03-disk-full.yaml` | `resources` | `alert` critical, `{host: host-a}` |
| 4 | `04-host-overload-heavy-queue.yaml` | `resources` | `alert` warning, `{host: host-a, max_heavy: 1}`; none for healthy host-b or light T-406 |
| 5 | `05-ci-minutes-exhausted.yaml` | `ci_economy` | `alert` critical (CI quota exhausted), `blocked_tasks` listed; no host alert for the healthy disk |
| 6 | `06-llm-quota-and-expired-auth.yaml` | `resources` | `alert` warning (rate limits), `alert` critical (expired auth); none for valid or non-expiring credentials |
| 7 | `07-shared-file-conflicts.yaml` | `return_causes` | `alert` recommendation with `hot_files` (conflicts dominate returns); `comment` on in-progress tasks T-705, T-706 touching them; none for T-707 |
| 8 | `08-block-outlived-cause.yaml` | `stale_blocks` | `unblock` T-801, reason mentions the 3h delay; T-802 (PR not merged) stays blocked |
| 9 | `09-sequential-chain-idle.yaml` | `bottlenecks` | `alert` suggesting parallelization, idle agents and `critical_path_length`; nothing for reviewer work T-906, no `escalate` |
| 10 | `10-reviewer-polls-ci.yaml` | `wait_condition` | `comment` T-1001 (register a condition and exit); no `requeue` |
| 11 | `11-orphan-accepted-task.yaml` | `orphan_status` | `alert` critical `{dependents: 7}`, `reassign` T-1101 |
| 12 | `12-review-ping-pong.yaml` | `ping_pong` | `escalate` T-1201 (9 rounds > 3) |
| 13 | `13-human-tasks-block-agents.yaml` | `needs_you` | `escalate` with `human_tasks` and `blocked_agent_tasks: 5`; T-1308 (nothing blocked behind it) not escalated |
| – | `field-01-heartbeat-runaway.yaml` | `loop_breaker` | `stop` + critical `alert` for the heartbeat loop (about 900 identical calls, about 80k input tokens each) and for the agent with 5 consecutive tool errors; nothing for the healthy agent |
| – | `field-02-idle-context-resend.yaml` | `no_progress` | `alert` with `recommendations: [longer_heartbeat_interval, cheaper_maintenance_model, bounded_session_handoff]` for the idle agent resending a 12k-token context every 5 min; nothing for the productive session |

## Adding a fixture

1. Reuse the incident number for another scenario of a known incident, or pick the next
   number; add a short slug: `NN-slug.yaml` (`field-NN-slug.yaml` for field evidence); set
   `id` to the stem.
2. Describe the real incident in generalized terms; no personal or company data.
3. Build the smallest snapshot that reproduces it. Add at least one distractor that the
   rule must leave alone and list it under `not_expected_actions` (required).
4. Put every threshold the rule needs in `config`, so the scenario does not depend on defaults.
5. Keep `expected_actions` minimal: assert the kind, target and the params that matter;
   use `reason_contains` for a key word rather than the full message.
6. Validate:

   ```sh
   uv run python -c "import glob; from pathlib import Path; from tests.incidents import load; \
     [load(Path(f)) for f in sorted(glob.glob('tests/fixtures/incidents/*.yaml'))]; print('ok')"
   ```

7. Add a row to the table above.
