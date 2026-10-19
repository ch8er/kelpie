# Checklist: from observe to act on a Hermes or Claude Code farm

Move **one rule at a time**, and only to the next step once the previous one has run
quietly for a day. No rule goes to `act` before it has passed [the gate](#the-gate-observe-before-act).
This is based on what the code does today. Things marked **unverified** have not been tried
against a live farm.

## The gate: observe before act

A rule goes to `act` only when it has been **in `observe` on the farm for at least 24 h, and
longer until it has at least 10 triggers**, and **at least 90 % of all its triggers in that
window are true positives**, as **reviewed by a person**. The gate is per rule and per farm,
and it needs step 1's preconditions (a real `[storage] path`: the triggers are read from the
ledger). A live 24 h review found a rule that would have proposed closing parent tasks whose
PRs had not merged; only reading every trigger catches that.

- [ ] **In observe.** Both `dry_run = true` (ledger outcome `dry_run`) and the rule in
  `observe` with `dry_run = false` (outcome `observed`) count; the ledger stores both with
  `mode = 'observe'`. The window starts again after any change to the rule's config, its
  mode, or its code (a Kelpie upgrade that changes the rule).
- [ ] **At least 10 triggers.** A trigger is one finding, not one tick: one opened state for
  a rule's lasting alerts (its escalations, changes and resolution are the same trigger), one
  ledger fingerprint for its other actions. With fewer than 10 the window grows; it never
  shrinks.
- [ ] **Every trigger reviewed by a person**, who marks it a true positive (an action they
  would have taken themselves at the time of the trigger) or a false positive (wrong task,
  wrong cause, wrong time, or harmless noise). "Probably fine" is not a true positive.
- [ ] **At least 90 % true positives** (at least 9 of 10, 18 of 20, ...). Below that, tune
  or fix the rule and start the window again.
- [ ] **No false positive that would have been destructive** (closing, stopping, committing
  or parking the wrong task), whatever the rate: fix the rule and observe it again.
- [ ] **Write the review down**: rule, window, Kelpie version, number of triggers, true and
  false positives with one line each, the reviewer, the date. A rule without a written
  review stays in `observe`.

The triggers of one rule in a window, from the ledger database (`[storage] path`; times are
UTC, written as `...+00:00`). With Python, for example
`python3 -c 'import sqlite3,sys; [print(*r, sep=" | ") for r in sqlite3.connect(sys.argv[1]).execute(sys.argv[2])]' kelpie.db "<query>"`:

```sql
-- Lasting alerts: one row per opened state.
SELECT id, task_id, opened_at, closed_at, severity, summary
FROM states
WHERE rule = 'loop_breaker' AND mode = 'observe' AND opened_at >= '2026-03-02T09:00:00+00:00'
ORDER BY opened_at;

-- Other actions (and one-off alerts): one row per finding, with how many ticks saw it. An
-- alert on a task while one of the rule's states about it was open belongs to that state.
SELECT fingerprint, MIN(at) AS first, SUM(1 + suppressed) AS sightings, task_id, kind, reason
FROM actions AS a
WHERE rule = 'loop_breaker' AND mode = 'observe' AND outcome IN ('observed', 'dry_run')
  AND at >= '2026-03-02T09:00:00+00:00'
  AND NOT (kind = 'alert' AND (
    reason LIKE 'Still open after %' OR reason LIKE 'Changed: %' OR reason LIKE 'Resolved after %'
    OR EXISTS (SELECT 1 FROM states AS s WHERE s.rule = a.rule AND s.task_id IS a.task_id
               AND a.at >= s.opened_at AND a.at <= COALESCE(s.closed_at, a.at))))
GROUP BY fingerprint ORDER BY first;
```

The evidence for a trigger is its ledger `reason` and `params`. `kelpie why <task>` shows the
task as it is now and its recent ledger entries, not the board as the rule saw it; a
`once` finding already handled before the window does not come back within `once_window`
(7 days).

## What can act today

With a Hermes adapter enabled, Kelpie refuses to start with `dry_run = false` if any other
rule is in `act` mode (`_refuse_unimplemented_actions`, `HERMES_ACT_RULES` in
`src/kelpie/app.py`). The Claude Code and OpenClaw adapters refuse other actions at runtime
(`UnsupportedAction`), so on those platforms they become "not executed" alerts.

