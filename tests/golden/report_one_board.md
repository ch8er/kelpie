# Kelpie daily report: 2026-03-02

Cost of chaos: 21.8 hours lost, $1.96 in 680,420 tokens burned + $873.33 in stuck hours. 1,000 tokens of unpriced models not in dollars. 2 tasks completed, $1.34 per completed task.

Biggest wait: developer tasks spent 14.0 task-hours waiting for an unknown cause (no condition registered) (1 task(s)), against 16.0 agent-hours of work. Suggestion: have agents register a condition when they wait (ci_status, human_task, ...), so Kelpie can tell what they wait for and wake them when it holds.

## Cost of chaos

| Cause | Hours lost | Tokens burned | USD | Tasks |
|---|---:|---:|---:|---|
| activity without progress (`no_progress`) | 4.0 | 577,920 | $1.76 | `T-9` |
| exits without a final status (`exit_contract`) | 0.0 | 102,500 | $0.20 | `T-6` |
| blocks that outlived their cause (`stale_blocks`) | 13.8 | 0 | $0.00 | `T-4` |
| ownerless tasks (`orphan_status`) | 4.0 | 0 | $0.00 | `T-3` |

Stuck hours at $40.00/hour: $873.33.

Hours lost: task-hours, how long a task was stuck after Kelpie first reported it, each task-hour counted once: when several rules saw the same task at the same time, under the first of a person's inbox, a stale block, an ownerless task, a wait past its limit, an exit without a final status, review ping-pong, idle capacity, a card stuck before work, a runaway loop, activity without progress (a lower bound); for forgotten holds, idle agent-hours: the agents rule 6 saw idle behind the hold after its expiry, over time; for agents waiting on people, agent-hours: the agent tasks rule 13 saw waiting on a person's approval or human task, over time (a human gate rule 6 also reports counts in both lines).

## Spend

1,201,420 tokens, $2.68; 2 tasks completed (as the board reports them now; adapters only look back so far): `T-1`, `T-2`.
1,000 tokens are of models without a price in [prices] and are not in the dollar figures: `mystery` 1,000.

## Fixed by Kelpie

- `exit_contract`: requeue x1 `T-6`
- `stale_blocks`: unblock x1 `T-7`

Would have done in act mode (observe, suggest or dry-run):

- `orphan_status`: reassign x1 `T-3`

## Needs a human

- **critical** `resources`: open 3.8h since 2026-03-02 20:00 UTC, last seen 2026-03-02 23:50 UTC: resources on farm
- **warning** `stale_blocks` `T-4`: open 13.8h since 2026-03-02 10:00 UTC, last seen 2026-03-02 23:50 UTC: stale_blocks on T-4
- Kelpie could not carry out `wait_condition` wake (failed) x1 `T-8`

## Top 3 recommendations

1. **Reduce blocks that outlived their cause** (13.8 hours lost): check which block reasons Kelpie could not tie to a condition. Tasks: `T-4`.
2. **Reduce activity without progress** (about $1.76/day, 4.0 hours lost): longer heartbeat interval, a cheaper model for maintenance calls, bounded sessions with a handoff file. Tasks: `T-9`.
3. **Let orphan_status act** (4.0 hours lost): in observe mode it would have done reassign on 1 subject(s); ownerless tasks lasted 4.0 task-hours. Tasks: `T-3`.

## Farm throughput

