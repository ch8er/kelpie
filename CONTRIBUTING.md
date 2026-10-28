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
   An alert or escalation also gets its message's own lines in its params,
   `**alert_params(headline, needed, blocking)` from `kelpie.rules._alerts`: a short
   headline (passed whole: the renderer cuts it), and for an alert about one task what a
   person should do (worded so it holds whether or not Kelpie acted; the engine drops it
   once Kelpie carried out what the alert announces) and how many tasks wait behind it,
   counted with `kelpie.rules._chains.behind_lookup` as rules 2, 4 and 13 do.
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
5. Document the rule: add a row with its incident numbers to the rules table in
   [docs/guide.md](docs/guide.md#rules) and a one-line row to the rules table in `README.md`.

## Adding a platform adapter

An adapter turns one agent platform (a board, a database, task files, a hook stream) into
a `Snapshot` that the rules read, and carries out the few actions the platform allows. The
reference, with the protocol table and the wiring rules, is
[docs/extending.md](docs/extending.md#writing-a-platform-adapter); this section is the
how-to and the checklist. Two adapters show the two usual shapes. Every path below is
relative to the repository root, and `hermes/` and `claude_code/` stand for
`src/kelpie/adapters/hermes/` and `src/kelpie/adapters/claude_code/`.

| | Hermes | Claude Code |
| --- | --- | --- |
| Tasks come from | a SQLite Kanban board, opened read-only | task files in `~/.claude/tasks/<list>/` |
| Sessions come from | the board's `task_runs` table plus a spool | a spool written by a command hook |
| Agent-host code | `hermes/plugin.py` (a Hermes plugin) | `claude_code/hook.py` (a command hook) |
| Actions carried out | stop, wait, wake (`requeue`), comment | stop only (a marker the hook enforces) |
| README | `hermes/README.md` | `claude_code/README.md` |

Read `claude_code/` first: it is the smaller example and almost read-only (`stop` only). `hermes/` adds the write
side (parking tasks through the platform's CLI).

### 1. Before you write code

- Decide what the platform can tell you. A task board with statuses and owners gives
  tasks; a hook or log stream gives sessions, heartbeats and tool calls. Either alone is
  useful. Claude Code has no blocked or review status, so some rules simply have nothing to
  do on it.
- Decide which actions the platform allows. Most platforms allow few or none; that is fine
  (step 5).
- Open an issue with the "New platform adapter" template first, so the scope is agreed
  before the code. One adapter per PR, and the read side first: actions come in later PRs.

### 2. Create the package

Create `src/kelpie/adapters/<platform>/` with `__init__.py` exporting the adapter class
(as `claude_code/__init__.py` does), `adapter.py`, `config.py` for `[adapters.<platform>]`,
and a `README.md` with the config, the privacy notes and the status mapping ("What Kelpie
sees" in both existing READMEs).

The adapter is a dataclass whose fields are its settings (`ClaudeCodeAdapter`,
`HermesAdapter`) and whose `name` is a stable identifier (`"claude-code"`, `"hermes"`). It
implements `kelpie.core.ports.PlatformAdapter`: `list_tasks`, `get_task`, `requeue`,
`unblock`, `comment`, `wip_commit`, `events`. The read methods can be thin: both adapters
build `list_tasks` from `snapshot(now)` and return nothing from `events` (see
`claude_code/adapter.py`).

### 3. `snapshot(now)`: the part that matters

Add `snapshot(self, now: datetime) -> Snapshot` (the optional `SnapshotSource` protocol in
`src/kelpie/core/ports.py`; only adapters with it contribute to the board). The caller owns
the clock: use `now`, never read the time, and reject a naive datetime as both adapters do.
The engine merges the snapshots of all adapters (`merged_snapshot` in `src/kelpie/app.py`).
An adapter that raises is logged, counted in `kelpie_adapter_errors_total` and left out of
that tick, so raise a clear error when the platform cannot be read (`HermesError`,
`ClaudeCodeError`) rather than returning a half-empty board, which rules would read as
"nothing is running".

What to fill in (the types are in `src/kelpie/core/model.py`):

- **Tasks.** `Task(id, title, status, status_since, role, owner_id, source=self.name,
  platform_status=...)`. Set `source` to the adapter's `name` on every task: the engine
  routes a task action to the adapter named there. Snapshots of all adapters are
  concatenated, so make task ids unique across platforms (Claude Code uses `<list>/<id>`,
  see `_tasks` in `claude_code/adapter.py`). `status_since` is when the task entered its
  current status; when the platform has no timestamp, approximate it and say how (Claude
  Code uses the latest `TaskUpdate` seen in the spool, else the file's modification time).
  Fill the optional fields (`block`, `returns`, `files`, `branch`, `subtasks`, `body`, ...)
  only when the platform really knows them: Hermes does for most (`HermesAdapter._tasks`),
  Claude Code for none.
- **Status mapping.** Map platform statuses onto `kelpie.core.model.Status` (`backlog`,
  `ready`, `in_progress`, `review`, `blocked`, `waiting`, `accepted`, `done`, `failed`,
  `cancelled`) in one table and document it in the adapter README. Hermes: `STATUS_MAP` and
  `OUTCOME_STATUS` in `hermes/adapter.py`. Claude Code: `STATUS_MAP` plus the `pending`
  rule in `_tasks` (ready, or backlog while a `blockedBy` task is open). Keep the raw status
  in `platform_status`. An unknown status is skipped and warned about once, not guessed
  (`_warn` in `claude_code/adapter.py`). `waiting` carries a contract: a task Kelpie
  reports as waiting registered a condition and exited on purpose, and the platform's
  dispatcher must not treat it as dead (see `PlatformAdapter` in `src/kelpie/core/ports.py`).
  Map to `waiting` only if you implement parking (step 5).
- **Owners.** `Owner(id, role, kind)`. `kind` is `OwnerKind.AGENT` by default;
  `OwnerKind.HUMAN` marks people, whom Kelpie escalates to rather than acts on. An adapter
  cannot guess who is a person, so make it config: Hermes has `human_owners` and
  `HermesAdapter.snapshot` turns those assignees into `OwnerKind.HUMAN`; Claude Code has
  only agents. Derive `role` from a naming convention or a config map (`HermesAdapter.role_of`),
  never from hard-coded names. Every `owner_id` used by a task or session should appear in
  `Snapshot.owners`.
- **Dependencies.** `Dependency(task_id, depends_on)` for each blocker still on the board
  (`_tasks` in `claude_code/adapter.py` drops edges to tasks it left out).
- **Sessions and heartbeats.** `Session(id, task_id, owner_id, started_at, ended_at,
  final_status, exit_reason, last_heartbeat, source=self.name, ...)` is one agent run. A
  session without `ended_at` is live, and `last_heartbeat` must mean "the agent was seen
  doing something at this time": rules compare it with `session_stale_after`. Hermes
  combines the run's heartbeat columns, spool tool calls and a valid claim
  (`HermesAdapter._sessions`). Claude Code uses the newest spool line and, because an idle
  interactive session writes nothing, asks the operating system whether the recorded process
  still runs (`claude_code/process.py`; `_sessions` and `_process_alive` in
  `claude_code/adapter.py`). `final_status` is what the agent declared at its end, not what
  you infer: leave it `None` when the session ended without saying so, which is exactly what
  the `exit_contract` rule looks for. A session may exist without a board task (Claude
  Code uses `session:<id>` as its `task_id`). Always set `Session.source`: a `stop` is
  routed by it.
- **Conditions.** Only if the platform can park a task on an external condition. The adapter
  then returns the conditions it registered in `Snapshot.conditions` with `task_id` set
  (Hermes keeps them in a signed marker on the platform's own record, so they survive
  restarts: `_park_conditions` in `hermes/adapter.py` and `hermes/park.py`). Otherwise leave
  the field empty, as Claude Code does. Evaluating a condition is a connector's job, not the
  adapter's ([Adding a condition connector](#adding-a-condition-connector)).
- **Spans.** Tool calls become `Span`s, which the loop and budget rules read. If the platform
  exports OpenTelemetry GenAI spans, OTLP ingest (`src/kelpie/traces/`) takes them, and the
  adapter only has to report sessions under the same ids (Claude Code's own spans join its
  sessions by `session.id`). If it does not, write a hook (step 4) and convert its records
  with `kelpie.traces.hooks.spans_from_hook_events`: one `HookEvent` per record, as
  `_event_from_line` in `hermes/adapter.py` and `_spans` in `claude_code/adapter.py` do. The
  spans then carry the same `args_hash` as OTLP ingest, and a call reported both ways is
  kept once.

### 4. Code that runs on the agent host

Hooks and plugins that run inside the platform stay in the adapter package, use only the
standard library, import nothing from Kelpie (they run in the platform's own interpreter),
never raise, never wait on the network and write one JSON line per event to a spool file
that the adapter reads. See `hermes/plugin.py` and `claude_code/hook.py`, and the "Spool
format" and "Privacy" sections of their READMEs.

- Record no prompts, messages or raw tool arguments and results. A tool call is its name,
  an `args_hash`, the size of its result and, at most, a redacted command prefix. The hash
  and the redaction are shared code: copy the shared blocks, never write your own. The
  redaction block (`src/kelpie/traces/command.py`, `hermes/plugin.py`,
  `claude_code/hook.py`) is kept byte-identical by `tests/traces/test_redaction.py`; the
  `args_hash` copies of `src/kelpie/traces/args.py` are checked by
  `tests/traces/test_semconv.py` (Hermes by source, Claude Code by results). Add your hook
  to both tests. Describe what is recorded in [docs/privacy.md](docs/privacy.md).
- Create the spool with mode `0600`, never follow a symlink or block on a FIFO there, and
  read it defensively (size limits, malformed lines skipped): agents can write next to it.
  `src/kelpie/adapters/_spool.py` does this for you. Task files are read the same way
  (`open_regular` there, `_read_task` in `claude_code/adapter.py`).
- Make `kelpie spool scrub` find the new spool (`_scrub` in `src/kelpie/cli.py` and
  `default_spools` in `src/kelpie/traces/scrub.py`).

### 5. Actions

An adapter carries out what the platform allows and refuses the rest.

- An action the platform does not allow raises `kelpie.core.ports.UnsupportedAction` before
  any side effect, and the engine alerts a human ("not executed, needs a human").
  `claude_code/adapter.py` does this for `requeue`, `unblock`, `comment` and `wip_commit`.
  A plain `NotImplementedError` means "not implemented yet" and is recorded as a failure.
- Optional capabilities are separate protocols in `src/kelpie/core/ports.py`, checked with
  `isinstance`: `SupportsStop`, `SupportsWait`, `SupportsReassign`, `SupportsClose`.
  Declare what the adapter really does in `actions: ClassVar[frozenset[ActionKind]]`, as
  both adapters do; `kelpie agent` offers only those.
- `stop(session_id, reason)` may return a few words on what the stop means on the platform,
  returns `ActionResult.NOTHING_TO_DO` when the session had already ended, and may raise an
  error with a `public_reason` (the only part of it a chat message shows). Hermes goes
  through the platform's CLI after re-checking the run (`hermes/stop.py`); Claude Code
  writes a marker that its hook enforces before the next tool call (`write_marker` and
  `stop` in `claude_code/adapter.py`; "Stopping a session" in its README).
- Read the platform's data read-only and change it only through the platform's own
  interface. Re-check the target right before a write; keep every action idempotent.
- Act-mode guard: Kelpie refuses to start with act-mode rules whose actions a configured
  adapter cannot carry out. If your adapter carries out actions, check `HERMES_ACT_RULES`
  and `_refuse_unimplemented_actions` in `src/kelpie/app.py`. An adapter that refuses
  everything else with `UnsupportedAction` (Claude Code) needs no guard entry.

### 6. Status, doctor and config

- `check() -> str | None` returns why the platform cannot be read, or `None`. Optional
  `status_detail(now) -> list[str]` and a `db_path` attribute feed `kelpie status`
  (`render_adapters` in `src/kelpie/app.py`). Both adapters report the age of the newest
  spool line.
- `kelpie doctor` has a module per adapter: `claude_code/doctor.py` (`run`) is called from
  `src/kelpie/doctor.py`. Add one if setup can go wrong in ways a human should hear about
  (hook not registered, spool empty, task list not found).
- Config is data: paths, list names, roles and human owners come from
  `[adapters.<platform>]`, never from code. Follow `claude_code/config.py`: a frozen
  `...Settings` dataclass with `build()` and a `load_...` function that rejects unknown
  keys, validates types and raises `ValueError` naming the key; if the platform needs
  secrets, read them from environment variables, never from the config file. Wire it in `src/kelpie/app.py`: a field on `Settings`, a loader
  called from `load_settings` (`_load_claude_code`), the new name in the allowed
  `[adapters]` entries (the check at the top of `_load_hermes`) and a branch in
  `build_adapters`. Document the section in [docs/configuration.md](docs/configuration.md)
  and `kelpie.example.toml`.

### 7. Tests and fixtures

- Adapter tests go to `tests/adapters/test_<platform>_*.py` and run against synthetic
  platform data built inside the test, never a live board: `tests/adapters/kanban.py`
  creates a throwaway Hermes `kanban.db`, and the `Farm` class in
  `tests/adapters/test_claude_code_adapter.py` writes a fake `~/.claude` (task lists and a
  spool). Inject the clock and whatever touches the host (`process_check` on the Claude
  Code adapter); `tests/adapters/conftest.py` keeps tests independent of the machine they
  run on. Cover each status mapping, a live, an ended and a crashed session, malformed
  input (bad lines, symlinks, oversized files), every refused action and the config loader
  (`tests/adapters/test_claude_code_config.py`).
- Wiring: extend `tests/test_app_adapters.py` for the config section, `build_adapters` and
  `kelpie status`.
- Rule fixtures stay platform-neutral. `tests/fixtures/incidents/` holds snapshots, not
  platform files, so an adapter does not add fixtures there. When the platform shows a
  failure that no existing fixture covers, add a fixture for the rule that should catch it
  ([format](tests/fixtures/incidents/README.md)).
- No test writes outside its temporary directories: `HOME` is a temporary directory for
  the whole run, every `KELPIE_*` variable of your shell is unset for it, and a test that
  writes `./kelpie.db` (the default storage path) in the directory pytest runs in fails.
  A test that loads settings and builds the app takes the `tmp_db` fixture
  (`tests/conftest.py`): it sets `KELPIE_DB`, which Kelpie uses instead of any
  `[storage] path`. Run the suite where no Kelpie instance uses `./kelpie.db`.
- All data is synthetic: no personal data, real hostnames, repo names, tokens or paths of a
  real farm. Use `T-101`, `agent-dev-1`, `host-a`, `example/repo`, `example.org`.

### 8. Invariants that apply

- `kelpie.core` and `kelpie.rules` never import `kelpie.adapters`. Adapters implement the
  protocols in `kelpie.core.ports`, and only `src/kelpie/app.py` and `src/kelpie/doctor.py`
  (and the `kelpie.sim` farm simulator) name concrete adapters. `uv run lint-imports` and `tests/test_architecture.py` enforce it.
- No platform specifics in core or in rules: no status name, tool name or path of one
  platform outside its adapter package. If a rule needs a new fact, add a neutral field to
  `kelpie.core.model` (as `Task.platform_status` and `Session.source` were) and fill it
  from every adapter that knows it.
- No LLM in an adapter or in the control loop, no I/O in a rule, no clock reads except the
  `now` you are given.

### Checklist

- [ ] `src/kelpie/adapters/<platform>/` with `PlatformAdapter` and `snapshot(now)`
- [ ] One status table onto `Status`, raw status in `platform_status`, documented in the README
- [ ] `source=self.name` on every task and session; task ids unique across adapters
- [ ] Owners with `role` from config or convention; people marked `OwnerKind.HUMAN` via config
- [ ] Sessions with a meaningful `last_heartbeat`; `final_status` only when declared
- [ ] Hook or plugin (if any): standard library only, never raises, `0600` spool, shared redaction, no raw arguments
- [ ] Unsupported actions raise `UnsupportedAction`; `actions` declares what is carried out
- [ ] `check()`, `status_detail()`, and a doctor module if setup can fail
- [ ] `[adapters.<platform>]` loader and `build_adapters` wiring; `docs/configuration.md` and `kelpie.example.toml` updated
- [ ] Tests on synthetic data (adapter, config, wiring); no network, no live platform
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run lint-imports && uv run pytest --cov=kelpie` pass

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
- Update docs (the rules tables in `README.md` and `docs/guide.md`, `docs/`) when behavior or interfaces change.

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
