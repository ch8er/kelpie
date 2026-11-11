"""Carrying out an answer from the chat (#300): the server side of answer buttons.

The chat side (:mod:`kelpie.alerts.answers`) checks a confirm press and hands the answer
here (:meth:`AnswerRunner.submit`): the question is already claimed in the button store, and
this writes the ledger's audit line (kind ``answer``, rule ``chat``, outcome ``pending``)
and queues the answer. The control loop then carries it out (:meth:`AnswerRunner.drain`,
in its own thread, never in the poller's): re-checked against the snapshot, a ``comment``
action through the task's own adapter (``params.expect``: the adapter re-reads the task
right before writing, :class:`~kelpie.core.ports.SupportsAnswerCheck`), then an ``unblock``
the same way (the board opted in to answers, which covers it), and the ledger entry gets
the outcome of each write. The claim is given back only when the comment was refused
before it reached the board; a crash leaves the question claimed and the entry
``pending``, which ``kelpie why`` and ``kelpie doctor`` show, and nothing retries it.

Answers have no rule mode: the three opt-ins are the mode (``answer_buttons`` on the
channel, ``chat_answers`` on the board, the global dry-run off), so with all three the
instance acts on that board and takes its act lock.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from kelpie.alerts import i18n
from kelpie.alerts.answers import AnswerControl, AnswerDesk, AnswerPress
from kelpie.alerts.buttons import ButtonStore
from kelpie.alerts.render import one_line
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import LedgerEntry, Outcome, to_json
from kelpie.core.model import Answer, Snapshot, Task
from kelpie.core.offers import Expect, answer_body, answers_from_ledger, stale
from kelpie.core.ports import StaleAnswer
from kelpie.core.snooze import CHAT_RULE
from kelpie.core.storage import SqliteLedgerStore

log = logging.getLogger(__name__)

ANSWERS_KEPT = timedelta(days=30)  # answers rule 13 shows as answered (the claims' life)
MAX_QUEUED = 100  # answers waiting for the control loop; more are refused (nothing written)


@dataclass(frozen=True, slots=True)
class AnswerBoards:
    """Which boards opted in to answers from the chat (``chat_answers``): the adapters'
    names (Hermes first) and, for an adapter with several boards, those boards' names."""

    adapters: frozenset[str] = frozenset()  # the whole adapter (one board, or every board)
    boards: frozenset[tuple[str, str]] = frozenset()  # (adapter, board)

    def __bool__(self) -> bool:
        return bool(self.adapters or self.boards)

    def on(self, task: Task | None) -> bool:
        if task is None or task.source is None:
            return False
        if task.board is not None and (task.source, task.board) in self.boards:
            return True
        return task.source in self.adapters