Per role, for the day. Utilization: agent-hours with a session running over agents x hours (the live `kelpie_role_agents_with_work` counts agents with any open task instead). Hand-offs: sessions that ended with a declared status, and every review session that ended. Mean queue: tasks of the role waiting for an agent (ready, or in progress with no session running), averaged over the day; queue at end: those still waiting at midnight (the live gauge counts ownerless tasks only). Rework: share of the tasks the role worked on that came back to it within the day, counted for the role that redid the work, not the reviewer that sent it back; - for a role that only reviews (the live returns gauge counts returns the same way; its done count goes by the task's role). Reopened: done tasks moved back out of done within the day, over the day's completions (those still done plus those reopened; rule 1's "done" needs evidence).

| Role | Agents | Utilization | Completed | Tasks/hour | Hand-offs/hour | Mean queue | Queue at end | Rework | Reopened | Cost per completed task |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| developer | 1 | 67% | 2 | 0.08 | 0.00 | 0.0 | 0 | 0% (0 return(s)) | 0 of 2 | $0.46 +1,000 unpriced |
| maintenance | 1 | 75% | 0 | 0.00 | 0.00 | 0.0 | 0 | - | - | *none done* |
| reviewer | 1 | 0% | 0 | 0.00 | 0.00 | 0.0 | 0 | - | - | *none done* |

Cost per completed task: all the role's spend of the day (work still in progress, reviews) over the tasks it completed, so it is noisy with one or two completions. *none done*: no task completed; *no spend*: no model calls linked to the role; *unpriced*: only models without a price in [prices]; `+N unpriced`: tokens left out of the dollars; `N plan tokens`: tokens of providers on a subscription per completed task, never dollars.
1,000 tokens of models without a price are not in the dollars.

**Task time by stage** (task-hours within the day, by the task's role):

| Role | Working | Queue | Review | Waiting for conditions | Ownerless |
|---|---:|---:|---:|---:|---:|
| developer | 7.0 | 0.0 | 0.0 | 14.0 | 16.0 |

**Waiting for conditions, by cause** (task-hours, tasks):

- developer: an unknown cause (no condition registered) 14.0 (1)

Stages are rebuilt from the board's current statuses, the sessions and their declared end statuses, and Kelpie's own alert states: the board keeps no status history, so a status change between sessions is placed at the next known change. A task counts from its first known moment of the day, so queue time before its first pickup is left out (a lower bound); the board only looks back so far (Hermes `lookback`), and a session with no end or heartbeat counts until midnight. A task's waiting time counts under what it waits (or last waited) for now: a hold label, a person (rule 13), else the kind of its condition; the part inside an open outage counts under it (a CI outage of rule 9, a block storm of rule 4, a tool failure of rule 10).

## Token optimization

**Prompt cache** per role and model: hit rate is cached over input tokens; a miss is a call that read less than half of the previous prompt from the cache (break: the prefix changed; expired: a pause past the cache lifetime; partial: only a fixed prefix is cached). Missed tokens are priced as uncached minus cached input. Step numbers count a session's calls within the day.

| Role | Model | Calls | Hit rate | Breaks | Expired | Partial | Missed tokens | USD |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| maintenance | `model-large` | 48 | 0% | 0 | 0 | 47 | 564,000 | $1.52 |
| developer | `model-large` | 15 | 75% | 0 | 0 | 0 | 0 | - |
| developer | `mystery` | 1 | 0% | 0 | 0 | 0 | 0 | - |

No call of `mystery` reported a cache read: the provider or its instrumentation may not report cache tokens, so no saving from caching is estimated for it.

**Context growth** per step (input tokens of each model call; compactions left out):

| Role | Sessions | Median steps | Median growth per step | Largest context | Runaway |
|---|---:|---:|---:|---:|---:|
| developer | 2 | 8 | 0 | 50,000 | 0 |
| maintenance | 1 | 48 | 0 | 12,000 | 0 |

**Restarts from zero**: none (no task started over after a session that left it unfinished).

These estimates overlap and are not additive: a runaway session's re-read context includes its cache misses, and huge tool outputs and model fit price some of the same tokens again.

## Context and model fit

**Model fit**: roles with many calls, short outputs and small tool outputs, and the cheapest model the farm already runs that could take them; quality after a switch is checked by task returns and review returns (ping-pong rounds). Short outputs alone do not make work routine: a switch is recommended only for roles named in `routine_roles`.

| Role | Model | Calls | Median output | Cheaper model | USD saved |
|---|---|---:|---:|---|---:|
| maintenance | `model-large` | 48 | 40 | none in the farm | - |

These estimates overlap with each other and with the token optimization and huge tool output sections: they are not additive.

## Before/after of recent fixes

**2026-03-02 12:00 UTC, mode**: orphan_status: observe -\> act (rule orphan_status). After-window: 12.0h. Rows marked *direction only* have fewer than 10 observations on a side or a short after-window: a direction, not a result.

| Metric | Before | After | Change | n before / after |
|---|---:|---:|---:|---:|
| hours lost per day (orphan_status) (direction only) | 4.0 h/day | 0.0 h/day | -100% better | 1 / 0 |
| tokens per day (direction only) | 993,920 | 415,000 | -58% better | 56 / 8 |
| sessions failed or without status (0 of 0 -> 1 of 1) (direction only) | n/a | 100% | n/a | 0 / 1 |
| tasks done per hour with work available (4.0 of 24h -> 12 of 12h with work) (direction only) | 0.2 | 0.2 | -33% worse | 1 / 2 |

## Largest tool outputs

| Role | Command or tool | Calls | Tokens | Per call | Filtered calls |
|---|---|---:|---:|---:|---:|
| developer | `xcodebuild` | 4 | ~160,000 | ~40,000 | 0 |
