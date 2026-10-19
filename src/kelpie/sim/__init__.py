"""A simulated Hermes farm, for ``kelpie demo`` and the end-to-end tests.

Everything Kelpie talks to on a real farm, faked locally (loopback only, no real network, no
Hermes, no tokens):

- :mod:`kelpie.sim.board`: a Hermes ``kanban.db`` built from scenario data;
- :mod:`kelpie.sim.hermes_cli`: a fake ``hermes`` executable that records every call and
  applies ``kanban block`` / ``kanban unblock`` to the board as Hermes does;
- :mod:`kelpie.sim.spool`: tool calls and session ends written through the real Hermes
  plugin (:mod:`kelpie.adapters.hermes.plugin`);
- :mod:`kelpie.sim.github`: a stateful GitHub REST API (pull requests, commit statuses,
  Actions runs and their cancel endpoint; issues with their labels, assignees, comments and
  the issues adapter's writes);
- :mod:`kelpie.sim.toml`: writes the generated config.

Nothing in Kelpie's control loop imports this package (an import-linter contract).
"""
