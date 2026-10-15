"""Hermes adapter: stop a run (``SupportsStop``).

Mechanism. Kelpie asks Hermes to do it, through the supported operator command
``hermes kanban block <task> --kind needs_input "<reason>"``. Kelpie itself never writes
``kanban.db`` and never signals a process. In one transaction Hermes' ``block_task``:

- moves the task ``running`` -> ``blocked`` (kind ``needs_input``, which Hermes parks for a
  human and never re-queues by itself, so the task is not picked up again until a human
  runs ``hermes kanban unblock <task>``);
- releases the claim and ends the run (outcome ``blocked``);
- records the reason as a ``BLOCKED: <reason>`` comment on the task, which carries how to
  resume.

The worker process is not signalled by ``block`` itself. Hermes' dispatcher does that on a
later tick (``reap_terminal_workers`` in ``hermes_cli/kanban_db_dispatch.py``): it finds a
worker that outlived its closed run through the run's retained ``worker_pid`` and
``worker_started_at`` fingerprint, signals it only when the fingerprint still matches (a
recycled pid or a legacy row without fingerprint is never touched), and only after
``TERMINAL_WORKER_REAP_GRACE_SECONDS`` (120 s). Without a running Hermes dispatcher the
worker lives until it exits by itself; the task is still blocked and not re-queued.

Rejected alternatives: ``hermes kanban reclaim`` terminates the worker but returns the task to
the queue, where the dispatcher can respawn it before a second command blocks it;
``hermes kanban archive`` terminates the worker but is terminal; signalling the recorded pid
from Kelpie would duplicate Hermes' fingerprint checks and could hit a stranger.

The command runs with ``HERMES_KANBAN_DB`` pinned to the adapter's ``db_path``, so it acts on
the board Kelpie watches, and without the worker-scoping variables of a Hermes worker.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_CLI = "hermes"
CLI_TIMEOUT = 30.0  # seconds for one command
RUN_PREFIX = "hermes-run-"
BLOCK_KIND = "needs_input"
MAX_REASON = 500
ERROR_TAIL = 300
# Set inside a Hermes worker; they scope the CLI to that worker's own task and run.
WORKER_ENV = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT")
BOARD_ENV = "HERMES_KANBAN_DB"

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class HermesCliError(RuntimeError):
    """The Hermes CLI is missing, timed out or refused the command."""


def run_id(session_id: str) -> int | None:
    """``hermes-run-<id>`` -> ``<id>``; ``None`` for any other session id."""
    if not session_id.startswith(RUN_PREFIX):
        return None
    digits = session_id[len(RUN_PREFIX) :]
    return int(digits) if digits.isascii() and digits.isdigit() else None


def stop_comment(task_id: str, reason: str) -> str:
    """The text left on the task: why it was stopped and how to resume it."""
    clean = _CONTROL.sub(" ", reason).strip() or "no reason given"
    if len(clean) > MAX_REASON:
        clean = clean[: MAX_REASON - 3] + "..."
    return (
        f"Stopped by Kelpie: {clean}. The task stays blocked until a human resumes it: "
        f"hermes kanban unblock {task_id}"
    )


def cli_env(base: Mapping[str, str], db_path: Path) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in WORKER_ENV}
    env[BOARD_ENV] = str(db_path)
    return env


def block_task(
    cli: str,
    db_path: Path,
    task_id: str,
    comment: str,
    *,
    timeout: float = CLI_TIMEOUT,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run ``hermes kanban block``. Raises :class:`HermesCliError` unless it succeeds."""
    exe = shutil.which(cli)
    if exe is None:
        raise HermesCliError(f"Hermes CLI not found: {cli!r} (set [adapters.hermes] cli)")
    argv: Sequence[str] = [exe, "kanban", "block", task_id, "--kind", BLOCK_KIND, comment]
    env = cli_env(os.environ if environ is None else environ, db_path)
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HermesCliError(f"hermes kanban block {task_id} timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise HermesCliError(f"cannot run the Hermes CLI {cli!r}: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip()[-ERROR_TAIL:]
        raise HermesCliError(
            f"hermes kanban block {task_id} failed (exit {done.returncode}): {tail}"
        )
