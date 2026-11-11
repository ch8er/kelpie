"""Answering an agent's question from Telegram (#300), against a local fake of the Bot API:
the options on the digest, the confirmation, and every press the design refuses (forged,
replayed, expired, from another chat or user, not the item's person, a changed question,
answers off, dry-run, a question answered already, typed text).

Waits are on events (a request answered, an update counted), never on wall time."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.alerts import Delivery, render
from kelpie.alerts.answers import CONFIRM_TTL, AnswerControl, AnswerPress
from kelpie.alerts.buttons import (
    REF_TTL,
    ButtonPoller,
    ButtonStore,
    ChatControl,
    answer_data,
    parse_answer,
)
from kelpie.alerts.telegram import load_telegram
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, AlertItem, Transition
from kelpie.core.offers import Expect, ItemOffer, Offer, parse_offer, question_key
from kelpie.metrics import Metrics
from tests.alerts.fake_telegram import FakeTelegram, fake_telegram

TOKEN = "123456:ABCdef_SECRET-token-0123456789abc"
ENV = "KELPIE_TEST_TELEGRAM_TOKEN"
CHAT = "-100123"
ALICE = 4242
BOB = 4343
MALLORY = 666
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=2)
KEY = bytes(range(32))
QUESTION = "Which schema?\nOptions:\nA) target schema v3\nB) keep v2 and add a shim\nRecommend: B"


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Answers:
    """The app's side, faked: answers on, no dry-run, the question unchanged."""

    on: bool = True
    dry: bool = False
    changed: str | None = None
    fail: bool = False
    submitted: list[AnswerPress] = field(default_factory=list)
    dry_runs: list[AnswerPress] = field(default_factory=list)
    checked: list[tuple[str, Expect]] = field(default_factory=list)

    def control(self) -> AnswerControl:
        return AnswerControl(
            enabled=lambda task: self.on,
            dry_run=lambda: self.dry,
            stale=self._stale,
            submit=self._submit,
            record_dry_run=lambda press, now: self.dry_runs.append(press),
        )

    def _stale(self, task: str, expect: Expect) -> str | None:
        self.checked.append((task, expect))
        return self.changed

    def _submit(self, press: AnswerPress, now: datetime) -> None:
        if self.fail:
            raise RuntimeError("ledger down")
        self.submitted.append(press)


class SignalingMetrics(Metrics):
    def __init__(self, fake: FakeTelegram) -> None:
        super().__init__()
        self.fake = fake

    def chat_action(self, channel: str, outcome: str) -> None:
        super().chat_action(channel, outcome)
        self.fake.notify()

    def chat_rejected(self, channel: str, reason: str) -> None:
        super().chat_rejected(channel, reason)
        self.fake.notify()

    def alert_sent(self, channel: str) -> None:
        super().alert_sent(channel)
        self.fake.notify()


def offer_item(task: str = "T-1", text: str = QUESTION, approver: str | None = "ceo") -> ItemOffer:
    offer = parse_offer(text)
    assert isinstance(offer, Offer)
    question = question_key(task, "dev-1", SINCE, offer.digest)
    return ItemOffer(offer, question, Expect("dev-1", SINCE, offer.digest), approver)


def digest(*items: AlertItem, **kw: Any) -> AlertEvent:
    return AlertEvent(
        "needs_you", "escalate", "Needs you (ceo).", Severity.WARNING,
        items=items, at=NOW, **({"state": "needs_you:ceo"} | kw),
    )  # fmt: skip


def asked(task: str = "T-1", **kw: Any) -> AlertItem:
    return AlertItem(
        task, f"Decide on {task}", timedelta(hours=2), "which schema?", 0,
        snooze_task=task, offer=kw.pop("offer", offer_item(task)), **kw,
    )  # fmt: skip