@dataclass(slots=True)
class AnswerRunner:
    """Takes confirmed answers from the chat and carries them out on the control loop."""

    store: SqliteLedgerStore
    engine: Engine
    boards: AnswerBoards
    dry_run: bool
    clock: Callable[[], datetime]
    buttons: ButtonStore | None = None  # gives a claim back when nothing was written
    latest: Callable[[], Snapshot | None] = lambda: None
    wake: Callable[[], None] = lambda: None
    desks: dict[str, AnswerDesk] = field(default_factory=dict)  # by channel name
    _queue: deque[tuple[AnswerPress, str]] = field(default_factory=deque)
    _closed: bool = False

    def control(self) -> AnswerControl:
        return AnswerControl(
            enabled=self._enabled,
            dry_run=lambda: self.dry_run,
            stale=self._stale,
            submit=self.submit,
            record_dry_run=self.record_dry_run,
        )

    @property
    def queued(self) -> int:
        return len(self._queue)

    # --- the chat side (the poller's thread) -----------------------------------------------

    def _enabled(self, task_id: str) -> bool:
        snapshot = self.latest()
        return snapshot is not None and self.boards.on(snapshot.task(task_id))

    def _stale(self, task_id: str, expect: object) -> str | None:
        snapshot = self.latest()
        if snapshot is None:
            return "Kelpie has not read the board yet"
        assert isinstance(expect, Expect)
        return stale(snapshot.task(task_id), expect)

    def submit(self, press: AnswerPress, now: datetime) -> None:
        """Record the answer ``pending`` and queue it for the control loop; raises (nothing
        recorded) when the queue is full or the ledger cannot be written."""
        if self._closed:
            raise RuntimeError("Kelpie is stopping")
        if len(self._queue) >= MAX_QUEUED:
            raise RuntimeError("too many answers waiting")
        entry = _entry(press, now, Outcome.PENDING)
        self.store.append(entry)
        self._queue.append((press, entry.fingerprint))
        self.wake()

    def record_dry_run(self, press: AnswerPress, now: datetime) -> None:
        """A confirm press under the global dry-run: recorded, nothing written."""
        self.store.append(_entry(press, now, Outcome.DRY_RUN))

    # --- the control loop ------------------------------------------------------------------

    def abandon(self) -> None:
        """Kelpie stops: every answer still queued was not written, so its entry becomes
        ``not_executed`` and its question is given back (nothing is told to the chat, whose
        poller is stopping too)."""
        self._closed = True
        self.desks = {}
        while self._queue:
            press, fingerprint = self._queue.popleft()
            try:
                self._refused(press, fingerprint, _params(press), "Kelpie stopped before writing")
            except Exception:
                log.exception("chat answer on %s not given back", one_line(press.task_id, 100))

    def drain(self, snapshot: Snapshot) -> None:
        """Carry out every queued answer (in the control loop's thread)."""
        while self._queue:
            press, fingerprint = self._queue.popleft()
            try:
                self._run(press, fingerprint, snapshot)
            except Exception:  # a bug: the entry stays pending for a person to look at
                log.exception("chat answer on %s failed", one_line(press.task_id, 100))

    def _run(self, press: AnswerPress, fingerprint: str, snapshot: Snapshot) -> None:
        task = snapshot.task(press.task_id)
        params = _params(press)
        if self.dry_run or not self.boards.on(task):
            self._refused(press, fingerprint, params, "answers are off for this board")
            return
        if (why := stale(task, press.expect)) is not None:
            self._refused(press, fingerprint, params, f"the question changed: {why}")
            return
        expect = {"expect": press.expect.params(), "question": press.question}
        comment = Action(
            ActionKind.COMMENT,
            CHAT_RULE,
            answer_body(press.offer, press.index, press.who),
            task_id=press.task_id,
            params=expect,
        )
        executor = self.engine.route(comment, snapshot)
        if executor is None or task is None or executor.name != task.source:
            self._refused(press, fingerprint, params, "no executor for the task's board")
            return
        try:
            done = executor.execute(comment)
        except StaleAnswer as exc:
            self._refused(press, fingerprint, params, str(exc), "the question changed")
            return
        except Exception as exc:  # it may have reached the board: the claim stays
            detail = f"the comment failed: {type(exc).__name__}: {exc}"
            self._finish(press, fingerprint, Outcome.FAILED, detail, params, written=False)
            return
        if done is False or isinstance(done, Outcome):
            self._refused(press, fingerprint, params, "the adapter refused the comment")
            return
        params["comment"] = "executed"
        unblock = Action(
            ActionKind.UNBLOCK,
            CHAT_RULE,
            f"A person chose option {press.label} of the options you offered, from the chat "
            "via Kelpie: see the comment above.",
            task_id=press.task_id,
            params=expect,
        )
        try:
            result = executor.execute(unblock)
            params["unblock"] = (
                "executed"
                if result is True or (result and not isinstance(result, Outcome))
                else "not_executed"
            )
        except StaleAnswer as exc:
            params["unblock"] = f"not_executed: {exc}"
        except Exception as exc:
            params["unblock"] = f"failed: {type(exc).__name__}"
            log.warning("chat answer: unblock of %s failed: %s", press.task_id, exc)
        self._finish(press, fingerprint, Outcome.EXECUTED, None, params, written=True)

    def _refused(
        self,
        press: AnswerPress,
        fingerprint: str,
        params: dict[str, object],
        why: str,
        chat: str | None = None,
    ) -> None:
        """Nothing reached the board: the question is free again."""
        if self.buttons is not None:
            try:
                self.buttons.release_question(press.question, press.ref_id)
            except Exception as exc:
                log.error("chat answer: question not released: %s", type(exc).__name__)
        params["comment"] = "not_executed"
        self._finish(
            press, fingerprint, Outcome.NOT_EXECUTED, why, params, written=False, chat=chat
        )

    def _finish(
        self,
        press: AnswerPress,
        fingerprint: str,
        outcome: Outcome,
        detail: str | None,
        params: dict[str, object],
        *,
        written: bool,
        chat: str | None = None,
    ) -> None:
        try:
            if not self.store.answer_outcome(fingerprint, outcome, detail, dict(params)):
                log.error("chat answer: its pending ledger entry is gone")
        except Exception as exc:  # the entry stays pending: a person looks
            log.error("chat answer: outcome not recorded: %s", type(exc).__name__)
        log.info(
            "chat answer %s on %s by user id %d: %s",
            press.label,
            one_line(press.task_id, 100),
            press.user_id,
            outcome.value,
        )
        desk = self.desks.get(press.channel)
        if desk is None:
            return
        task = one_line(press.task_id, 100)
        if written:
            key = "chat.answer.unblocked" if params.get("unblock") == "executed" else None
            text = _text(desk, key or "chat.answer.done", press.label, task)
        else:
            # Kelpie's fixed words only: an adapter's error (paths, CLI output) stays in the
            # ledger and the log.
            if outcome is Outcome.FAILED:
                why = "the write failed, it may or may not be on the task; see the Kelpie log"
            else:
                why = chat or detail or ""
            text = _text(desk, "chat.answer.failed", press.label, task, one_line(why, 200))
        try:
            desk.finished(press, written, text)
        except Exception as exc:
            log.error("chat answer: not reported to the chat: %s", type(exc).__name__)

    def answers(self, now: datetime) -> tuple[Answer, ...]:
        """``Snapshot.answers`` as of ``now``: the ledger's answers of :data:`ANSWERS_KEPT`."""
        return answers_from_ledger(self.store.answers(now - ANSWERS_KEPT))


