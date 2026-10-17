"""OpenClaw adapter: stop a session (``SupportsStop``).

OpenClaw has no operator command that stops a session for good: ``/stop`` and the Gateway's
``sessions.abort`` end the run in flight, and the next heartbeat, cron wake or queued message
starts another. A stop is therefore two steps.

1. **A stop marker** ``<stop_dir>/<sessionId>.json`` (``{"at": ..., "reason": ...}``, mode
   ``0600``, written atomically). The Kelpie OpenClaw plugin reads it in the Gateway process
   (``plugin/index.js``): while it holds, every tool call of the session is refused
   (``before_tool_call``) and, with conversation access, every new run that is not a message
   from the session's owner (``before_agent_run``: heartbeats, cron wakes, inter-session
   deliveries). A message from the operator lifts the stop and goes through; so does a
   reset, ``/new`` or deletion of the session, deleting the marker, or the plugin's
   ``stopTtl`` (24 hours; ``[adapters.openclaw] stop_ttl`` must match it). An idle, daily or
   compaction rollover carries the marker to the successor session id.
2. **An abort of the run in flight**, through the Gateway's own operator method:
   ``openclaw gateway call sessions.abort --params '{"key": <sessionKey>, "clearQueued":
   true}' --json``. It ends the current run (and its subagents) and drops queued follow-ups,
   so a model that keeps asking for refused tools stops spending at once. Background runs that
   OpenClaw starts outside the session's lane (seen live: a ``skill-workshop-review`` run under
   ``agent:main:internal-session-effects:...``) answer ``no-active-run`` to the key; for those
   Kelpie aborts the session's latest ``runId`` from the spool (``{"runId": ...}``), which
   stopped that run. The ``openclaw`` CLI authenticates with the local OpenClaw config
   (``gateway.auth``) or ``OPENCLAW_GATEWAY_TOKEN``, so Kelpie runs it as the Gateway's user on
   its host.

When the abort fails (no CLI, Gateway down, refused), the marker still holds: the run goes on
until it gives up on the refused tools, and the human told about the stop reads so.

Verified against OpenClaw 2026.10.4 on a local Gateway: ``sessions.abort`` answered
``{"ok": true, "abortedRunId": ..., "status": "aborted"}`` in about a second and the looping run
stopped calling the model; with nothing running it answers ``"status": "no-active-run"``.

The CLI gets only an allow-listed part of Kelpie's environment (:data:`ENV_EXACT`, ``LC_*``,
``XDG_*``, ``OPENCLAW_*``), so Kelpie's own tokens never reach it; on timeout its whole process
group is killed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_STOP_DIR = Path("~/.openclaw/kelpie/stop")
DEFAULT_STOP_TTL = 24 * 3600  # seconds; the plugin's default ``stopTtl``
DEFAULT_CLI = "openclaw"
CLI_TIMEOUT = 20.0  # seconds; the command runs inside the control loop tick
MAX_MARKER = 4096
MAX_REASON = 500
ERROR_TAIL = 300
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")  # used with fullmatch, as in the plugin
ENV_EXACT = frozenset({"PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR"})
ENV_PREFIXES = ("LC_", "XDG_", "OPENCLAW_")


class OpenClawCliError(RuntimeError):
    """The ``openclaw`` CLI is missing, timed out or refused the command. ``public_reason``
    says so without anything the CLI printed, for the human told."""

    def __init__(
        self, message: str, public_reason: str | None = None, kind: str = "failed"
    ) -> None:
        super().__init__(message)
        self.public_reason = public_reason
        self.kind = kind  # "refused": the Gateway answered and said no


def clean_reason(reason: str) -> str:
    """As the plugin shows it: control and format characters (bidi overrides, zero width)
    and line separators folded into spaces, at most ``MAX_REASON``."""
    text = "".join(
        " " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c for c in reason
    )
    text = re.sub(r" {2,}", " ", text).strip() or "no reason given"
    return text if len(text) <= MAX_REASON else text[: MAX_REASON - 3] + "..."


def write_marker(
    directory: Path, session_id: str, reason: str, at: float, key: str | None = None
) -> Path:
    """Write ``<directory>/<session_id>.json`` atomically (mode 0600), replacing an older one.
    ``key`` is the session key the stopped session ran under (as the spool has it): only an
    operator's message under that key lifts the stop."""
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError(f"not an OpenClaw session id: {session_id!r}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError(f"{directory} is not a plain directory")
    data: dict[str, object] = {"at": at, "reason": clean_reason(reason)}
    if key:
        data["key"] = key[:256]
    body = json.dumps(data).encode()
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".stop-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        target = directory / f"{session_id}.json"
        os.replace(tmp, target)  # replaces a symlink itself, never its target
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def read_marker(directory: Path, session_id: str) -> datetime | None:
    """When the session's stop marker was written; None if there is no usable one."""
    if not SESSION_ID.fullmatch(session_id):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(directory / f"{session_id}.json", flags)
    except OSError:
        return None
    try:
        raw = os.read(fd, MAX_MARKER + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        data = json.loads(raw.decode("utf-8", "replace")) if len(raw) <= MAX_MARKER else None
    except ValueError:
        return None
    at = data.get("at") if isinstance(data, dict) else None
    if isinstance(at, bool) or not isinstance(at, int | float):
        return None
    try:
        return datetime.fromtimestamp(at, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def cli_env(base: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in base.items() if k in ENV_EXACT or k.startswith(ENV_PREFIXES)}


def abort_session(
    cli: str,
    session_key: str | None,
    run_id: str | None,
    *,
    timeout: float = CLI_TIMEOUT,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Abort the session's run in flight: by its key, then, when the Gateway knows no run
    under the key (or the spool holds only a reduced key), by the latest run id. Returns the
    Gateway's last ``status``. Raises :class:`OpenClawCliError` when a call fails."""
    status = "no-active-run"
    end = time.monotonic() + timeout  # one budget for both calls: they run inside the tick
    # A key the hook reduced (a chat peer replaced by "~<hash>") is not the Gateway's key.
    if session_key and "~" not in session_key:
        try:
            status = abort_run(cli, {"key": session_key}, timeout=timeout, environ=environ)
        except OpenClawCliError as exc:
            # Refused (e.g. "session target is unavailable"): the run id may still be known.
            # A timeout or a missing CLI is not worth a second try.
            if not run_id or exc.kind != "refused":
                raise
    left = end - time.monotonic()
    if status == "no-active-run" and run_id and left > 1:
        status = abort_run(cli, {"runId": run_id}, timeout=left, environ=environ)
    return status


def abort_run(
    cli: str,
    params: Mapping[str, object],
    *,
    timeout: float = CLI_TIMEOUT,
    environ: Mapping[str, str] | None = None,
) -> str:
    """One ``sessions.abort`` call; returns the Gateway's ``status`` (``aborted``,
    ``no-active-run``, ...). Raises :class:`OpenClawCliError` unless the Gateway says ok."""
    body = {**params, "clearQueued": True}
    answer = gateway_call(cli, "sessions.abort", body, timeout=timeout, environ=environ)
    status = answer.get("status")
    return status if isinstance(status, str) else "ok"


def gateway_call(
    cli: str,
    method: str,
    params: Mapping[str, object] | None = None,
    *,
    timeout: float = CLI_TIMEOUT,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """``openclaw gateway call <method> [--params <json>] --json``: the Gateway's answer.
    Raises :class:`OpenClawCliError` unless it answers ``"ok": true``."""
    exe = shutil.which(cli)
    if exe is None:
        raise OpenClawCliError(
            f"OpenClaw CLI not found: {cli!r} (set [adapters.openclaw] cli)",
            "the openclaw CLI was not found",
        )
    env = cli_env(os.environ if environ is None else environ)
    argv = [exe, "gateway", "call", method]
    if params is not None:
        argv += ["--params", json.dumps(params)]
    try:
        proc = subprocess.Popen(
            [*argv, "--json"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise OpenClawCliError(
            f"cannot run the OpenClaw CLI {cli!r}: {exc}", "the openclaw CLI could not be started"
        ) from exc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        try:  # a grandchild that left the group may hold the pipes: do not wait for it
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
        raise OpenClawCliError(
            f"openclaw gateway call {method} timed out after {timeout:g}s",
            f"the Gateway did not answer within {timeout:g}s",
        ) from exc
    answer = _json_tail(out)
    if proc.returncode != 0 or not isinstance(answer, dict) or answer.get("ok") is not True:
        tail = (err or out or "").strip()[-ERROR_TAIL:]
        raise OpenClawCliError(
            f"openclaw gateway call {method} failed (exit {proc.returncode}): {tail}",
            f"the Gateway refused or did not answer (exit {proc.returncode})",
            "refused" if isinstance(answer, dict) and answer.get("ok") is False else "failed",
        )
    return answer


def _json_tail(out: str | None) -> object:
    """The JSON object the CLI printed (it may log lines before it)."""
    text = (out or "").strip()
    start = text.find("{")
    while start >= 0:
        try:
            return json.loads(text[start:])
        except ValueError:
            start = text.find("{", start + 1)
    return None


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Kill the command and anything it started; it is our own child in its own session."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(proc.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows
            proc.kill()
    except (ProcessLookupError, PermissionError):
        pass