@dataclass
class Rig:
    fake: FakeTelegram
    store: ButtonStore
    answers: Answers
    clock: Clock
    metrics: Metrics
    delivery: Delivery
    poller: ButtonPoller

    def send(self, event: AlertEvent) -> tuple[int, dict[str, Any]]:
        before = sum(self.metrics.alerts_sent.values())
        self.delivery.deliver(event)
        self.fake.wait_for(lambda: sum(self.metrics.alerts_sent.values()) > before)
        return self.fake.sent()[-1]

    def handled(self) -> int:
        return sum(self.metrics.chat_actions.values())

    def raw(
        self, message_id: int, body: dict[str, Any], data: str, user: int = ALICE,
        chat: int | str = CHAT,
    ) -> None:  # fmt: skip
        before = self.handled()
        self.fake.press(message_id, body, data, user, chat)
        self.fake.wait_for(lambda: self.handled() > before)

    def press(self, sent: tuple[int, dict[str, Any]], row: int, col: int, **kw: Any) -> None:
        message_id, body = sent
        data = body["reply_markup"]["inline_keyboard"][row][col]["callback_data"]
        self.raw(message_id, body, data, **kw)

    def choose(
        self, sent: tuple[int, dict[str, Any]], col: int = 1, **kw: Any
    ) -> tuple[int, dict[str, Any]]:
        """Press option ``col`` on the digest's answer row (the last row): the confirmation."""
        rows = sent[1]["reply_markup"]["inline_keyboard"]
        before = len(self.fake.sent())
        self.press(sent, len(rows) - 1, col, **kw)
        assert len(self.fake.sent()) == before + 1, self.toasts()
        return self.fake.sent()[-1]

    def toasts(self) -> list[str]:
        return [b["text"] for b in self.fake.calls("answerCallbackQuery")]

    def rejected(self) -> dict[str, int]:
        return {r: n for (_, r), n in self.metrics.chat_rejections.items()}


def _rig(
    fake: FakeTelegram, tmp_path: Path, answers: Answers | None = None, **entry_kw: Any
) -> Rig:
    store = ButtonStore(tmp_path / "kelpie.buttons.db")
    clock, metrics = Clock(), SignalingMetrics(fake)
    metrics.set_chat_channels(["telegram"])
    answers = answers or Answers()
    control = ChatControl(
        store, lambda p, n: n, clock, metrics, poll_timeout=0, backoff=0.01, min_interval=0.0,
        answers=answers.control(),
    )  # fmt: skip
    entry: dict[str, Any] = {
        "type": "telegram", "token_env": ENV, "chat_id": CHAT, "api_url": fake.url,
        "buttons": True, "answer_buttons": True, "allowed_user_ids": [ALICE, BOB],
        **entry_kw,
    }  # fmt: skip
    settings = load_telegram(entry, "test")
    channel, poller = settings.build_with_buttons(control, {ENV: TOKEN})
    delivery = Delivery([channel], metrics, pollers=[poller])
    return Rig(fake, store, answers, clock, metrics, delivery, poller)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    with fake_telegram() as fake:
        built = _rig(fake.bot(), tmp_path)
        try:
            yield built
        finally:
            built.delivery.close(1.0)


@pytest.fixture
def make(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []
    with fake_telegram() as fake:
        fake.bot()

        def build(answers: Answers | None = None, **kw: Any) -> Rig:
            r = _rig(fake, tmp_path, answers, **kw)
            made.append(r)
            return r

        try:
            yield build
        finally:
            for r in made:
                r.delivery.close(1.0)


# --- the reference -------------------------------------------------------------------------


def test_answer_references_are_short_signed_and_carry_no_text() -> None:
    option = answer_data(KEY, CHAT, "a", 1, 0x2A)
    confirm = answer_data(KEY, CHAT, "c", None, 0x2A)
    assert option.startswith("a1.2a.") and confirm.startswith("c.2a.")
    assert len(answer_data(KEY, "-1001234567890", "a", 3, 2**60 - 1).encode()) <= 64
    assert parse_answer(option, KEY, CHAT) == ("a", 1, 0x2A)
    assert parse_answer(confirm, KEY, CHAT) == ("c", None, 0x2A)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda d: d[:-1],
        lambda d: d[:-1] + ("A" if d[-1] != "A" else "B"),
        lambda d: "a2" + d[2:],  # another option under the same MAC
        lambda d: "c" + d[2:],  # the option's reference as a confirm
        lambda d: d.replace(".2a.", ".2b."),
        lambda d: d + " ",
        lambda d: None,
    ],
)
def test_a_forged_answer_reference_is_refused(mangle: Callable[[str], Any]) -> None:
    assert parse_answer(mangle(answer_data(KEY, CHAT, "a", 1, 0x2A)), KEY, CHAT) is None


