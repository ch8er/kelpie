"""Hermes adapter: park a task on conditions (``SupportsWait``), wake it, comment on it.

Mechanism. Hermes has its own state for "waiting on time or an external event":
``scheduled`` (``hermes kanban schedule <task> <reason>``). A scheduled task is not
dispatchable, sends no notification, is never re-queued by Hermes itself, and leaves only
through ``hermes kanban unblock`` (Hermes ``hermes_cli/kanban_db.py`` ``schedule_task`` and
``unblock_task``). Kelpie parks there rather than in ``blocked``: Hermes keeps a block's kind
across an unblock, so a second block of the same kind sends the task to ``triage`` (its
unblock-loop breaker), and blocked tasks page subscribed chats.

The conditions travel in the reason. Kelpie writes the text of the reason followed by a
marker, ``[kelpie-wait:v2:<issued>:<nonce>:<base64url JSON>.<mac>]`` with
``[{"id", "kind", "params"}, ...]``.
Hermes stores the reason in the ``scheduled`` event's payload (and as a ``SCHEDULED:``
comment and the run summary), and the adapter reads it back: a ``scheduled`` task whose
latest ``scheduled`` event (the one that parked it: a scheduled task cannot be scheduled
again) carries a valid marker is ``waiting`` on those conditions, registered at that event's
time, whatever Hermes writes afterwards (the worker reap, operator edits). Kelpie never
writes ``kanban.db``.

Forgery and replay. A worker may schedule its own task, so a marker is only trusted with a
valid ``mac``: HMAC-SHA256 over a domain string, the task id, the time of issue, a nonce and
the encoded conditions, keyed by a secret Kelpie creates on first use (``wait_key``, default
next to Kelpie's ledger, mode 0600, refused if others can read it, if it is a symlink or
belongs to another user). Run Kelpie as a user the agents cannot read that file as. Hermes
must have recorded the ``scheduled`` event within one CLI call (``cli_timeout``, plus a few
seconds of clock skew) of the time of issue: the reason is visible to later runs (the
``SCHEDULED:`` comment Hermes writes before it schedules, even when it then refuses), and
the same reason scheduled again later is not Kelpie's park. Within that window a copy still
verifies (the nonce is not checked against a list of issued parks); it can only park the
task on Kelpie's own conditions.
Condition ids must also name the task (``...:<task>:...``), so a park cannot claim another
task's conditions. A scheduled task with a marker that does not verify (forged, replayed,
the key lost or changed) is logged once and left alone as backlog; losing the key therefore
strands Kelpie's parks: keep it with the ledger (a container's data volume).

A Kelpie park counts as woken once the task left ``scheduled``, however (``unblock``, or a
human dragging it in the dashboard, which records no ``unblocked`` event). After a wake the
last park's conditions stay in the snapshot with their original time:
rules ignore them for the task's new status, and the GitHub step does not attach the same
PR reference again, so a human who moves a gated task back to ready is not overruled.

Safety, as for ``stop``:

- A task is parked only if Hermes still shows the status and run Kelpie's snapshot showed
  when the rule decided (ready for the dispatch gate, running for rule 12); otherwise the
  park fails (``HermesError``) and the rule decides again on the next tick.
- A running task is parked pinned to that run (``HERMES_KANBAN_TASK`` and
  ``HERMES_KANBAN_RUN_ID``): if it was re-claimed in between, the command fails. Its worker
  is reaped by Hermes' dispatcher about 120 s after the run ends. A ready task has no run
  to pin and Hermes offers no "still ready" guard: a claim in the moment between Kelpie's
  re-read and the command would end that fresh run (the task is parked and woken later;
  the cost is one restart), and a human's block in that moment would be replaced by the
  park (Hermes schedules todo, ready, running and blocked tasks alike), to be woken to
  ready later.
- Anything else (backlog, review, blocked, done) is not parked: ``UnsupportedAction``.
- A park Kelpie cannot read back as its own (Hermes stored the reason differently, the key
  changed meanwhile) is undone at once with ``unblock``, so it is never left scheduled with
  nothing to wake it. For a running task the run has already ended, so the undo costs a
  restart, and a new worker may start before the old one is reaped.
- Wake (``requeue``) unblocks only a task Kelpie parked; a Kelpie park already woken is left
  alone; any other requeue is ``UnsupportedAction``.
  Hermes lands it in ``ready``, or ``todo`` while a parent is open. The ``unblocked`` event
  does page chats subscribed to the task, and Hermes still holds a woken task back for 24 h
  if a GitHub PR URL appeared in one of its comments (written by its worker, for instance).

Text. Hermes holds back a ready task for 24 h after a GitHub PR URL appears in a comment on
it (its ``active_pr`` guard), so every text Kelpie writes has PR URLs rewritten to
``owner/repo#N``. Control characters are removed, ``kelpie-wait`` in the text (file names
come from workers) becomes ``kelpie wait`` so the text cannot carry a marker, each step
repeats until nothing changes, and the text is capped.

CLI (Hermes ``hermes_cli/kanban_parser.py``): ``schedule <task> [reason...]``,
``unblock [--reason TEXT] <task>...`` (the reason is also stored as a comment) and
``comment <task> <text...> [--author NAME]``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.hermes.stop import (
    BOARD_ENV,
    ENV_DENIED,
    ENV_EXACT,
    ENV_PREFIXES,
    ERROR_TAIL,
    RUN_ENV,
    TASK_ENV,
    HermesCliError,
    kill_group,
)
from kelpie.core.model import Condition, ConditionKind

log = logging.getLogger(__name__)

AUTHOR = "kelpie"
MAX_TEXT = 1000  # characters of the human-readable part of a reason or comment
MAX_MARKER = 4000  # characters of the encoded conditions
MIN_KEY = 16  # bytes
WAIT_KEY_NAME = "hermes-wait.key"
DEFAULT_WAIT_KEY = Path("~/.kelpie") / WAIT_KEY_NAME
CLOCK_SKEW = timedelta(seconds=5)  # between Kelpie's clock and the time Hermes records
_DOMAIN = "kelpie-wait:v2"
# The marker is the reason's last word: "[kelpie-wait:v2:<issued>:<nonce>:<blob>.<mac>]".
_MARKER = re.compile(
    r"\[kelpie-wait:v2:([0-9]{1,12}):([0-9a-f]{16}):([A-Za-z0-9_-]+=*)\.([0-9a-f]{64})\]\s*\Z"
)
_ANY_MARKER = re.compile(r"\[?kelpie-wait", re.IGNORECASE)
# As Hermes' active_pr guard (case-insensitive, any owner/repo segment), with its tail.
_PR_URL = re.compile(
    r"https?://[^\s/]+/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/#?][^\s)\]>\"']*)?", re.IGNORECASE
)
# C0 (but tab and newline), DEL, C1, and bidirectional overrides.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]+")


def clean_text(text: str, limit: int = MAX_TEXT) -> str:
    """Text safe to write to Hermes: no control characters, nothing that reads as a marker
    (``kelpie-wait`` becomes ``kelpie wait``), PR URLs as ``owner/repo#N``, at most
    ``limit`` characters. Each step repeats until nothing changes, so removing one piece
    cannot assemble another."""
    while True:
        before = text
        text = _CONTROL.sub(" ", text)
        text = _ANY_MARKER.sub("kelpie wait", text)
        text = _PR_URL.sub(lambda m: f"{m[1]}/{m[2]}#{m[3]}", text)
        if text == before:
            break
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def load_key(path: Path, *, create: bool) -> bytes | None:
    """The secret that signs park markers. ``None`` when it does not exist and ``create`` is
    false; created (32 random bytes, mode 0600, written to a temporary file and linked into
    place, so it is never seen half-written) when ``create``. Raises ``ValueError`` for a
    key that is too short, a symlink, or readable or writable by anyone but its owner, which
    must be this user (a key others can read lets them forge parks)."""
    path = path.expanduser()
    try:
        return _read_key(path)
    except FileNotFoundError:
        if not create:
            return None
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
            f.flush()
            os.fsync(f.fileno())
        try:
            # Atomic; fails if another process created the key meanwhile (that one is used).
            os.link(tmp, path)
            created = True
        except FileExistsError:
            created = False
        except OSError:  # no hard links on this filesystem: exclusive create instead
            created = _write_new(path, tmp.read_bytes())
    finally:
        tmp.unlink(missing_ok=True)
    key = _read_key(path)
    if not created:
        return key
    log.warning(
        "hermes: created the key that signs Kelpie's parks at %s; keep it with the ledger and "
        "out of the agents' reach (parks made with an earlier key are no longer recognised)",
        path.resolve(),
    )
    return key


def _write_new(path: Path, data: bytes) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    return True


def _read_key(path: Path) -> bytes:
    # Non-blocking, so a FIFO planted at the path cannot hang the control loop.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"{path}: the wait key must be a regular file")
        if st.st_mode & 0o077:
            raise ValueError(f"{path}: the wait key must be private (chmod 600)")
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise ValueError(f"{path}: the wait key must belong to the user Kelpie runs as")
        data = f.read(4096)
    if len(data) < MIN_KEY:
        raise ValueError(f"{path}: the wait key is too short")
    return data


def _mac(key: bytes, task_id: str, issued: str, nonce: str, blob: str) -> str:
    msg = f"{_DOMAIN}\n{task_id}\n{issued}\n{nonce}\n{blob}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def encode_wait(
    text: str,
    conditions: Sequence[Condition],
    task_id: str,
    key: bytes,
    now: datetime,
) -> str:
    """The reason of a park: readable text, then the signed marker with the conditions, the
    time of issue and a nonce."""
    data = [{"id": c.id, "kind": c.kind.value, "params": c.params} for c in conditions]
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True, default=str)
    blob = base64.urlsafe_b64encode(raw.encode()).decode()
    if len(blob) > MAX_MARKER:
        raise HermesCliError(f"too many conditions to park on ({len(conditions)})")
    issued, nonce = str(int(now.timestamp())), secrets.token_hex(8)
    mac = _mac(key, task_id, issued, nonce, blob)
    return f"{clean_text(text)} [kelpie-wait:v2:{issued}:{nonce}:{blob}.{mac}]"


def has_marker(reason: object) -> bool:
    """Whether a reason looks like it carries a Kelpie park marker (valid or not)."""
    return isinstance(reason, str) and "[kelpie-wait:" in reason


def decode_wait(
    reason: str,
    task_id: str,
    key: bytes | None,
    recorded_at: datetime | None,
    window: timedelta,
) -> list[tuple[str, ConditionKind, dict[str, Any]]] | None:
    """The conditions of Kelpie's park of ``task_id``, or ``None``.

    The reason must end with the only marker in it, signed with ``key`` for this task, and
    Hermes must have recorded it (``recorded_at``, the ``scheduled`` event's time) within
    ``window`` of its issue: the same reason written again later (copied from the comment
    the park leaves) is not Kelpie's park. Entries whose id does not name the task, or that
    are malformed, are left out; ``None`` if none is left."""
    if key is None or recorded_at is None or not reason or reason.count("[kelpie-wait:") != 1:
        return None
    m = _MARKER.search(reason)
    if m is None:
        return None
    issued, nonce, blob, mac = m.groups()
    if not hmac.compare_digest(mac, _mac(key, task_id, issued, nonce, blob)):
        return None
    at = datetime.fromtimestamp(int(issued), UTC)
    if not at - CLOCK_SKEW <= recorded_at <= at + window + CLOCK_SKEW:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(blob.encode()))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(data, list):
        return None
    result: list[tuple[str, ConditionKind, dict[str, Any]]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        cid, kind, params = item.get("id"), item.get("kind"), item.get("params", {})
        if not isinstance(cid, str) or f":{task_id}:" not in f":{cid}:":
            continue
        if not isinstance(params, dict):
            continue
        try:
            result.append((cid, ConditionKind(kind), params))
        except ValueError:
            continue
    return result or None


def kanban_env(
    base: Mapping[str, str], db_path: Path, task_id: str | None = None, rid: int | None = None
) -> dict[str, str]:
    """The CLI's environment: allow-listed variables and the board; the task and run only
    when the command is pinned to a run (``unblock`` refuses to run with a task set)."""
    env = {
        k: v
        for k, v in base.items()
        if k not in ENV_DENIED and (k in ENV_EXACT or k.startswith(ENV_PREFIXES))
    }
    env[BOARD_ENV] = str(db_path)
    if task_id is not None and rid is not None:
        env[TASK_ENV] = task_id
        env[RUN_ENV] = str(rid)
    return env


def run_kanban(
    cli: str,
    db_path: Path,
    args: Sequence[str],
    *,
    timeout: float,
    task_id: str | None = None,
    rid: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Run ``hermes kanban <args>``; raise :class:`HermesCliError` unless it exits 0."""
    exe = shutil.which(cli)
    if exe is None:
        raise HermesCliError(f"Hermes CLI not found: {cli!r} (set [adapters.hermes] cli)")
    env = kanban_env(os.environ if environ is None else environ, db_path, task_id, rid)
    what = " ".join(args[:1])
    try:
        proc = subprocess.Popen(
            [exe, "kanban", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise HermesCliError(f"cannot run the Hermes CLI {cli!r}: {exc}") from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        kill_group(proc)
        proc.communicate()
        raise HermesCliError(f"hermes kanban {what} timed out after {timeout:g}s") from exc
    if proc.returncode != 0:
        tail = (err or out or "").strip()[-ERROR_TAIL:]
        raise HermesCliError(f"hermes kanban {what} failed (exit {proc.returncode}): {tail}")


def schedule_args(task_id: str, reason: str) -> list[str]:
    return ["schedule", "--", task_id, reason]


def unblock_args(task_id: str, reason: str) -> list[str]:
    text = clean_text(reason) or "woken by Kelpie"
    return ["unblock", f"--reason={text}", "--", task_id]


def comment_args(task_id: str, body: str) -> list[str]:
    text = clean_text(body, 4 * MAX_TEXT) or "(Kelpie: no text)"
    return ["comment", "--author", AUTHOR, "--", task_id, text]
