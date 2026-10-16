"""A simulated Hermes farm for end-to-end tests of ``kelpie serve``.

Everything Kelpie talks to on a real farm, faked locally (loopback only, no real network):

- :mod:`tests.sim.board`: a Hermes ``kanban.db`` built from scenario data, with times
  relative to the moment the scenario starts (Kelpie runs on the wall clock);
- :mod:`tests.sim.hermes_cli`: a fake ``hermes`` executable that records every call and
  applies ``kanban block`` / ``kanban unblock`` to the board as Hermes does;
- :mod:`tests.sim.spool`: tool calls and session ends written through the real Hermes plugin
  (:mod:`kelpie.adapters.hermes.plugin`);
- :mod:`tests.sim.github`: a stateful GitHub REST API (pull requests, commit statuses,
  Actions runs and their cancel endpoint);
- :mod:`tests.sim.channels`: Telegram and Slack endpoints that collect the messages;
- :mod:`tests.sim.kelpie`: runs the real ``kelpie`` CLI as a subprocess with a generated
  config, waits for control-loop ticks on ``/metrics`` and keeps its log.

:mod:`tests.sim.farm` ties them together for one scenario.
"""
