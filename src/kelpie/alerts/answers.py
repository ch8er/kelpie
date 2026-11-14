"""Answering an agent's question from the chat (#300): the presses of answer buttons.

The design and its threat model are in docs/security.md ("Answering an agent's question
from the chat"). In short: a digest item whose agent offered options (``Options:`` in the
owner's latest comment, :mod:`kelpie.core.offers`) gets a button per option on a Telegram
channel with ``answer_buttons = true``. A press never writes: it answers with a
confirmation that restates the chosen option's full text **from Kelpie's record**, with one
"Send B" button (a new single-use reference, good for :data:`CONFIRM_TTL`). Only the confirm
press of the same person writes, and only through the app's :class:`AnswerControl`: the
question is claimed (one answer per question, across digest messages), a ledger entry is
recorded ``pending``, and the comment (then, where the board opted in, the unblock) is
carried out by the board's adapter on the control loop, re-checked against the board
right before it is written.

Every guard runs on both presses, in this order, before anything is written:

1. the press passed the checks of every press (well-formed, not rate limited, an allowed
   user id, the configured chat; :meth:`ButtonPoller._sender`), and its reference is one
   Kelpie signed for this chat and this message (``bad_ref``), the confirm pressed by the
   person who chose the option (``bad_ref``);
2. it is fresh: an option button for :data:`~kelpie.alerts.buttons.REF_TTL` and only on
   the latest message of its alert, a confirm for :data:`CONFIRM_TTL` and once
   (``expired_ref``, ``used_ref``), and the question is not answered yet (``used_question``);
3. answers are on for the task's board (``not_enabled``) and Kelpie is not in dry-run
   (``dry_run``: recorded, nothing sent, the question not used up);
4. with ``answer_users``, the presser is the item's own person (``not_approver``);
5. the question still holds against the latest snapshot: the task is blocked with the
   same block, held by the same owner, whose latest comment offers the same options
   (``stale_offer``).

The comment is built from the record's option index and the re-read offer, never from
anything a press carries or from the message's text (:func:`kelpie.core.offers.answer_body`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from kelpie.alerts import i18n
from kelpie.alerts.buttons import (
    CONFIRM,
    MAX_NAME,
    OPTION,
    REF_TTL,
    AnswerRef,
    ButtonStore,
    Claim,
    _name,
    _remaining,
    answer_data,
    int_id,
    looks_like_answer,
    parse_answer,
)
from kelpie.alerts.render import (
    DEFAULT_RENDERING,
    MAX_ID,
    MAX_TEXT,
    RenderSettings,
    one_line,
    utf16_len,
)
from kelpie.core.offers import Expect, Offer, offer_from_params

if TYPE_CHECKING:
    from kelpie.alerts.buttons import ButtonPoller

log = logging.getLogger(__name__)

CONFIRM_TTL = timedelta(minutes=2)  # a confirm button's life
REPORT_TIMEOUT = 3.0  # seconds per Bot API call reporting an outcome (on the control loop)
SKEW = timedelta(minutes=5)  # a reference issued this far "in the future" is still taken
REASONS = ("stale_offer", "not_enabled", "dry_run", "not_approver", "used_question")


@dataclass(frozen=True, slots=True)
class AnswerPress:
    """A confirmed answer: option ``index`` of ``offer`` for ``question`` on ``task_id``,
    by the person with ``user_id``. ``who`` is what the board gets (an owner id from
    ``answer_users``, else "Telegram user <id>"); ``name`` is their chat name, cleaned,
    for the chat's acknowledgement only. ``ref_id`` is the confirm reference (the claim),
    ``message_id`` the confirmation, ``digest_message_id`` and ``item`` the digest item."""

    channel: str
    chat_id: str
    task_id: str
    board: str | None
    question: str
    offer: Offer
    index: int
    expect: Expect
    user_id: int
    who: str
    name: str
    ref_id: int
    message_id: int
    digest_message_id: int
    item: int

    @property
    def label(self) -> str:
        return self.offer.options[self.index].label


@dataclass(slots=True)
class AnswerControl:
    """What the app gives the chat side to take answers: whether answers are on for a
    task's board, whether Kelpie is in dry-run, the re-check against the latest snapshot
    (why the question changed, or None), and the callbacks that record a dry-run press and
    hand a confirmed answer on (its ledger entry ``pending``, then the write on the control
    loop; an exception: nothing was recorded)."""

    enabled: Callable[[str], bool]
    dry_run: Callable[[], bool]
    stale: Callable[[str, Expect], str | None]
    submit: Callable[[AnswerPress, datetime], None]
    record_dry_run: Callable[[AnswerPress, datetime], None]


class AnswerDesk:
    """Takes the presses of answer buttons for one Telegram channel (the poller hands them
    over after the checks every press passes) and reports a written answer back to the
    chat (:meth:`finished`)."""

    def __init__(
        self,
        poller: ButtonPoller,
        control: AnswerControl,
        answer_users: Mapping[int, str] | None = None,
        rendering: RenderSettings = DEFAULT_RENDERING,
    ) -> None:
        self.poller = poller
        self.control = control
        self.store: ButtonStore = poller.store
        self.channel = poller.channel
        self.chat_id = poller.chat_id
        self.answer_users = dict(answer_users or {})
        self.rendering = rendering

    def __repr__(self) -> str:
        return f"AnswerDesk(channel={self.channel!r})"

    def owns(self, data: object) -> bool:
        return looks_like_answer(data)

    # --- a press ---------------------------------------------------------------------------

    def take(
        self,
        query_id: object,
        data: object,
        user_id: int,
        sender: Mapping[str, Any],
        message: Mapping[str, Any],
    ) -> None:
        parsed = parse_answer(data, self.store.key(), self.chat_id)
        if parsed is None:
            self._reject("bad_ref", query_id)
            return
        kind, index, ref_id = parsed
        ref = self.store.answer_ref(ref_id)
        message_id = int_id(message.get("message_id"))
        if (
            ref is None
            or ref.kind != kind
            or ref.channel != self.channel
            or ref.chat_id != self.chat_id
            or ref.message_id is None
            or ref.message_id != message_id
        ):
            self._reject("bad_ref", query_id)  # forged, copied onto another message, ...
            return
        offer = offer_from_params(ref.offer)
        expect = Expect.from_params(ref.offer)
        if offer is None or expect is None:
            self._reject("bad_ref", query_id)
            return
        name = _name(sender) or f"user {user_id}"
        try:
            if kind == OPTION:
                assert index is not None
                self._option(query_id, ref, offer, expect, index, user_id)
            else:
                self._confirm(query_id, ref, offer, expect, user_id, name)
        except Exception as exc:  # a store failure or a bug: nothing more is done
            log.error("telegram answer: press not handled: %s", type(exc).__name__)
            self._answer(query_id, i18n.text(self.rendering.language, "chat.failed"))
            self._notify("chat_action", self.channel, "failed")

    def _option(
        self, query_id: object, ref: AnswerRef, offer: Offer, expect: Expect, index: int, user: int
    ) -> None:
        """An option press: checked, then answered with a confirmation (nothing written)."""
        if offer.option(index) is None:
            self._reject("bad_ref", query_id)
            return
        now = self.poller.control.clock()
        fresh = ref.issued_at <= now + SKEW and now - ref.issued_at <= REF_TTL
        if not fresh or not self.store.is_latest_message(
            self.channel, self.chat_id, ref.alert, ref.message_id
        ):
            self._expired(query_id, ref.message_id)
            return
        if not self._guards(query_id, ref, offer, expect, index, user, None, now):
            return
        confirm = AnswerRef(
            kind=CONFIRM,
            channel=self.channel,
            chat_id=self.chat_id,
            alert=ref.alert,
            task_id=ref.task_id,
            board=ref.board,
            item=ref.item,
            question=ref.question,
            offer=ref.offer,
            issued_at=now,
            choice=index,
            user_id=user,
            parent=ref.id,
        )
        cid = self.store.issue_answer(confirm)
        data = answer_data(self.store.key(), self.chat_id, CONFIRM, None, cid)
        lang = self.rendering.language
        label = offer.options[index].label
        task = one_line(ref.task_id, MAX_ID)
        head = i18n.text(lang, "chat.confirm", label=label, task=task)
        quoted = one_line(offer.quoted(index), 260)  # from Kelpie's record, never the chat's
        text = f"{head}\n{quoted}"
        button = {"text": i18n.text(lang, "button.send", label=label), "callback_data": data}
        body = {
            "chat_id": self.chat_id,
            "text": text,
            "entities": [
                {"type": "code", "offset": utf16_len(head) + 1, "length": utf16_len(quoted)}
            ],
            "reply_parameters": {"message_id": ref.message_id, "allow_sending_without_reply": True},
            "reply_markup": {"inline_keyboard": [[button]]},
            "link_preview_options": {"is_disabled": True},
        }
        sent = self.poller._call("sendMessage", body)
        sent_id = int_id(sent.get("message_id")) if isinstance(sent, Mapping) else None
        if sent_id is None:
            self._answer(query_id, i18n.text(lang, "chat.failed"))
            self._notify("chat_action", self.channel, "failed")
            return
        self.store.bind_answers([cid], sent_id)
        self._answer(query_id, i18n.text(lang, "chat.confirm.toast", label=label))
        self._notify("chat_action", self.channel, "confirming")

    def _confirm(
        self,
        query_id: object,
        ref: AnswerRef,
        offer: Offer,
        expect: Expect,
        user: int,
        name: str,
    ) -> None:
        """A confirm press: checked again, the question claimed, the answer handed on."""
        index = ref.choice
        if index is None or offer.option(index) is None or ref.user_id != user:
            self._reject("bad_ref", query_id)  # only the person who chose may confirm
            return
        now = self.poller.control.clock()
        if not ref.issued_at <= now + SKEW or now - ref.issued_at > CONFIRM_TTL:
            self._expired(query_id, ref.message_id)
            return
        if ref.used_at is not None:
            self._reject("used_ref", query_id, self._already(self.store.claim(ref.question)))
            return
        if not self._guards(query_id, ref, offer, expect, index, user, name, now):
            return
        if not self.store.use_answer_ref(ref.id, now):
            self._reject("used_ref", query_id, self._already(self.store.claim(ref.question)))
            return
        press = self._press(ref, offer, expect, index, user, name)
        held = self.store.claim_question(ref.question, press.label, press.who, user, ref.id, now)
        if held is not None:
            self._reject("used_question", query_id, self._already(held))
            return
        try:
            self.control.submit(press, now)
        except Exception as exc:  # nothing recorded, nothing sent: the question is free again
            log.error("telegram answer: not recorded: %s", type(exc).__name__)
            self.store.release_question(ref.question, ref.id)
            self._answer(query_id, i18n.text(self.rendering.language, "chat.failed"))
            self._notify("chat_action", self.channel, "failed")
            return
        log.info(
            "telegram answer: user id %d chose %s on %s",
            user,
            press.label,
            one_line(ref.task_id, MAX_ID),
        )
        lang = self.rendering.language
        self._answer(query_id, i18n.text(lang, "chat.sending", label=press.label))
        self._settle_digest(ref)
        self._notify("chat_action", self.channel, "answered")

    def _guards(
        self,
        query_id: object,
        ref: AnswerRef,
        offer: Offer,
        expect: Expect,
        index: int,
        user: int,
        name: str | None,
        now: datetime,
    ) -> bool:
        """The guards both presses pass (an answered question, the board's opt-in, the
        item's person, a changed question), and for the confirm (``name`` set) the global
        dry-run; a refused press is answered and counted."""
        lang = self.rendering.language
        if (claim := self.store.claim(ref.question)) is not None:
            self._reject("used_question", query_id, self._already(claim))
            return False
        if not self.control.enabled(ref.task_id):
            self._reject("not_enabled", query_id, i18n.text(lang, "chat.not_enabled"))
            return False
        approver = ref.offer.get("approver")
        if self.answer_users:
            mapped = self.answer_users.get(user)
            if (
                not isinstance(approver, str)
                or mapped is None
                or mapped.casefold() != approver.casefold()
            ):
                who = one_line(approver, MAX_NAME) if isinstance(approver, str) else "-"
                self._reject(
                    "not_approver", query_id, i18n.text(lang, "chat.not_approver", who=who)
                )
                return False
        if (why := self.control.stale(ref.task_id, expect)) is not None:
            log.info("telegram answer: %s changed: %s", one_line(ref.task_id, MAX_ID), why)
            self._reject("stale_offer", query_id, i18n.text(lang, "chat.stale_offer"))
            return False
        # Under the global dry-run the option press still gets its confirmation (it writes
        # nothing); the confirm, which would write, is recorded with dry_run and refused,
        # and the question stays open.
        if name is not None and self.control.dry_run():
            if not self.store.use_answer_ref(ref.id, now):  # one record per confirm
                self._reject("used_ref", query_id)
                return False
            press = self._press(ref, offer, expect, index, user, name)
            try:
                self.control.record_dry_run(press, now)
            except Exception as exc:
                log.error("telegram answer: dry-run press not recorded: %s", type(exc).__name__)
            self._reject("dry_run", query_id, i18n.text(lang, "chat.dry_run"))
            return False
        return True

    def _press(
        self, ref: AnswerRef, offer: Offer, expect: Expect, index: int, user: int, name: str
    ) -> AnswerPress:
        mapped = self.answer_users.get(user)
        who = f"{mapped} (Telegram user {user})" if mapped else f"Telegram user {user}"
        assert ref.message_id is not None  # matched against the press's message
        digest_message = ref.message_id
        if ref.kind == CONFIRM and ref.parent is not None:
            parent = self.store.answer_ref(ref.parent)
            if parent is not None and parent.message_id is not None:
                digest_message = parent.message_id
        return AnswerPress(
            channel=self.channel,
            chat_id=self.chat_id,
            task_id=ref.task_id,
            board=ref.board,
            question=ref.question,
            offer=offer,
            index=index,
            expect=expect,
            user_id=user,
            who=who,
            name=name,
            ref_id=ref.id,
            message_id=ref.message_id,
            digest_message_id=digest_message,
            item=ref.item,
        )

    def _already(self, claim: Claim | None) -> str | None:
        """ "Already answered: B by … at …", or None (the neutral "Not allowed.")."""
        if claim is None:
            return None
        when = claim.at.astimezone(self.rendering.tz).strftime("%Y-%m-%d %H:%M %Z")
        return i18n.text(
            self.rendering.language,
            "chat.already_answered",
            label=one_line(claim.label, 4),
            who=one_line(claim.who, MAX_NAME * 2),
            at=when,
        )

    # --- after the press -------------------------------------------------------------------

    def _settle_digest(self, ref: AnswerRef) -> None:
        """Take the confirmation's button off and the answered item's option buttons off
        the digest (best effort): the question is answered."""
        if ref.message_id is not None:
            self.poller._call(
                "editMessageReplyMarkup",
                {
                    "chat_id": self.chat_id,
                    "message_id": ref.message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        parent = self.store.answer_ref(ref.parent) if ref.parent is not None else None
        if parent is None or parent.message_id is None:
            return
        shown = self.store.shown(self.channel, self.chat_id, parent.message_id)
        used = self.store.used_on(self.channel, self.chat_id, parent.message_id)
        answered = self.store.answered_on(self.channel, self.chat_id, parent.message_id)
        markup = _remaining(None if shown is None else shown.markup, used, answered)
        if not self.store.is_latest_message(
            self.channel, self.chat_id, parent.alert, parent.message_id
        ):
            markup = {"inline_keyboard": []}
        self.poller._call(
            "editMessageReplyMarkup",
            {"chat_id": self.chat_id, "message_id": parent.message_id, "reply_markup": markup},
        )

    def finished(self, press: AnswerPress, written: bool, text: str) -> None:
        """The answer was carried out (``written``: its comment is on the board) or not
        (``text`` says why, in Kelpie's words): said on the confirmation, as a reply to it
        when the edit fails. Best effort: the outcome is in the ledger either way."""
        lang = self.rendering.language
        task = one_line(press.task_id, MAX_ID)
        mark = "✅" if written else "⚠️"
        head = f"{mark} {text}"
        name = one_line(press.name, MAX_NAME)
        line = i18n.text(lang, "chat.answered", label=press.label, task=task, who=name)
        full = f"{head}\n{line}" if written else head
        if utf16_len(full) > MAX_TEXT:
            full = full[: MAX_TEXT // 2]
        body = {
            "chat_id": self.chat_id,
            "message_id": press.message_id,
            "text": full,
            "link_preview_options": {"is_disabled": True},
        }
        if self.poller._call("editMessageText", body, REPORT_TIMEOUT) is None:
            reply = {
                "chat_id": self.chat_id,
                "text": full,
                "reply_parameters": {
                    "message_id": press.message_id,
                    "allow_sending_without_reply": True,
                },
                "link_preview_options": {"is_disabled": True},
                "disable_notification": True,
            }
            self.poller._call("sendMessage", reply, REPORT_TIMEOUT)

    def _expired(self, query_id: object, message_id: int | None) -> None:
        """A genuine answer button that has expired: say so, and take that message's
        buttons off (best effort)."""
        if message_id is not None:
            self.poller._call(
                "editMessageReplyMarkup",
                {
                    "chat_id": self.chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        self._reject("expired_ref", query_id, i18n.text(self.rendering.language, "chat.expired"))

    def _reject(self, reason: str, query_id: object, answer: str | None = None) -> None:
        self.poller._reject(reason, query_id, answer)

    def _answer(self, query_id: object, text: str) -> None:
        self.poller._answer(query_id, text)

    def _notify(self, method: str, *args: str) -> None:
        self.poller._notify(method, *args)