| Rule | Action | Needs |
| --- | --- | --- |
| `loop_breaker` | `stop`: `hermes kanban block --kind needs_input` on the run Kelpie looked at, then SIGTERM (SIGKILL after `worker_grace`) to that run's worker processes once their pid and start fingerprint match what Hermes recorded | `[adapters.hermes]`; `cli` and `cli_timeout` if `hermes` is not on `PATH`; Kelpie on the same host, in the same PID namespace and as the same user as the workers (else Kelpie does not signal them, and the alert says why); `end_worker = false` to leave it to the reaper always |
| `loop_breaker` | `stop` on Claude Code: a stop marker the hook enforces at the session's next tool call | `[adapters.claude_code]` with `stop_dir`; see [Claude Code](#claude-code) below |
| `loop_breaker` | `stop` on OpenClaw: a stop marker the plugin enforces in the Gateway, and `openclaw gateway call sessions.abort` for the run in flight | `[adapters.openclaw]` with `stop_dir` and `cli`; see [OpenClaw](#openclaw) below |
| `ci_economy` | `cancel_build` of a superseded GitHub Actions run, by the CI connector | `[connectors.github.ci]` with `allow_cancel = true`, `cancel_repos`, ideally `cancel_token_env` (a separate token with Actions: write on those repos only) |
| `dispatch_gate` | `wait`: `hermes kanban schedule` of a ready task, with its conditions in the reason (**unverified**) | `wait_condition` in `act` too, or parked tasks are never woken (Kelpie warns at startup) |
| `wait_condition` | `wake` of a task Kelpie parked: `hermes kanban unblock` (**unverified**); `comment` (`hermes kanban comment --author kelpie`) | |
| `conflict_warning` | `comment` on both tasks; with `serialize`, `wait` of the later running task pinned to its run (**unverified**) | `wait_condition` in `act` too |

Everything else stays `observe` or `suggest`. That includes `stale_blocks`: its unblock, and
requeue of tasks Kelpie did not park and WIP commit, are not implemented for Hermes. Kelpie
never writes a GitHub PR URL into a Hermes comment or reason: Hermes holds a ready task back
for 24 h after one appears (a URL its worker wrote still does, also after a wake). A wake
(`unblocked`) pages chats subscribed to the task. Parks and stops are signed with a key Kelpie
creates next to its ledger database (`[adapters.hermes] wait_key`): run Kelpie as a user the
agents cannot read it as, or a worker could forge a park that Kelpie then wakes (or pass its
own block off as Kelpie's stop, which rule 4 does not alert on), and keep it on the persistent
volume, or Kelpie no longer recognises its parks (logged; wake them by hand) and treats its
stops as ordinary blocks.

## 1. Preconditions

- [ ] `uv run kelpie doctor` has no FAIL ("disk watched" needs node_exporter behind `[resources.prometheus]`), and every WARN and SKIP has a reason you understand
  (a "PR blocks have conditions" WARN means rules 2 and 4 treat those blocks as stuck), run while a
  task is running so the hooks and spans checks count. Do this **before** you change any
  mode: its "observe only" check FAILs by design once `dry_run` is off or a rule is not in
  `observe`.
- [ ] The ledger is healthy: `kelpie_ledger_up 1` on `/metrics`, and `[storage] path` points
  at a real file (not `:memory:`), so cooldowns and once-actions survive a restart.
- [ ] You know the `dry_run` semantics: `dry_run = true` overrides every rule's mode, so
  nothing leaves Kelpie and actions are logged as `dry_run`. With `dry_run = false` each
  rule runs in its own mode. Keep it `true` while editing modes, and flip it last.
- [ ] No alerter is wired yet. In `suggest` mode an alert or suggestion has nobody to go to:
  the ledger records it as `dropped` and it shows in `kelpie_actions_total` and the logs. Do
  not count on being paged; watch the metrics and the ledger.
- [ ] `[engine]` limits are what you want: `cooldown`, `retry_after`,
  `max_actions_per_task_per_hour`.
- [ ] Run Kelpie as the user (and with the `HERMES_HOME`) that owns the board, so
  `hermes kanban` acts on the board Kelpie reads.

## 2. Suggest first, for a day

With `dry_run = true` a suggestion is only logged as `dry_run`. To see `suggested` outcomes
set `dry_run = false`, put exactly one rule in `suggest` and leave all others in `observe`.
Nothing changes the board in `suggest` mode.

```toml
[engine]
dry_run = false
[engine.rules.loop_breaker]
mode = "suggest"
```

- [ ] Watch for a full day (a full working day of the farm): `kelpie_actions_total` by rule,
  kind, mode and outcome, the **Kelpie** Grafana dashboard, and the `kelpie.db` ledger.
- [ ] Every proposed `stop` should be a run you would have stopped yourself. If you disagree
  with one, tune the thresholds (`identical_calls`, `consecutive_errors`, `poll_hard_limit`,
  per-agent or per-role tables) and watch another day.
- [ ] Leave `stop_on_shared_budgets = false` (the default): hard day or project budgets then
  only alert. Set it to `true` only when you accept that a shared budget can stop runs of
  tasks that did nothing wrong.

## 3. Test one stop on a test board

- [ ] Use a board that holds nothing important (named boards live in
  `~/.hermes/kanban/boards/<slug>/kanban.db`; point `db_path` at it).
- [ ] Set `mode = "act"` for `loop_breaker` only and lower its thresholds enough to trip on a
  task you start on purpose (for example a task that repeats one tool call).
- [ ] Expect: the task moves `running` to `blocked` with kind `needs_input`, a
  `BLOCKED: Stopped by Kelpie: ...` comment names the reason, and the ledger shows
  `executed`. A task that was re-claimed in between is not blocked; the stop fails and is
  recorded as `failed`.
- [ ] Undo it with `hermes kanban unblock <task>`.
- [ ] Only then point `db_path` at the real board.

For `ci_economy`: first run it in `suggest` with a read-only token and compare the proposed
cancellations with the runs you would cancel. Then set `allow_cancel = true` with
`cancel_repos` as short as possible (plus `cancel_workflows` and `cancel_branches` if you can),
and `cancel_token_env`. Kelpie only cancels a run its last scan offered as superseded and a
fresh re-check agrees with, never on the default branch.

## 4. Go live, small

- [ ] The rule passed [the gate](#the-gate-observe-before-act), and its written review is at
  hand.
- [ ] Set `dry_run = false` with exactly one rule in `act` and every other rule in `observe`
  or `suggest`. Do not change `default_mode` to `act`.
- [ ] Stay on it for the first hours. Look at `kelpie_actions_total{outcome="executed"}`,
  `outcome="failed"`, `kelpie_adapter_errors_total`, and the `BLOCKED:` comments on the board.
- [ ] Add the next rule or the next board only after a quiet day.

## Claude Code

The steps above apply, with these differences
([quickstart](quickstart-claude-code.md), [adapter README](../src/kelpie/adapters/claude_code/README.md#stopping-a-session)):

- [ ] Preconditions: Kelpie runs on the agent host as the same user as Claude Code; the hook
  is the current `hook.py` and registered for `PreToolUse` with `"matcher": "*"`;
  `stop_dir` and the sessions' `KELPIE_CLAUDE_STOP_DIR` name the same directory.
  `kelpie doctor` checks all of these (the "claude code" lines).
- [ ] Instead of a test board (step 3), test on a throwaway session: the quickstart's step 7
  writes a marker by hand. Then lower `loop_breaker`'s thresholds and let it stop a session you
  make loop on purpose. Expect the tool call refused with "Stopped by Kelpie: ...", the
  session's `exit_reason` reading `stopped by kelpie`, and the ledger `executed`. A session
  shown as `stop not enforced` means the hook did not see the marker: check the
  preconditions.
- [ ] Undo: type a new prompt in the session, or delete
  `~/.claude/kelpie/stop/<session id>.json`.
- [ ] A stop is cooperative (the next tool call) and lasts for the session only: a headless
  run ends, and a launcher may start the work again.

## OpenClaw

The steps above apply, with these differences
([quickstart](quickstart-openclaw.md), [adapter README](../src/kelpie/adapters/openclaw/README.md#stopping-a-session)):

- [ ] Preconditions: Kelpie runs on the Gateway host as the Gateway's user; the Gateway has
  loaded this Kelpie's copy of the plugin, with conversation access; `stop_dir` and the
  plugin's `stopDir` name the same directory; `cli` runs an `openclaw` that reaches the
  Gateway. `kelpie doctor` checks all of these (the "openclaw" lines).
- [ ] Instead of a test board (step 3), test on a throwaway agent: the quickstart's step 7
  writes a marker by hand. Then lower `loop_breaker`'s thresholds and let it stop a session you
  make loop on purpose. Expect the tool call refused with "Stopped by Kelpie: ...", a
  heartbeat (`openclaw system event --text check --mode now`) refused before any model call, the ledger
  `executed` and the message saying the run was aborted. A session shown as `stop not
  enforced` means the plugin did not see the marker: check the preconditions.
- [ ] Undo: send the session a message from the CLI or the Control UI (the operator), `/new`,
  or delete `~/.openclaw/kelpie/stop/<session id>.json`.
- [ ] A stop lasts for the session id (rollovers carry it) and up to 24 hours; a cron job that
  starts a new session is not held back.

## 5. Rollback

- Fastest: `dry_run = true` and restart. Every rule is back to log-only, whatever its mode.
- Per rule: set its `mode` back to `suggest` or `observe` (or remove the line) and restart.
- For `ci_economy`: set `allow_cancel = false`, and revoke the cancel token if in doubt.
- Stopped tasks: `hermes kanban unblock <task>`. Stopped Claude Code sessions: delete the
  marker or type a prompt. Stopped OpenClaw sessions: delete the marker or message the session
  as the operator. A cancelled CI run is re-run from GitHub.
- Check afterwards: `uv run kelpie status` shows each rule's mode, and `kelpie_dry_run` is 1.

## Not verified live

- The Hermes CLI contract: the `hermes kanban block --kind needs_input ...` command, the
  `HERMES_KANBAN_*` variables and the `expected_run_id` race check are written from the Hermes
  source and tested against fakes, not against a running farm. Test step 3 is how you find out.
- GitHub Actions: the run fields Kelpie relies on to call a run superseded (workflow,
  branch, PR, event, re-run attempt, status) and the cancel request itself, including the
  token scopes, have not been tried on a live repository.
- Whether a read-only mount of the Hermes files is enough in Docker (see the quickstart).
