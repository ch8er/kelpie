# Checklist: from observe to act on a Hermes or Claude Code farm

Move **one rule at a time**, and only to the next step once the previous one has run
quietly for a day. This is based on what the code does today. Things marked **unverified**
have not been tried against a live farm.

## What can act today

With a Hermes adapter enabled, Kelpie refuses to start with `dry_run = false` if any other
rule is in `act` mode (`_refuse_unimplemented_actions`, `HERMES_ACT_RULES` in
`src/kelpie/app.py`). The Claude Code adapter refuses other actions at runtime
(`UnsupportedAction`), so on Claude Code they become "not executed" alerts.

| Rule | Action | Needs |
| --- | --- | --- |
| `loop_breaker` | `stop`: `hermes kanban block --kind needs_input` on the run Kelpie looked at | `[adapters.hermes]`; `cli` and `cli_timeout` if `hermes` is not on `PATH` |
| `loop_breaker` | `stop` on Claude Code: a stop marker the hook enforces at the session's next tool call | `[adapters.claude_code]` with `stop_dir`; see [Claude Code](#claude-code) below |
| `ci_economy` | `cancel_build` of a superseded GitHub Actions run, by the CI connector | `[connectors.github.ci]` with `allow_cancel = true`, `cancel_repos`, ideally `cancel_token_env` (a separate token with Actions: write on those repos only) |
| `dispatch_gate` | `wait`: `hermes kanban schedule` of a ready task, with its conditions in the reason (**unverified**) | `wait_condition` in `act` too, or parked tasks are never woken (Kelpie warns at startup) |
| `wait_condition` | `wake` of a task Kelpie parked: `hermes kanban unblock` (**unverified**); `comment` (`hermes kanban comment --author kelpie`) | |
| `conflict_warning` | `comment` on both tasks; with `serialize`, `wait` of the later running task pinned to its run (**unverified**) | `wait_condition` in `act` too |

Everything else stays `observe` or `suggest`. That includes `stale_blocks`: its unblock, and
requeue of tasks Kelpie did not park and WIP commit, are not implemented for Hermes. Kelpie
never writes a GitHub PR URL into a Hermes comment or reason: Hermes holds a ready task back
for 24 h after one appears (a URL its worker wrote still does, also after a wake). A wake
(`unblocked`) pages chats subscribed to the task. Parks are signed with a key Kelpie creates
next to its ledger database (`[adapters.hermes] wait_key`): run Kelpie as a user the agents
cannot read it as, or a worker could forge a park that Kelpie then wakes, and keep it on the
persistent volume, or Kelpie no longer recognises its parks (logged; wake them by hand).

## 1. Preconditions

- [ ] `uv run kelpie doctor` has no FAIL, and every WARN and SKIP has a reason you understand
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

## 5. Rollback

- Fastest: `dry_run = true` and restart. Every rule is back to log-only, whatever its mode.
- Per rule: set its `mode` back to `suggest` or `observe` (or remove the line) and restart.
- For `ci_economy`: set `allow_cancel = false`, and revoke the cancel token if in doubt.
- Stopped tasks: `hermes kanban unblock <task>`. Stopped Claude Code sessions: delete the
  marker or type a prompt. A cancelled CI run is re-run from GitHub.
- Check afterwards: `uv run kelpie status` shows each rule's mode, and `kelpie_dry_run` is 1.

## Not verified live

- The Hermes CLI contract: the `hermes kanban block --kind needs_input ...` command, the
  `HERMES_KANBAN_*` variables and the `expected_run_id` race check are written from the Hermes
  source and tested against fakes, not against a running farm. Test step 3 is how you find out.
- GitHub Actions: the run fields Kelpie relies on to call a run superseded (workflow,
  branch, PR, event, re-run attempt, status) and the cancel request itself, including the
  token scopes, have not been tried on a live repository.
- Whether a read-only mount of the Hermes files is enough in Docker (see the quickstart).
