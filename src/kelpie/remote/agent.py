"""``kelpie agent``: runs on a machine with agents, next to their platforms.

It takes a snapshot of every local adapter each ``[agent] interval`` and reports it to the
hub of ``kelpie serve``, and long-polls the hub for actions, which it carries out through
the same :class:`PlatformExecutor` that runs them in-process in single-host mode. It has no
rules, no ledger, no alerts and no connectors. It only connects out; it listens nowhere.

Only the action kinds the local adapters implement are accepted (see
:func:`kelpie.remote.protocol.adapter_actions`); the protocol carries no commands.
"""

from __future__ import annotations

import gzip
import json
import logging
import secrets
import ssl
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from kelpie import __version__, net
from kelpie.core.alerts import failure_reason
from kelpie.core.executors import PlatformExecutor
from kelpie.core.ledger import Outcome as LedgerOutcome
from kelpie.core.ports import PlatformAdapter, SnapshotSource
from kelpie.remote.config import AgentSettings
from kelpie.remote.protocol import (
    ACTIONS_PATH,
    MAX_ID,
    MAX_RESULTS,
    MAX_WAIT,
    REPORT_PATH,
    AdapterReport,
    Outcome,
    ProtocolError,
    Result,
    adapter_actions,
    decode_action,
    decode_actions,
    depth_ok,
    encode_poll,
    encode_report,
)

log = logging.getLogger(__name__)

MAX_REPLY_BYTES = 1024 * 1024
POLL_WAIT = 20.0  # seconds the hub may hold a poll open
# Results of recent action ids, so an action the hub sends again is not run twice: for as
# long as the hub keeps an unanswered action's id (an hour), up to this many.
REMEMBER_FOR = 3600.0
REMEMBERED = 10_000
_BACKOFF_MIN, _BACKOFF_MAX = 1.0, 60.0

Post = Callable[..., net.Response]


