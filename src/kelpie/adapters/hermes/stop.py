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

The block is signed. Its reason ends with a marker, ``[kelpie-stop:v1:<issued>:<nonce>.<mac>]``:
HMAC-SHA256 over a domain string, the task, the time of issue and a nonce, keyed by
the same secret that signs parks (``wait_key``, see park.py). The adapter reads a block as
Kelpie's own stop (``Block.kelpie``) only when the marker verifies for that task and Hermes
recorded the block within one CLI call of its issue, so a worker cannot pass its own block
off as Kelpie's by copying the text. Rule 4 then leaves it to rule 13: it waits for a human,
not for a condition. Without a usable key the stop still happens, unsigned (stopping a
runaway matters more), and the block is treated like any other.

The command is pinned to exactly the run Kelpie looked at. ``HERMES_KANBAN_TASK`` and
``HERMES_KANBAN_RUN_ID`` name that task and run, so the CLI passes the run as
``expected_run_id`` and ``block_task`` adds ``AND current_run_id = ?``: if the task was
re-claimed (a new run) between Kelpie's check and the command, the new run is not blocked and
the command fails. (The CLI records the reason as a comment before it tries the block, so a
lost race can leave one stray "BLOCKED: Stopped by Kelpie" comment on the task.)
``HERMES_KANBAN_DB`` is pinned to the adapter's ``db_path``, so it acts on the board Kelpie
watches. The environment is an allow-list (:data:`ENV_EXACT`, ``LC_*``, ``XDG_*``, ``HERMES_*``
except the board-selecting variables), so Kelpie's own credentials never reach the CLI.
On timeout the whole process group is killed.

