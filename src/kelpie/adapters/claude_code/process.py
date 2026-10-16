"""Is a Claude Code session's process still running? (liveness for rule 1 and friends)

An interactive session that waits at its prompt fires no hooks, so its last spool line ages
like that of a crashed session. Its transcript is no help either: nothing is written while
it waits. Its process is the one reliable sign, so the hook records the pid of the Claude
Code process it runs under, that process's start time and where the pid is meaningful
(:func:`kelpie.adapters.claude_code.hook.claude_process`), and Kelpie checks it here.

The answer is True or False only when it is certain, else None (the session keeps the
spool-based liveness it had before):

- the pid must mean the same here as where the hook ran: same host name, same user (a
  ``/proc`` mounted with ``hidepid`` hides other users' processes), same PID namespace
  (containers, ``hostNetwork`` pods) and same boot (``boot_id``; start times count from
  boot). Anything missing or different gives None;
- the start time must be known (Linux ``/proc/<pid>/stat``): a pid alone can belong to a
  later process once the session's process is gone. Without ``/proc`` (macOS) the answer
  is None;
- a running process with that pid and start time: True. No such pid, one that started at
  another time, or a zombie (dead, not yet reaped by its parent): False.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from kelpie.adapters.claude_code.hook import PROC, proc_stat

IDENTITY = ("host", "uid", "pidns", "boot")
DEAD_STATES = frozenset({"Z", "X", "x"})


def process_state(
    pid: int,
    recorded: Mapping[str, Any],
    local: Mapping[str, Any],
    proc: Path | None = None,
) -> bool | None:
    """True if the recorded process runs, False if it is gone, None if it cannot be told.

    ``recorded`` holds what the hook saw (``pid_start`` and the :data:`IDENTITY` keys),
    ``local`` Kelpie's own identity (:func:`kelpie.adapters.claude_code.hook.identity`)."""
    start = recorded.get("pid_start")
    if start is None or any(
        recorded.get(k) is None or recorded.get(k) != local.get(k) for k in IDENTITY
    ):
        return None
    fields = proc_stat(pid, proc)
    if fields is None:
        return False if not ((proc or PROC) / str(pid)).exists() else None
    if fields[0] in DEAD_STATES:
        return False
    if len(fields) <= 19:
        return None
    return fields[19] == start