def _text(desk: AnswerDesk, key: str, label: str, task: str, why: str = "") -> str:
    return i18n.text(desk.rendering.language, key, label=label, task=task, why=why)


def _params(press: AnswerPress) -> dict[str, object]:
    return {
        "question": press.question,
        "label": press.label,
        "by": press.who,
        "user_id": press.user_id,
        "name": one_line(press.name, 40),
        "offer_digest": press.offer.digest,
        "channel": press.channel,
        "board": press.board,
    }


def _entry(press: AnswerPress, now: datetime, outcome: Outcome) -> LedgerEntry:
    """The ledger's audit line of an answer: who (user id, cleaned name, what the board
    gets), the task, the question, the option and the offer's digest."""
    raw = to_json(["chat_answer", press.channel, press.question, press.user_id, now.isoformat()])
    params = _params(press)
    reason = f"{press.who} answered option {press.label} on {press.task_id} from {press.channel}."
    if outcome is Outcome.DRY_RUN:
        params["dry_run"] = True
        reason += " Not sent: Kelpie is in dry-run."
    return LedgerEntry(
        at=now,
        fingerprint=hashlib.sha256(raw.encode()).hexdigest()[:32],
        subject=f"chat:{press.channel}",
        rule=CHAT_RULE,
        kind=ActionKind.ANSWER,
        mode=Mode.ACT,
        outcome=outcome,
        reason=reason,
        task_id=press.task_id,
        executor=press.channel,
        params=params,
    )
