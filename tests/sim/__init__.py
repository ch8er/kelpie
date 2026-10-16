"""The end-to-end harness around the packaged farm simulator (:mod:`kelpie.sim`).

- :mod:`tests.sim.channels`: Telegram and Slack endpoints that collect the messages;
- :mod:`tests.sim.kelpie`: runs the real ``kelpie`` CLI as a subprocess with a generated
  config, waits for control-loop ticks on ``/metrics`` and keeps its log.

:mod:`tests.sim.farm` ties them and the simulator together for one scenario; times on its
board are relative to the moment the scenario starts (``kelpie serve`` runs on the wall
clock).
"""
