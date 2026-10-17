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
  budgets: [...]             # BudgetStatus, as Kelpie computes it from [[budgets]] (rule 10)
  budgets_ok: true           # false: spend could not be read; budgets are the last good ones
  limit_hits: [...]          # LimitHit: provider limits reached
  providers: [...]           # ProviderStatus: LLM provider probes ([connectors.llm_quotas])
  builds: [...]              # Build: recent CI builds, as a CI connector reports them
  pull_requests: [...]       # PullRequest: open PRs, as a code-host connector reports them
  closed_pull_requests: [...]  # PullRequest with closed_at (and merged_at when merged):
                             # PRs closed within the connector's lookback
  pull_requests_complete: true # the code host listed both in full (omitted: no code host)
  output_offenders: [...]    # OutputOffender: repeated huge tool outputs ([traces.outputs])
expected_actions:            # must be emitted; subset match, order-insensitive
  - kind: unblock            # ActionKind value
    task_id: T-101           # optional (omit for host/global alerts)
    severity: warning        # optional Severity value
    executor: ci             # optional; the executor the action is routed to
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
field is copied unchanged. `repeat` must contain exactly `count` (>= 1) and `every`.
Tokens belong on `kind: llm` spans. Example:

```yaml
  spans:
    - {trace_id: TR-1, span_id: SP-HB, name: "execute_tool heartbeat", kind: tool, tool: heartbeat,
       args_hash: h-1, start: "2026-03-02T00:00:00+00:00",
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
| 1 | `01-guard-block-after-new-push.yaml` | `stale_blocks` | `unblock` only the block whose condition was re-checked after it was set and recently; `alert` for the condition the connector stopped checking; nothing for a fresh block with pre-push data |
| 2 | `02-exit-without-status.yaml` | `exit_contract` | `requeue` T-201 (session ended with no final status), `requeue` T-202 (block reason not allowed) |
| 2 | `02-done-without-evidence.yaml` | `exit_contract` | with `done_evidence`: `requeue` T-201 (done with only a note: no tests, CI or live check); nothing for test output, green CI, a person's task or a task done two days ago |
| 2 | `02-healthy-exit.yaml` | `exit_contract` | `requeue` only T-215 (ended an hour ago without status); nothing for a final status, an allowed reason with detail, a block on a registered condition, or an exit still inside the grace period |
| 3 | `03-disk-full.yaml` | `resources` | `alert` critical, `{host: host-a}` |
| 4 | `04-host-overload-heavy-queue.yaml` | `resources` | `alert` warning, `{host: host-a, max_heavy: 1}`; none for healthy host-b or light T-406 |
| 5 | `05-ci-minutes-exhausted.yaml` | `ci_economy` | `alert` critical (CI quota exhausted), `blocked_tasks` listed; no host alert for the healthy disk |
| 5 | `05-superseded-builds.yaml` | `ci_economy` | `cancel_build` for the superseded running cancellable build B-1 (`executor: ci`), `info` waste summary; nothing for the newest build, a superseded build that finished before its successor started, one outside the connector's cancel policy, or a manual run |
| 5 | `05-docs-only-build.yaml` | `ci_economy` | `info` waste summary: docs-only D-1, draft D-3, already verified D-4, WIP D-6 (35 min); nothing for a docs-plus-code build, the same commit built for another trigger, a build outside the window, or the healthy quota |
| 6 | `06-llm-quota-and-expired-auth.yaml` | `resources` | `alert` warning (rate limits), `alert` critical (expired auth); none for valid or non-expiring credentials |
| 6 | `06-subscription-limit-forecast.yaml` | `loop_breaker` | `info` alerts saying "estimate": the next provider limit (`next_limit_at`) and the budget running out within the horizon, for the subscription agent on course to hit its limit; nothing for the slow-burning agent, no `stop` |
| 7 | `07-shared-file-conflicts.yaml` | `return_causes` | `alert` recommendation with `hot_files` (conflicts dominate returns); `comment` on in-progress tasks T-705, T-706 touching them; none for T-707 |
| 7 | `07-disjoint-files.yaml` | `return_causes` | conflicts dominate but share no file (the shared changelog is in `expected_shared`): only the informational `dominant_cause` alert; no hot-file alert, no comment |
| 7 | `07-live-tasks-same-files.yaml` | `conflict_warning` | `comment` T-711 and T-712 naming each other and the shared files; `wait` T-712 (started later) until T-711 is done; nothing for T-713 (only the expected-shared changelog in common) or the finished T-714 |
| 8 | `08-block-outlived-cause.yaml` | `stale_blocks` | `unblock` T-801, reason mentions the 3h delay; T-802 (PR not merged) stays blocked |
| 9 | `09-sequential-chain-idle.yaml` | `bottlenecks` | `alert` suggesting parallelization, idle agents and `critical_path_length`; nothing for reviewer work T-906, no `escalate` |
| 10 | `10-review-queue-bottleneck.yaml` | `bottlenecks` | `alert` for the overloaded reviewer role (4 queued reviews) with the idle developers that its own queue cannot absorb; nothing for the developer role or the review T-1001 already being worked on |
| 10 | `10-reviewer-polls-ci.yaml` | `wait_condition` | `comment` T-1001 (register a condition and exit); no `requeue` |
| 10 | `10-condition-satisfied-wake.yaml` | `wait_condition` | `wake` T-1011 (all conditions hold, freshly checked); `alert` T-1013 (overdue) and T-1014 (waiting on nothing); nothing for a pending condition, stale evidence, or a blocked task |
| 10 | `10-productive-long-session.yaml` | `wait_condition` | `comment` only on the session polling a known CI check; nothing for a busy edit/test session |
| 10 | `10-block-waits-on-pr-merge.yaml` | `stale_blocks` | reviewer self-block "waiting for CI/merge of #N" with a `pr_merged` condition attached from `[[block_conditions]]`: nothing while pending (T-1021), `alert` "limit 8h" when overdue (T-1022); unmatched FIX block 13h old keeps the "no condition" `alert` (T-1023) |
| 10 | `10-block-pr-merged-unblock.yaml` | `stale_blocks` | `unblock` T-1031 once the PR merged; nothing for T-1032 (PR still open) |
| 10 | `10-block-pr-closed-unmerged.yaml` | `stale_blocks` | `alert` T-1051 at once ("closed without merging"); nothing for T-1052 (PR still open) |
| 10 | `10-block-ci-green-unblock.yaml` | `stale_blocks` | `unblock` T-1041 once the head commit is green (`ci_green` → `ci_status`); nothing for T-1042 (CI running) |
| 11 | `11-orphan-accepted-task.yaml` | `orphan_status` | `alert` critical `{dependents: 7}`, `reassign` T-1101 |
| 11 | `11-below-threshold.yaml` | `orphan_status` | `alert` + `reassign` to the idle developer for an ownerless in-progress task; `alert` only for a vanished owner and for a task with no role; nothing below the thresholds, for queues, blocked or done tasks, or a task a live session is working on |
| 11 | `11-review-nobody-driving.yaml` | `orphan_status` | `alert` T-1131 (`pattern: review-undriven`): in review for 4h, owned by its developer, no reviewer, session or open condition; nothing for a reviewer's task, the reviewer queue, a live session, a CI wait, a human owner, a held task or a fresh review (on only with `reviewer_roles`) |
| 11 | `11-successful-run-no-next-action.yaml` | `orphan_status` | `alert` T-1141 (`no-next-action`): its review run declared accepted an hour ago, the task is still in review; `alert` T-1143 (`dependents-not-woken`, opt-in) naming T-1144, T-1145 left in backlog; nothing for a run that just ended, an in-progress task (rule 1's), a run without a status, a declared hand-off, an open parent, a held dependent or woken dependents |
| 11 | `11-blocker-chain-dead-end.yaml` | `orphan_status` | `alert` T-1171 (`dead-end-chain`): T-1172 → T-1171 ends in T-9999, not on the board; nothing for the chain behind the cancelled T-1160 (rule 4 alerts on T-1161 with the stuck tasks), a lone task in front of a dead end, an open chain or a cancelled parent |
| 11 | `11-recovery-loop.yaml` | `orphan_status` | `alert` T-1125 (`recovery-loop`): recovery tasks 3 deep, origin T-1122; `alert` T-1126: two recovery tasks recovering each other; nothing for a single recovery, a staged plan of recovery-labelled steps, or a title that only looks like one |
| 12 | `12-review-ping-pong.yaml` | `ping_pong` | `escalate` T-1201 (9 rounds > 3) |
| 12 | `12-below-threshold.yaml` | `ping_pong` | `escalate` T-1211 to the configured architect at exactly 3 rounds, listing return reasons; nothing for 2 rounds, merge-conflict returns, or a done task |
| 13 | `13-human-tasks-block-agents.yaml` | `needs_you` | `escalate` with `human_tasks` and `blocked_agent_tasks: 5`; T-1308 (nothing blocked behind it) not escalated |
| 13 | `13-condition-block-not-a-question.yaml` | `needs_you` | `escalate` to human-lead with `approvals: [T-1363]` only: a `needs_input` block naming a PR (`condition_reasons`) and one with a linked `pr_merged` condition wait on a condition, not a person |
| 13 | `13-approval-wait.yaml` | `needs_you` | `escalate` to human-owner (approver of the ops role) with approval T-1351, to human-lead (default approver) with T-1352; nothing for a 10-minute-old question or a block waiting for CI |
| 14 | `14-forgotten-merge-hold.yaml` | `bottlenecks` | `alert` idle developers with ready work, likely cause the `merge-hold` hold; `alert` the hold is past its 2h30m expiry, owner named; nothing for a fresh freeze or the busy designer |
| 14 | `14-assigned-ready-idle.yaml` | `bottlenecks` | with `ready_is_queued`: `alert` dev-1 and dev-2 idle while their assigned ready tasks t_1, t_2 wait (cause `dispatch`); nothing for t_4 (its run is live) or the human's task |
| 16 | `16-human-task-gates-chain.yaml` | `bottlenecks` | `alert` 3 of 5 slots busy and only human tasks ready, `human_tasks: [T-1600]`, 28 agent tasks behind; no per-gate `escalate` (rule 13's by default); nothing for the human task with nothing behind it, no parallelization advice |
| 16 | `16-human-task-gates-chain-inbox.yaml` | `needs_you` | `escalate` to human-owner with `human_tasks: [T-1600]`, 28 agent tasks held, 7.0 agent-hours (the chain's head waited 7h); T-1650 (nothing behind it) not listed |
| 17 | `17-foreign-pr-mention.yaml` | `bottlenecks` | the hold on PR #71 (task code T-1701 in its title and branch) holds T-1701 only; T-1702, merely mentioned in the PR's description, is ready work (`likely_cause: dispatch`) |
| 17 | `17-parent-left-open.yaml` | `orphan_status` | `alert` "close it?" on T-1700 (all 3 subtasks done) with `waiting_tasks: [T-1704, T-1705]`, and `close`; nothing for a parent with a subtask in progress or one with an open PR of its own |
| 17 | `17-prework-not-backlog.yaml` | `orphan_status` | `alert` stuck-before-work on t_c200 (25h scheduled, with a dependent), `alert` dependents-not-woken on t_c220 naming only the plain backlog dependent; nothing for a lone scheduled card or the triage dependent |
| 17 | `17-card-stuck-scheduled.yaml` | `orphan_status` | `alert` on T-1750, 21h in pre-work with `waiting_tasks: [T-1751, T-1752]`; nothing for a pre-work card with no dependents, a 2-hour-old one, or a plain backlog card |
| 18 | `18-acceptance-criterion-dropped.yaml` | `exit_contract` | critical `alert` on T-1801: "20 runs in a row, 20 of 20" asked by a person, its PR labelled automerge, the reviewer only confirmed green CI; nothing for a task whose PR is not ready yet, or one where only an agent wrote "20 runs" |
| 18 | `18-criterion-met.yaml` | `exit_contract` | `alert` on T-1812 (done, "check it on a tablet before merge" unanswered); nothing for T-1811, whose developer reported 20/20 |
| 18 | `18-flaky-test-returns.yaml` | `return_causes` | `alert` flaky test `LoginTests.testSessionTimeout` that sent T-1821 and T-1822 back; nothing for a test named by one task only |
| – | `14-checkpoint-commit-reminder.yaml` | `checkpoints` | `comment` T-1401 (no commit for 50 minutes) and T-1402 (25 changed files, over 20); nothing for a session that committed 10 minutes ago or a clean worktree |
| – | `15-checkpoint-wip-commit-on-exit.yaml` | `checkpoints` | `wip_commit` with `[skip ci]` for T-1501 (session ended dirty) and T-1502 (session went silent), resume `comment` T-1501; nothing for a clean exit, a task that is already done, or an old dirty session while a new one is live |
| – | `16-checkpoint-resume-from-commit.yaml` | `checkpoints` | `comment` T-1601 (resume from commit a1b2c3d on task/T-1601); nothing when the previous session committed nothing or the task was never worked on |
| – | `field-01-heartbeat-runaway.yaml` | `loop_breaker` | `stop` + critical `alert` for the heartbeat loop (about 900 identical calls, about 80k input tokens each) and for the agent with 5 consecutive tool errors (`stop` carries `session_id`); nothing for the healthy agent or for the CI-polling reviewer, which is left to rule 2 within the grace period |
| – | `field-01-stopped-runaway-waits-for-a-human.yaml` | `stale_blocks` | `alert` (no condition) for T-1002, a block with the same words an agent could write; nothing for T-1001, the run Kelpie stopped (verified marker, `Block.kelpie: stop`): it waits for a human (rule 13) |
| – | `field-02-idle-context-resend.yaml` | `no_progress` | `alert` with `recommendations: [longer_heartbeat_interval, cheaper_maintenance_model, bounded_session_handoff]` for the idle agent resending a 12k-token context every 5 min; nothing for the productive session |
| – | `field-03-budget-overrun.yaml` | `loop_breaker` | `alert` warning for the agent past the soft threshold; `alert` critical (spend, hard limit) and `stop` for the live session of the agent past the hard threshold; nothing for its ended session, or for the agent under budget |
| – | `field-04-dispatch-red-pr.yaml` | `dispatch_gate` | `wait` T-F401 (its PR's head is red) and T-F402 (parent still in progress); nothing for a merged PR, an unknown or stale PR state, a finished parent, or a task in progress |
| – | `field-05-repeated-huge-outputs.yaml` | `output_offenders` | `comment` T-F502 (the latest offending task still open; T-F503 is done) addressed to human-owner (xcbeautify for `xcodebuild`, 2 days, ~179k tokens); `alert` to whoever owns the ops instructions for `tool:read_file` (no task to comment on) |

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