def test_an_answer_reference_is_bound_to_its_chat_and_key() -> None:
    data = answer_data(KEY, CHAT, "a", 0, 5)
    assert parse_answer(data, KEY, "-100999") is None
    assert parse_answer(data, bytes(32), CHAT) is None
    with pytest.raises(ValueError):
        answer_data(KEY, CHAT, "a", 4, 5)
    with pytest.raises(ValueError):
        answer_data(KEY, CHAT, "c", 1, 5)


# --- the digest ----------------------------------------------------------------------------


def test_the_digest_shows_every_option_and_a_button_each(rig: Rig) -> None:
    _, body = rig.send(digest(asked()))
    text = body["text"]
    assert "Options:" in text
    assert "<code>A) target schema v3</code>" in text
    assert "<code>B) keep v2 and add a shim</code> (recommended)" in text
    rows = body["reply_markup"]["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["1. Snooze 1d", "3d", "7d"]
    assert [b["text"] for b in rows[1]] == ["1. A", "1. B"]
    for button in rows[1]:
        assert "T-1" not in button["callback_data"] and "schema" not in button["callback_data"]


def test_only_three_items_get_buttons_the_others_wait_for_the_next_digest(rig: Rig) -> None:
    _, body = rig.send(digest(*(asked(f"T-{n}") for n in range(1, 5))))
    rows = body["reply_markup"]["inline_keyboard"]
    answer_rows = [r for r in rows if r[0]["text"].endswith(". A")]
    assert [r[0]["text"] for r in answer_rows] == ["1. A", "2. A", "3. A"]
    assert body["text"].count("Answer in the next digest.") == 1


def test_an_answered_or_refused_offer_gets_no_buttons(rig: Rig) -> None:
    from kelpie.core.offers import Answered

    done = offer_item()
    done = ItemOffer(done.offer, done.question, done.expect, "ceo", Answered("B", "ceo", NOW))
    refused = AlertItem("T-2", "Other", offer_refused="mixed label styles")
    _, body = rig.send(digest(asked(offer=done), refused))
    assert "Answered B by ceo at 2026-10-19 10:00 UTC" in body["text"]
    assert "Options not offered as buttons: mixed label styles" in body["text"]
    rows = body["reply_markup"]["inline_keyboard"]
    assert not [r for r in rows if r[0]["text"].endswith(". A")]


def test_options_are_shown_only_with_answer_buttons() -> None:
    event = digest(asked())
    assert "Options" not in render.plain(event)
    _, _, targets = render.telegram_message_full(event)
    assert targets == ()
    _, _, resolved = render.telegram_message_full(
        digest(asked(), transition=Transition.RESOLVED), render.RenderSettings(answers=True)
    )
    assert resolved == ()


def test_answer_buttons_are_off_by_default_and_need_the_allow_list() -> None:
    entry = {"type": "telegram", "token_env": ENV, "chat_id": CHAT}
    off = load_telegram(entry, "t")
    assert not off.answer_buttons and not off.has_buttons
    on = load_telegram({**entry, "answer_buttons": True}, "t")
    assert on.buttons_problem() is not None
    stray = load_telegram(
        {**entry, "answer_buttons": True, "allowed_user_ids": [ALICE],
         "answer_users": {str(BOB): "bob"}}, "t",
    )  # fmt: skip
    assert "answer_users" in (stray.buttons_problem() or "")
    for bad in ({"x": "a"}, {"1": ""}, {"1": "a b"}, [1], {"0": "a"}):
        with pytest.raises(ValueError, match="answer_users"):
            load_telegram({**entry, "answer_users": bad}, "t")


# --- a press, the confirmation, the confirm ------------------------------------------------


def test_an_option_press_asks_for_confirmation_restating_the_option(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent, col=1)
    _, body = confirmation
    assert body["text"] == (
        "Send option B to T-1? The agent's option, quoted:\nB) keep v2 and add a shim"
    )
    (code,) = body["entities"]
    quoted = body["text"].encode("utf-16-le")[2 * code["offset"] :].decode("utf-16-le")
    assert quoted == "B) keep v2 and add a shim"
    assert body["reply_parameters"]["message_id"] == sent[0]
    ((button,),) = body["reply_markup"]["inline_keyboard"]
    assert button["text"] == "Send B" and button["callback_data"].startswith("c.")
    assert rig.toasts() == ["Confirm below to send B."]
    assert rig.answers.submitted == []  # nothing written by an option press


def test_the_confirm_hands_the_answer_on_and_settles_the_buttons(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent, col=1)
    rig.press(confirmation, 0, 0)
    (press,) = rig.answers.submitted
    assert (press.task_id, press.label, press.user_id) == ("T-1", "B", ALICE)
    assert press.who == "Telegram user 4242" and press.name == "Alice"
    assert press.offer.quoted(press.index) == "B) keep v2 and add a shim"
    assert press.digest_message_id == sent[0] and press.message_id == confirmation[0]
    assert rig.toasts()[-1] == "Sending option B."
    assert rig.metrics.chat_actions[("telegram", "answered")] == 1
    edits = rig.fake.calls("editMessageReplyMarkup")
    assert {
        "chat_id": CHAT,
        "message_id": confirmation[0],
        "reply_markup": {"inline_keyboard": []},
    } in edits
    digest_edit = [e for e in edits if e["message_id"] == sent[0]][-1]
    rows = digest_edit["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in r] for r in rows] == [["1. Snooze 1d", "3d", "7d"]]


def test_the_outcome_is_reported_on_the_confirmation(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent)
    rig.press(confirmation, 0, 0)
    (press,) = rig.answers.submitted
    desk = rig.poller.desk
    assert desk is not None
    desk.finished(press, True, "Option B sent to T-1.")  # type: ignore[attr-defined]
    edit = rig.fake.calls("editMessageText")[-1]
    assert edit["message_id"] == confirmation[0]
    assert edit["text"] == "✅ Option B sent to T-1.\nAnswered B on T-1 by Alice"
    assert "parse_mode" not in edit


def test_with_answer_users_the_board_gets_the_owner_id(make: Callable[..., Rig]) -> None:
    rig = make(answer_users={str(ALICE): "ceo"})
    sent = rig.send(digest(asked()))
    rig.press(rig.choose(sent), 0, 0)
    assert rig.answers.submitted[0].who == "ceo (Telegram user 4242)"


# --- refused presses (the design's acceptance cases) ---------------------------------------


def test_a_forged_or_edited_reference_writes_nothing(rig: Rig) -> None:
    message_id, body = sent = rig.send(digest(asked()))
    data = body["reply_markup"]["inline_keyboard"][1][1]["callback_data"]
    rig.raw(message_id, body, data[:-1] + ("A" if data[-1] != "A" else "B"))
    rig.raw(message_id, body, "a1.ffff.AAAAAAAAAAAAAAAAAAAAAA")
    rig.raw(message_id, body, "a3" + data[2:])  # an index the record does not have
    assert rig.rejected() == {"bad_ref": 3}
    assert rig.toasts() == ["Not allowed."] * 3
    assert len(rig.fake.sent()) == 1 and rig.answers.submitted == []
    assert sent[0] == message_id


def test_a_valid_reference_copied_onto_another_message_is_refused(rig: Rig) -> None:
    first = rig.send(digest(asked()))
    other = rig.send(digest(asked("T-2"), state="needs_you:cfo"))
    data = first[1]["reply_markup"]["inline_keyboard"][1][0]["callback_data"]
    rig.raw(other[0], other[1], data)
    assert rig.rejected() == {"bad_ref": 1}


def test_an_index_beyond_the_offer_is_refused(rig: Rig) -> None:
    message_id, body = rig.send(digest(asked()))
    ref_id = parse_answer(body["reply_markup"]["inline_keyboard"][1][0]["callback_data"],
                          rig.store.key(), CHAT)[2]  # type: ignore[index]  # fmt: skip
    rig.raw(message_id, body, answer_data(rig.store.key(), CHAT, "a", 3, ref_id))
    assert rig.rejected() == {"bad_ref": 1} and rig.toasts() == ["Not allowed."]


def test_a_press_from_another_chat_or_a_stranger_gets_no_answer(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    rig.press(sent, 1, 0, chat=-100999)
    rig.press(sent, 1, 0, user=MALLORY)
    assert rig.rejected() == {"wrong_chat": 1, "unknown_user": 1}
    assert rig.toasts() == [] and len(rig.fake.sent()) == 1


def test_only_the_person_who_chose_may_confirm(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent)
    rig.press(confirmation, 0, 0, user=BOB)
    assert rig.rejected() == {"bad_ref": 1} and rig.answers.submitted == []


def test_a_used_or_replayed_confirm_writes_once(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent)
    rig.press(confirmation, 0, 0)
    rig.press(confirmation, 0, 0)  # pressed again
    assert len(rig.answers.submitted) == 1
    assert rig.rejected() == {"used_ref": 1}
    assert rig.toasts()[-1].startswith("Already answered: B by Telegram user 4242 at")


def test_the_same_update_is_never_taken_twice(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    message_id, body = rig.choose(sent)
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    before = rig.handled()
    with rig.fake.batch():
        update = rig.fake.press(message_id, body, data, ALICE, CHAT)
        rig.fake.updates.append(dict(next(u for u in rig.fake.updates if u["update_id"] == update)))
    rig.fake.wait_for(lambda: rig.handled() > before)
    assert len(rig.answers.submitted) == 1


def test_another_option_of_an_answered_question_is_refused_on_any_message(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    a = rig.choose(sent, col=0)
    b = rig.choose(sent, col=1, user=BOB)
    rig.press(b, 0, 0, user=BOB)
    rig.press(a, 0, 0)  # Alice confirms A after Bob's B went: one answer per question
    assert [p.label for p in rig.answers.submitted] == ["B"]
    assert rig.rejected() == {"used_question": 1}
    assert rig.toasts()[-1].startswith("Already answered: B by Telegram user 4343")
    later = rig.send(digest(asked(), headline="again"))  # a newer digest of the same question
    rig.press(later, 1, 0)
    assert rig.rejected() == {"used_question": 2}


def test_an_old_option_button_expires(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    rig.clock.now = NOW + REF_TTL + timedelta(seconds=1)
    rig.press(sent, 1, 0)
    assert rig.rejected() == {"expired_ref": 1} and rig.toasts() == ["This button has expired."]


def test_an_option_button_on_an_older_message_of_the_alert_expires(rig: Rig) -> None:
    first = rig.send(digest(asked()))
    rig.send(digest(asked(), headline="changed"))
    rig.press(first, 1, 0)
    assert rig.rejected() == {"expired_ref": 1}


def test_a_confirm_expires_after_two_minutes(rig: Rig) -> None:
    confirmation = rig.choose(rig.send(digest(asked())))
    rig.clock.now = NOW + CONFIRM_TTL + timedelta(seconds=1)
    rig.press(confirmation, 0, 0)
    assert rig.rejected() == {"expired_ref": 1} and rig.answers.submitted == []


def test_not_the_items_person(make: Callable[..., Rig]) -> None:
    rig = make(answer_users={str(ALICE): "ceo", str(BOB): "cfo"})
    sent = rig.send(digest(asked()))
    rig.press(sent, 1, 0, user=BOB)
    assert rig.rejected() == {"not_approver": 1}
    assert rig.toasts() == ["This question is for ceo."]
    unassigned = rig.send(digest(asked("T-2", offer=offer_item("T-2", approver=None)),
                                 state="needs_you:-"))  # fmt: skip
    rig.press(unassigned, 1, 0)
    assert rig.rejected() == {"not_approver": 2}


def test_without_answer_users_anyone_allowed_may_answer(rig: Rig) -> None:
    rig.choose(rig.send(digest(asked())), user=BOB)
    assert rig.rejected() == {}


def test_a_changed_question_writes_nothing(make: Callable[..., Rig]) -> None:
    answers = Answers()
    rig = make(answers)
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent)
    answers.changed = "the task was blocked again"
    rig.press(confirmation, 0, 0)
    assert rig.rejected() == {"stale_offer": 1}
    assert rig.toasts()[-1] == "This question changed; nothing was sent."
    assert answers.submitted == []
    task, expect = answers.checked[-1]
    assert task == "T-1" and expect == offer_item().expect  # from the record, not the press


def test_answers_off_for_the_board(make: Callable[..., Rig]) -> None:
    rig = make(Answers(on=False))
    rig.press(rig.send(digest(asked())), 1, 0)
    assert rig.rejected() == {"not_enabled": 1}
    assert rig.toasts() == ["Answers are off for this board."]


def test_dry_run_records_the_confirm_and_keeps_the_question(make: Callable[..., Rig]) -> None:
    answers = Answers(dry=True)
    rig = make(answers)
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent)  # the option press writes nothing: confirmed as usual
    rig.press(confirmation, 0, 0)
    assert rig.rejected() == {"dry_run": 1}
    assert rig.toasts()[-1] == "Kelpie is in dry-run: nothing was sent."
    assert [p.label for p in answers.dry_runs] == ["B"] and answers.submitted == []
    assert rig.store.claim(offer_item().question) is None  # not used up
    rig.press(confirmation, 0, 0)  # the same confirm again: recorded once
    assert len(answers.dry_runs) == 1 and rig.rejected() == {"dry_run": 1, "used_ref": 1}


def test_dry_run_records_nothing_for_a_refused_person_or_a_changed_question(
    make: Callable[..., Rig],
) -> None:
    answers = Answers(dry=True)
    rig = make(answers, answer_users={str(ALICE): "ceo", str(BOB): "cfo"})
    sent = rig.send(digest(asked()))
    confirmation = rig.choose(sent)
    answers.changed = "the task was blocked again"
    rig.press(confirmation, 0, 0)
    rig.press(sent, 1, 0, user=BOB)
    assert rig.rejected() == {"stale_offer": 1, "not_approver": 1}
    assert answers.dry_runs == []


def test_two_confirms_racing_for_one_question_write_once(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    a = rig.choose(sent, col=0)
    b = rig.choose(sent, col=1, user=BOB)
    # Both pass the read-only check; the claim in the store decides (unique question key).
    question = offer_item().question
    assert rig.store.claim_question(question, "A", "x", ALICE, 999, NOW) is None
    rig.press(b, 0, 0, user=BOB)
    assert rig.rejected() == {"used_question": 1} and rig.answers.submitted == []
    assert rig.store.release_question(question, 999)
    rig.press(a, 0, 0)
    assert [p.label for p in rig.answers.submitted] == ["A"]


def test_a_confirm_claimed_meanwhile_is_used(rig: Rig) -> None:
    sent = rig.send(digest(asked()))
    message_id, body = rig.choose(sent)
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    ref_id = parse_answer(data, rig.store.key(), CHAT)[2]  # type: ignore[index]
    assert rig.store.use_answer_ref(ref_id, NOW)  # taken by a press just before
    assert not rig.store.use_answer_ref(ref_id, NOW)
    rig.raw(message_id, body, data)
    assert rig.rejected() == {"used_ref": 1} and rig.answers.submitted == []


def test_a_failed_answer_shows_no_buttons(rig: Rig) -> None:
    from kelpie.core.offers import Answered

    item = offer_item()
    failed = ItemOffer(item.offer, item.question, item.expect, "ceo",
                       Answered("B", "ceo", NOW, failed=True))  # fmt: skip
    _, body = rig.send(digest(asked(offer=failed)))
    assert "Answer B by ceo failed to send at 2026-10-19 10:00 UTC" in body["text"]
    rows = body["reply_markup"]["inline_keyboard"]
    assert not [r for r in rows if r[0]["text"].endswith(". A")]


def test_a_failed_hand_off_gives_the_question_back(make: Callable[..., Rig]) -> None:
    answers = Answers(fail=True)
    rig = make(answers)
    sent = rig.send(digest(asked()))
    rig.press(rig.choose(sent), 0, 0)
    assert rig.metrics.chat_actions[("telegram", "failed")] == 1
    assert rig.store.claim(offer_item().question) is None


def test_typed_text_is_never_read(rig: Rig) -> None:
    before = rig.handled()
    rig.fake.push({"message": {"message_id": 9, "text": "B", "chat": {"id": int(CHAT)}}})
    rig.fake.wait_for(lambda: rig.handled() > before)
    assert rig.rejected() == {"not_callback": 1} and rig.answers.submitted == []


def test_an_answer_press_without_answer_buttons_is_a_forgery(make: Callable[..., Rig]) -> None:
    rig = make(answer_buttons=False)
    sent = rig.send(digest(asked()))
    assert len(sent[1]["reply_markup"]["inline_keyboard"]) == 1  # snooze only
    rig.raw(sent[0], sent[1], answer_data(rig.store.key(), CHAT, "a", 0, 1))
    assert rig.rejected() == {"bad_ref": 1}