class AgentError(Exception):
    """A request to the hub that failed; the text is safe to log. ``status``: the hub's
    HTTP status, if it answered."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Agent:
    """Reports local adapters' snapshots and carries out the hub's actions."""

    def __init__(
        self,
        settings: AgentSettings,
        adapters: Sequence[PlatformAdapter],
        token: str,
        *,
        clock: Callable[[], datetime],
        post: Post = net.post,
        context: ssl.SSLContext | None = None,
    ) -> None:
        if not adapters:
            raise ValueError("kelpie agent needs at least one enabled [adapters.*] section")
        self.settings = settings
        self.adapters = list(adapters)
        self.offered = {a.name: adapter_actions(a, settings.actions) for a in self.adapters}
        self._executors = {a.name: PlatformExecutor(a) for a in self.adapters}
        self._token = token
        self._clock = clock
        self._post = post
        self._context = context
        self._done: OrderedDict[str, tuple[float, Result]] = OrderedDict()
        self._unsent: list[Result] = []
        self._stop = threading.Event()
        self._boot = secrets.token_hex(8)  # orders this process's reports at the hub
        self._seq = 0

    # --- reports --------------------------------------------------------------------------

    def report(self) -> None:
        """Snapshot every adapter and send one report. An adapter that fails is reported
        with its error, so the hub knows its tasks are not observed; AgentError when the
        hub cannot be reached or refuses the report."""
        now = self._clock()
        parts: list[AdapterReport] = []
        for adapter in self.adapters:
            kinds = self.offered[adapter.name]
            if not isinstance(adapter, SnapshotSource):
                parts.append(AdapterReport(adapter.name, kinds, error="cannot take snapshots"))
                continue
            try:
                parts.append(AdapterReport(adapter.name, kinds, adapter.snapshot(now)))
            except Exception as exc:
                log.error("adapter %s failed to snapshot: %s", adapter.name, exc)
                parts.append(AdapterReport(adapter.name, kinds, error=type(exc).__name__))
        self._seq += 1
        body = encode_report(__version__, now, self.settings.interval, parts, self._boot, self._seq)
        self._send(REPORT_PATH, body, self.settings.timeout.total_seconds())

    # --- actions --------------------------------------------------------------------------

    def poll(self, wait: float = POLL_WAIT) -> int:
        """Send the results not yet delivered, wait up to ``wait`` seconds for actions,
        carry them out one at a time. Returns how many actions came."""
        results, self._unsent = self._unsent[:MAX_RESULTS], self._unsent[MAX_RESULTS:]
        try:
            reply = self._send(
                ACTIONS_PATH,
                encode_poll(results, min(wait, MAX_WAIT)),
                self.settings.timeout.total_seconds() + wait,
            )
        except AgentError as exc:
            if exc.status in (400, 413) and results:
                # The hub refuses this batch as it is: sending it again would block every
                # later poll. Its actions are reported failed on the server side anyway.
                log.error("the hub refused %d results; dropped: %s", len(results), exc)
            else:
                self._unsent = results + self._unsent  # delivered with the next poll
            raise
        try:
            raw = decode_actions(reply, self.offered)
        except ProtocolError as exc:
            raise AgentError(f"the hub sent an invalid reply: {exc}") from None
        received = time.monotonic()
        for item in raw:
            if self._stop.is_set():
                break  # not acknowledged: the hub re-sends it to the next agent run
            result = self.execute(item, received)
            if result is not None:
                self._unsent.append(result)
        return len(raw)

    def execute(self, raw: object, received: float | None = None) -> Result | None:
        """Carry out one action the hub sent (at monotonic time ``received``), at most once
        per action id, and only if its ``ttl`` has not run out before it starts."""
        try:
            wire = decode_action(raw, self.offered)
        except ProtocolError as exc:
            action_id = raw.get("id") if isinstance(raw, dict) else None
            log.warning("refused an action from the hub: %s", exc)
            if isinstance(action_id, str) and 0 < len(action_id) <= MAX_ID:
                return Result(action_id, Outcome.NOT_EXECUTED, f"the agent refused it: {exc}")
            return None
        self._forget_old()
        if (known := self._done.get(wire.id)) is not None:
            return known[1]  # sent again after a slow confirmation: not run twice
        if received is not None and time.monotonic() - received > wire.ttl:
            # The hub gave up waiting: a late start would surprise whoever was told it failed.
            log.warning("action %s expired before it could start; not carried out", wire.id)
            return Result(wire.id, Outcome.EXPIRED)  # not remembered: a retry may run it
        log.info(
            "executing %s on %s %s (rule %s)",
            wire.kind,
            wire.adapter,
            wire.task_id or wire.params.get("session_id"),
            wire.rule,
        )
        try:
            out = self._executors[wire.adapter].execute(wire.action())
        except Exception as exc:
            log.warning("action %s failed: %r", wire.id, exc)
            result = Result(wire.id, Outcome.FAILED, failure_reason(exc))
        else:
            result = _result(wire.id, out)
        self._done[wire.id] = (time.monotonic(), result)
        while len(self._done) > REMEMBERED:
            self._done.popitem(last=False)
        return result

    def _forget_old(self) -> None:
        cutoff = time.monotonic() - REMEMBER_FOR
        while self._done and next(iter(self._done.values()))[0] < cutoff:
            self._done.popitem(last=False)

    # --- loops ----------------------------------------------------------------------------

    def run(self, grace: float = 5.0) -> None:
        """Report and poll until :meth:`stop`; then give an action being carried out
        ``grace`` seconds to finish (a held poll is simply abandoned)."""
        threads = [
            threading.Thread(target=self._report_loop, name="kelpie-agent-report", daemon=True),
            threading.Thread(target=self._poll_loop, name="kelpie-agent-poll", daemon=True),
        ]
        for thread in threads:
            thread.start()
        self._stop.wait()
        for thread in threads:
            thread.join(grace)

    def _poll_loop(self) -> None:
        backoff = _BACKOFF_MIN
        while not self._stop.is_set():
            try:
                self.poll()
                backoff = _BACKOFF_MIN
            except AgentError as exc:
                log.warning("polling the hub for actions failed: %s", exc)
                self._stop.wait(backoff)
                backoff = min(_BACKOFF_MAX, backoff * 2)
            except Exception:
                log.exception("poll failed")
                self._stop.wait(_BACKOFF_MAX)

    def _report_loop(self) -> None:
        interval = self.settings.interval.total_seconds()
        backoff = _BACKOFF_MIN
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.report()
                backoff = _BACKOFF_MIN
                delay = max(0.0, interval - (time.monotonic() - started))
            except AgentError as exc:
                log.warning("reporting to the hub failed: %s", exc)
                delay = min(interval, backoff)
                backoff = min(_BACKOFF_MAX, backoff * 2)
            except Exception:
                log.exception("report failed")
                delay = interval
            self._stop.wait(delay)

    def stop(self) -> None:
        self._stop.set()

    # --- transport ------------------------------------------------------------------------

    def _send(self, path: str, body: Mapping[str, Any], timeout: float) -> Any:
        data = json.dumps(body, separators=(",", ":")).encode()
        if len(data) > self.settings.max_body_bytes:
            raise AgentError(
                f"the report is {len(data)} bytes, over [agent] max_body_bytes "
                f"({self.settings.max_body_bytes}); shorten the adapters' lookback or raise "
                "the limit on both sides"
            )
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
        }
        try:
            response = self._post(
                self.settings.server,
                path,
                headers,
                gzip.compress(data, compresslevel=5),
                timeout,
                max_bytes=MAX_REPLY_BYTES,
                context=self._context,
            )
        except net.RequestError as exc:
            raise AgentError(f"{path}: {exc.detail}") from None
        if response.status != 200:
            raise AgentError(
                f"{path}: HTTP {response.status} {_error_text(response.body)}", response.status
            )
        try:
            reply = json.loads(response.body)
        except (ValueError, RecursionError):
            raise AgentError(f"{path}: the hub's reply is not JSON") from None
        if not depth_ok(reply):
            raise AgentError(f"{path}: the hub's reply nests too deep")
        return reply


def tls_context(settings: AgentSettings) -> ssl.SSLContext:
    """Verification of the hub's certificate and host name, against ``[agent] ca_file`` when
    set, else the system trust store; TLS 1.2 at least. Always an explicit context, so a
    library that swapped Python's default for an unverified one changes nothing here."""
    try:
        context = ssl.create_default_context(
            cafile=None if settings.ca_file is None else str(settings.ca_file)
        )
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"[agent] ca_file {settings.ca_file}: {exc}") from None
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _result(action_id: str, out: bool | str | LedgerOutcome) -> Result:
    if out is LedgerOutcome.NOTHING_TO_DO:
        return Result(action_id, Outcome.NOTHING_TO_DO)
    if out is False or isinstance(out, LedgerOutcome):
        return Result(action_id, Outcome.NOT_EXECUTED)
    return Result(action_id, Outcome.DONE, out if isinstance(out, str) else None)


def _error_text(body: bytes) -> str:
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError, RecursionError):
        return ""
    return str(error)[:300] if isinstance(error, str) else ""
