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

The `farm` fixture fails a scenario when `kelpie serve` logs anything at ERROR or above (or a
traceback) that the scenario did not list in `farm.allowed_errors`, or does not exit cleanly
on SIGTERM.

```bash
uv run pytest tests/e2e -v                      # all scenarios, about a minute
uv run pytest tests/e2e --basetemp=/tmp/e2e     # keep each farm and kelpie-serve.log
```

CI runs them in their own `e2e` job; the `test` job skips this directory.
