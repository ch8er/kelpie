# Kelpie daily report: 2026-03-02

Cost of chaos: 20.0 hours lost, $2.09 in 680,420 tokens burned + $800.00 in stuck hours. 3 tasks completed, $1.30 per completed task.

Biggest wait: ops tasks spent 14.0 task-hours waiting for an unknown cause (no condition registered) (1 task(s)), against 4.0 agent-hours of work. Suggestion: have agents register a condition when they wait (ci_status, human_task, ...), so Kelpie can tell what they wait for and wake them when it holds.

## Cost of chaos

| Cause | Hours lost | Tokens burned | USD | Tasks |
|---|---:|---:|---:|---|
| activity without progress (`no_progress`) | 4.0 | 577,920 | $1.76 | `svc:t_9` |
| exits without a final status (`exit_contract`) | 0.0 | 102,500 | $0.34 | `app-b:t_3` |
| blocks that outlived their cause (`stale_blocks`) | 10.0 | 0 | $0.00 | `svc:t_9` |
| ownerless tasks (`orphan_status`) | 6.0 | 0 | $0.00 | `app-a:t_2` |

Stuck hours at $40.00/hour: $800.00.

Hours lost: task-hours, how long a task was stuck after Kelpie first reported it, each task-hour counted once: when several rules saw the same task at the same time, under the first of a person's inbox, a stale block, an ownerless task, a wait past its limit, an exit without a final status, review ping-pong, idle capacity, a card stuck before work, a runaway loop, activity without progress (a lower bound); for forgotten holds, idle agent-hours: the agents rule 6 saw idle behind the hold after its expiry, over time; for agents waiting on people, agent-hours: the agent tasks rule 13 saw waiting on a person's approval or human task, over time (a human gate rule 6 also reports counts in both lines).

## Per board

| Board | Hours lost | Tokens burned | Cost of chaos, USD | Tasks done | Biggest wait |
|---|---:|---:|---:|---:|---|
| `app-a` | 4.0 | 0 | $160.00 | 1 (0.04/h) | - |
| `app-b` | 0.0 | 102,500 | $0.34 | 2 (0.08/h) | - |
| `svc` | 14.0 | 577,920 | $561.76 | 0 (0.00/h) | ops: an unknown cause (no condition registered), 14.0 task-h |
| not on one board | 2.0 | 0 | $80.00 | 0 (0.00/h) | - |
| **Farm total** | 20.0 | 680,420 | $802.09 | 3 (0.12/h) | ops: an unknown cause (no condition registered), 14.0 task-h |

Each board counted as the farm is, over its own tasks, sessions and alerts: cost of chaos in tokens burned, plus stuck hours at $40.00/hour; tasks that reached done (per hour of the day); the role and cause its tasks waited longest for. What names no board (a forgotten hold, a person's inbox, spans of no board's session) counts only in the farm total.

## Spend

1,200,420 tokens, $3.89; 3 tasks completed (as the board reports them now; adapters only look back so far): `app-a:t_1`, `app-b:t_1`, `app-b:t_2`.

## Fixed by Kelpie

- `exit_contract`: requeue x1 `app-b:t_3`

## Needs a human

- **warning** `orphan_status` `app-a:t_2`: open 4.0h since 2026-03-02 08:00 UTC, last seen 2026-03-02 12:00 UTC: orphan_status on app-a:t_2
- **warning** `stale_blocks` `svc:t_9`: open 10.0h since 2026-03-02 10:00 UTC, last seen 2026-03-02 20:00 UTC: stale_blocks on svc:t_9
- **warning** `orphan_status`: open 2.0h since 2026-03-02 18:00 UTC, last seen 2026-03-02 20:00 UTC: orphan_status on farm

## Top 3 recommendations

1. **Reduce blocks that outlived their cause** (10.0 hours lost): check which block reasons Kelpie could not tie to a condition. Tasks: `svc:t_9`. *Proposed*: not accepted yet (note it with `kelpie fix --rule stale_blocks "<what changed>"`).
2. **Reduce ownerless tasks** (6.0 hours lost): give ownerless statuses an owner: route them to a role or let orphan_status reassign them. Tasks: `app-a:t_2`. *Proposed*: not accepted yet (note it with `kelpie fix --rule orphan_status "<what changed>"`).
3. **Reduce activity without progress** (about $1.76/day, 4.0 hours lost): longer heartbeat interval, a cheaper model for maintenance calls, bounded sessions with a handoff file. Tasks: `svc:t_9`. *Proposed*: not accepted yet (note it with `kelpie fix --rule no_progress "<what changed>"`).

A *proposed* recommendation has no fix noted for it in the days this report reads: once the change is made, note it with the `kelpie fix` command shown and the report checks its effect before/after. An *accepted* one has a fix noted (`kelpie fix`, a rule's mode change, or an output filter seen in traces), measured under "Before/after of recent fixes".

## Before/after of recent fixes

No fixes on this day or the day before (note one with `kelpie fix`).
