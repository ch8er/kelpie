"""The hooks spool, written by the real Hermes plugin (:mod:`kelpie.adapters.hermes.plugin`).

A Hermes worker runs the plugin with ``HERMES_KANBAN_TASK`` and ``HERMES_KANBAN_RUN_ID`` in
its environment and ``KELPIE_HERMES_SPOOL`` pointing at the spool. :class:`Spool` calls the
plugin's hooks with that environment, so the lines are exactly what a farm writes. Calls in
the past (a loop that ran all night) get their time by replacing the plugin's clock, nothing
else.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import types
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from kelpie.adapters.hermes import plugin
from kelpie.adapters.hermes.worker import run_worker

# The plugin reads os.environ, so a hook call changes the process environment while it runs:
# one writer at a time, and nothing else in the process may read HERMES_* meanwhile (the e2e
# tests run Kelpie in another process; the demo runs everything in one thread).
_LOCK = threading.Lock()


SIM_PID_BASE = 1 << 30  # above any pid a kernel hands out (Linux: at most 4194304)


def sim_writer(run_id: int) -> tuple[int, str]:
    """The writer a simulated run's worker puts on its spool lines, and the board records
    as the run's worker (:meth:`kelpie.sim.board.Board.claim`): a pid and a fingerprint no
    live process has, so stopping the run never signals a real process."""
    return SIM_PID_BASE + run_id, f"sim:0|{run_id}"


class Spool:
    """``board``: the simulated ``kanban.db``; a line's writer is then the run's recorded
    worker (its agent, else its submitter), as on a farm where that worker runs the plugin.
    Without it, or for a run without one, :func:`sim_writer`."""

    def __init__(self, path: Path, board: Path | None = None) -> None:
        self.path = path
        self.board = board

    def _writer(self, run_id: int) -> tuple[int, str | None]:
        if self.board is not None and self.board.exists():
            conn = sqlite3.connect(self.board)
            try:
                worker = run_worker(conn, run_id)
            finally:
                conn.close()
            if worker is not None:
                for pid, start in ((worker.agent, worker.agent_print),
                                   (worker.submitter, worker.submitter_print)):  # fmt: skip
                    if pid is not None:
                        return pid, start
        return sim_writer(run_id)

    @contextmanager
    def _worker(self, task_id: str, run_id: int, at: datetime | None) -> Iterator[None]:
        env: Mapping[str, str] = {
            plugin.SPOOL_ENV: str(self.path),
            "HERMES_KANBAN_TASK": task_id,
            "HERMES_KANBAN_RUN_ID": str(run_id),
        }
        clock = types.SimpleNamespace(time=lambda: at.timestamp()) if at is not None else None
        pid, start = self._writer(run_id)
        writer = lambda: {"pid": pid, "pid_start": start}  # noqa: E731
        with _LOCK, mock.patch.dict(os.environ, env), mock.patch.object(plugin, "_writer", writer):
            # Only this worker's variables: none of this machine's own Hermes ones.
            for key in [k for k in os.environ if k.startswith("HERMES_") and k not in env]:
                del os.environ[key]
            if clock is None:
                yield
            else:
                with mock.patch.object(plugin, "time", clock):
                    yield

    def tool_call(
        self,
        task_id: str,
        run_id: int,
        tool: str,
        args: Any,
        *,
        at: datetime | None = None,
        status: str = "ok",
        result: Any = "ok",
        duration_ms: int = 1000,
        call_id: str | None = None,
    ) -> None:
        """One ``post_tool_call`` hook, as Hermes fires it after a tool call."""
        with self._worker(task_id, run_id, at):
            plugin.post_tool_call(
                tool_name=tool,
                args=args,
                result=result,
                task_id="",  # Hermes' own internal id, not the kanban task
                session_id="",
                tool_call_id=call_id or "",
                duration_ms=duration_ms,
                status=status,
            )

    def session_end(
        self,
        task_id: str,
        run_id: int,
        *,
        at: datetime | None = None,
        completed: bool | None = True,
        interrupted: bool | None = False,
    ) -> None:
        with self._worker(task_id, run_id, at):
            plugin.on_session_end(completed=completed, interrupted=interrupted)

    def lines(self) -> list[str]:
        if not self.path.exists():
            return []
        return self.path.read_text(encoding="utf-8").splitlines()
