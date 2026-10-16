# End-to-end scenarios

Each test here runs the real `kelpie serve` (a subprocess, a generated TOML config, the
installed `kelpie` entry point) against a simulated Hermes farm from [`tests/sim`](../sim):

- a `kanban.db` with the Hermes schema, times relative to the real now;
- a fake `hermes` executable that records its calls and applies `kanban block` /
  `kanban unblock` to the board;
- a hooks spool written through the real Hermes plugin;
- fake GitHub (pull requests, CI, Actions runs and cancel), Telegram and Slack servers on
  loopback.

A scenario builds the board, starts Kelpie with the rules it needs in `suggest` or `act`
mode (act only where Kelpie can act on Hermes: `loop_breaker` stops a run, `ci_economy`
cancels a build), waits a few 1-second ticks on `/metrics`, and asserts on the board, the
Hermes calls and the messages in the channels.

The `farm` fixture fails a scenario when `kelpie serve` logs anything at ERROR or above, a
traceback, or a WARNING that reports a failure (a failed action or alert delivery, a failed
GitHub read: `FAILURE_WARNINGS` in `tests/sim/kelpie.py`) that the scenario did not list in
`farm.allowed_errors`, or does not exit cleanly on SIGTERM. Check that something was *not*
sent after `farm.stop()`: Kelpie delivers queued alerts before it exits.

```bash
uv run pytest tests/e2e -v                      # all scenarios (seconds each)
uv run pytest tests/e2e --basetemp=/tmp/e2e     # keep each farm and kelpie-serve.log
```

CI runs them in their own `e2e` job; the `test` job skips this directory.