On Hermes a stop is therefore "block the task now, reap the worker later": the worker lives
until the dispatcher's next reaping tick after the 120 s grace period, and never when no
dispatcher runs. Anything that must end spend at once (a hard budget) is delayed by that.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_CLI = "hermes"
CLI_TIMEOUT = 10.0  # seconds for one command; a stop runs inside the control loop tick
RUN_PREFIX = "hermes-run-"
BLOCK_KIND = "needs_input"
MAX_REASON = 500
ERROR_TAIL = 300
BOARD_ENV = "HERMES_KANBAN_DB"
TASK_ENV = "HERMES_KANBAN_TASK"
RUN_ENV = "HERMES_KANBAN_RUN_ID"
# The CLI gets only this much of Kelpie's environment, so Kelpie's own tokens never reach it.
ENV_EXACT = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "VIRTUAL_ENV"})
ENV_PREFIXES = ("LC_", "XDG_", "HERMES_")
# Hermes variables that would send the command to another board or scope it elsewhere; the
# board and the run are set explicitly instead.
ENV_DENIED = frozenset(
    {
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_HOME",
        "HERMES_DELEGATED_CHILD_CONTEXT",
        BOARD_ENV,
        TASK_ENV,
        RUN_ENV,
    }
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_STOP_DOMAIN = "kelpie-stop:v1"
# The marker is the reason's last word: "[kelpie-stop:v1:<issued>:<nonce>.<mac>]".
_STOP_MARKER = re.compile(
    r"\s*\[kelpie-stop:v1:([0-9]{1,12}):([0-9a-f]{16})\.([0-9a-f]{64})\]\s*\Z"
)
_ANY_STOP_MARKER = re.compile(r"\[?kelpie-stop", re.IGNORECASE)
STOP_SKEW = timedelta(seconds=5)  # between Kelpie's clock and the time Hermes records
# What a done stop means on Hermes, for the human reading "Stopped run <id>; ..." (the reap
# grace is TERMINAL_WORKER_REAP_GRACE_SECONDS, see above).
STOP_DONE = (
    "task {task} is blocked for a human (hermes kanban unblock {task}); Hermes' dispatcher "
    "ends the worker within about 2 minutes"
)


class HermesCliError(RuntimeError):
    """The Hermes CLI is missing, timed out or refused the command. ``public_reason`` says
    so without anything the CLI printed (which may hold secrets), for the human told."""

    def __init__(self, message: str, public_reason: str | None = None) -> None:
        super().__init__(message)
        self.public_reason = public_reason


def run_id(session_id: str) -> int | None:
    """``hermes-run-<id>`` -> ``<id>``; ``None`` for any other session id."""
    if not session_id.startswith(RUN_PREFIX):
        return None
    digits = session_id[len(RUN_PREFIX) :]
    return int(digits) if digits.isascii() and digits.isdigit() else None


def stop_comment(
    task_id: str,
    reason: str,
    *,
    key: bytes | None = None,
    now: datetime | None = None,
) -> str:
    """The text left on the task: why it was stopped and how to resume it, ending with the
    signed stop marker when there is a ``key`` (issued at ``now``)."""
    clean = _CONTROL.sub(" ", reason)
    while _ANY_STOP_MARKER.search(clean):  # rule text may quote agents: it carries no marker
        clean = _ANY_STOP_MARKER.sub("kelpie stop", clean)
    clean = clean.strip() or "no reason given"
    if len(clean) > MAX_REASON:
        clean = clean[: MAX_REASON - 3] + "..."
    end = "" if clean.endswith(".") else "."  # reasons are sentences: no ".."
    text = (
        f"Stopped by Kelpie: {clean}{end} The task stays blocked until a human resumes it: "
        f"hermes kanban unblock {task_id}"
    )
    if key is None or now is None:
        return text
    issued, nonce = str(int(now.timestamp())), secrets.token_hex(8)
    return f"{text} [kelpie-stop:v1:{issued}:{nonce}.{_stop_mac(key, task_id, issued, nonce)}]"


def verify_stop(
    reason: str,
    task_id: str,
    key: bytes | None,
    recorded_at: datetime | None,
    window: timedelta,
) -> str | None:
    """The reason without its marker when it ends with Kelpie's stop marker for ``task_id``,
    signed with ``key`` and recorded by Hermes (``recorded_at``) within ``window`` of its
    issue; else ``None``. A marker names the task it was made for, and a copy recorded later
    (the text stays on the task as a comment) is not Kelpie's stop."""
    if key is None or recorded_at is None or reason.lower().count("kelpie-stop") != 1:
        return None
    m = _STOP_MARKER.search(reason)
    if m is None:
        return None
    issued, nonce, mac = m.groups()
    if not hmac.compare_digest(mac, _stop_mac(key, task_id, issued, nonce)):
        return None
    at = datetime.fromtimestamp(int(issued), recorded_at.tzinfo)
    if not at - STOP_SKEW <= recorded_at <= at + window + STOP_SKEW:
        return None
    return reason[: m.start()]


def has_stop_marker(reason: object) -> bool:
    """Whether a reason looks like it carries a Kelpie stop marker (valid or not)."""
    return isinstance(reason, str) and "[kelpie-stop:" in reason


def _stop_mac(key: bytes, task_id: str, issued: str, nonce: str) -> str:
    msg = f"{_STOP_DOMAIN}\n{task_id}\n{issued}\n{nonce}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def cli_env(base: Mapping[str, str], db_path: Path, task_id: str, rid: int) -> dict[str, str]:
    """The environment of the command: allow-listed variables, the board and the run."""
    env = {
        k: v
        for k, v in base.items()
        if k not in ENV_DENIED and (k in ENV_EXACT or k.startswith(ENV_PREFIXES))
    }
    env[BOARD_ENV] = str(db_path)
    env[TASK_ENV] = task_id
    env[RUN_ENV] = str(rid)
    return env


def block_command(exe: str, task_id: str, comment: str) -> Sequence[str]:
    return [exe, "kanban", "block", "--kind", BLOCK_KIND, "--", task_id, comment]


def block_task(
    cli: str,
    db_path: Path,
    task_id: str,
    rid: int,
    comment: str,
    *,
    timeout: float = CLI_TIMEOUT,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run ``hermes kanban block`` for run ``rid`` of ``task_id``.

    Raises :class:`HermesCliError` unless it succeeds (also when the run is no longer the
    task's current run).
    """
    exe = shutil.which(cli)
    if exe is None:
        raise HermesCliError(
            f"Hermes CLI not found: {cli!r} (set [adapters.hermes] cli)",
            "the Hermes CLI was not found (set [adapters.hermes] cli)",
        )
    env = cli_env(os.environ if environ is None else environ, db_path, task_id, rid)
    try:
        proc = subprocess.Popen(
            block_command(exe, task_id, comment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise HermesCliError(
            f"cannot run the Hermes CLI {cli!r}: {exc}", "the Hermes CLI could not be started"
        ) from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        proc.communicate()
        raise HermesCliError(
            f"hermes kanban block {task_id} timed out after {timeout:g}s",
            f"hermes kanban block timed out after {timeout:g}s",
        ) from exc
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-ERROR_TAIL:]
        raise HermesCliError(
            f"hermes kanban block {task_id} failed (exit {proc.returncode}): {tail}",
            f"hermes kanban block failed (exit {proc.returncode})",
        )


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill the command and anything it started; it is our own child in its own session."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass


kill_group = _kill_group  # shared with kelpie.adapters.hermes.park
