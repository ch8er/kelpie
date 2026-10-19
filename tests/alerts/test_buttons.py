"""Snooze buttons on Telegram alerts and the poller that takes the presses (#176), against a
local fake of the Bot API: who may press, the signed references, expiry, replay, the
acknowledgement, back-off and shutdown."""

from __future__ import annotations

import os
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.alerts import Delivery
from kelpie.alerts.buttons import (
    REF_TTL,
    ButtonPoller,
    ButtonStore,
    ChatControl,
    Press,
    alert_key,
    button_store_path,
    callback_data,
    parse_callback,
)
from kelpie.alerts.config import load_alerts
from kelpie.alerts.render import AT_SIGN, MAX_TEXT, SnoozeTarget
from kelpie.alerts.telegram import TelegramChannel, load_telegram
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, AlertItem, Transition
from kelpie.core.snooze import CHAT_DAYS
from kelpie.metrics import Metrics
from tests.alerts.fake_telegram import FakeTelegram, fake_telegram

TOKEN = "123456:ABCdef_SECRET-token-0123456789abc"
ENV = "KELPIE_TEST_TELEGRAM_TOKEN"
CHAT = "-100123"
ALICE = 4242
MALLORY = 666
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
KEY = bytes(range(32))


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Sink:
    presses: list[Press] = field(default_factory=list)
    fail: bool = False

    def __call__(self, press: Press, now: datetime) -> datetime:
        if self.fail:
            raise RuntimeError("disk full")
        self.presses.append(press)
        return now + timedelta(days=press.days)


def digest(*tasks: str, state: str = "needs_you:ceo", **kw: Any) -> AlertEvent:
    items = tuple(
        AlertItem(t, f"Decide on {t}", timedelta(hours=5), "Pick one", 2, snooze_task=t)
        for t in tasks
    )
    return AlertEvent(
        "needs_you", "escalate", "Needs you (ceo).", Severity.WARNING,
        items=items, at=NOW, state=state, **kw,
    )  # fmt: skip


def wait(cond: Callable[[], object], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


@dataclass
class Rig:
    fake: FakeTelegram
    store: ButtonStore
    sink: Sink
    clock: Clock
    metrics: Metrics
    delivery: Delivery
    poller: ButtonPoller

    def send(self, event: AlertEvent) -> tuple[int, dict[str, Any]]:
        before = len(self.fake.sent())
        self.delivery.deliver(event)
        wait(lambda: len(self.fake.sent()) > before)
        return self.fake.sent()[-1]

    def press(
        self, sent: tuple[int, dict[str, Any]], row: int = 0, col: int = 0, **kw: Any
    ) -> None:
        message_id, body = sent
        data = body["reply_markup"]["inline_keyboard"][row][col]["callback_data"]
        self.raw_press(message_id, body, data, **kw)

    def raw_press(
        self,
        message_id: int,
        body: dict[str, Any],
        data: str,
        user: int = ALICE,
        chat: int | str = CHAT,
        **sender: Any,
    ) -> None:
        before = self.handled()
        self.fake.press(message_id, body, data, user, chat, **sender)
        wait(lambda: self.handled() > before)

    def handled(self) -> int:
        return sum(self.metrics.chat_actions.values())

    def answers(self) -> list[str]:
        return [b["text"] for b in self.fake.calls("answerCallbackQuery")]

    def rejected(self) -> dict[str, int]:
        return {r: n for (_, r), n in self.metrics.chat_rejections.items()}


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    with fake_telegram() as fake:
        built = _rig(fake.bot(), tmp_path)
        try:
            yield built
        finally:
            built.delivery.close(1.0)


def _rig(fake: FakeTelegram, tmp_path: Path, allowed: tuple[int, ...] = (ALICE,)) -> Rig:
    store = ButtonStore(tmp_path / "kelpie.buttons.db")
    sink, clock, metrics = Sink(), Clock(), Metrics()
    metrics.set_chat_channels(["telegram"])
    control = ChatControl(store, sink, clock, metrics, poll_timeout=0, backoff=0.01)
    settings = load_telegram(
        {
            "type": "telegram", "token_env": ENV, "chat_id": CHAT, "api_url": fake.url,
            "buttons": True, "allowed_user_ids": list(allowed),
        },
        "test",
    )  # fmt: skip
    channel, poller = settings.build_with_buttons(control, {ENV: TOKEN})
    delivery = Delivery([channel], metrics, pollers=[poller])
    return Rig(fake, store, sink, clock, metrics, delivery, poller)


# --- the reference -------------------------------------------------------------------------


def test_the_reference_is_short_signed_and_carries_no_task_text() -> None:
    data = callback_data(KEY, CHAT, 3, 0x1F)
    assert data.startswith("s3.1f.") and len(data.encode()) == 28
    longest = callback_data(KEY, "-1001234567890", 7, 2**60 - 1)
    assert len(longest.encode()) <= 64  # Telegram's limit
    assert parse_callback(data, KEY, CHAT) == (0x1F, 3)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda d: d[:-1],  # truncated
        lambda d: d[:-1] + ("A" if d[-1] != "A" else "B"),  # forged MAC
        lambda d: "s7" + d[2:],  # another duration under the same MAC
        lambda d: d.replace(".1f.", ".20."),  # another record
        lambda d: d.replace(".1f.", ".01f."),  # a leading zero: not the canonical form
        lambda d: "u" + d[1:],  # another action: there is none
        lambda d: d + " ",
        lambda d: None,
    ],
)
def test_a_forged_or_truncated_reference_is_refused(mangle: Callable[[str], Any]) -> None:
    assert parse_callback(mangle(callback_data(KEY, CHAT, 3, 0x1F)), KEY, CHAT) is None


def test_a_reference_is_bound_to_its_chat_and_key() -> None:
    data = callback_data(KEY, CHAT, 1, 5)
    assert parse_callback(data, KEY, "-100999") is None
    assert parse_callback(data, bytes(32), CHAT) is None
    with pytest.raises(ValueError):
        callback_data(KEY, CHAT, 30, 5)  # no 30-day button exists


def test_the_store_keeps_its_key_private_and_never_reuses_an_id(tmp_path: Path) -> None:
    path = button_store_path(tmp_path / "kelpie.db")
    assert path.name == "kelpie.buttons.db"
    store = ButtonStore(path)
    key = store.key()
    first = store.issue("telegram", CHAT, "a", [SnoozeTarget(1, "T-1")], NOW)
    store.issue("telegram", CHAT, "a", [SnoozeTarget(1, "T-2")], NOW + timedelta(days=30))
    store.close()  # the first was pruned
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert key.hex() not in repr(store)
    again = ButtonStore(path)
    assert again.key() == key  # kept across restarts
    assert again.get(first[0]) is None
    (newer,) = again.issue(
        "telegram", CHAT, "a", [SnoozeTarget(1, "T-3")], NOW + timedelta(days=31)
    )
    assert newer > first[0] + 1  # AUTOINCREMENT: an old reference never names a new record
    again.close()


def test_the_store_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    link = tmp_path / "kelpie.buttons.db"
    link.symlink_to(target)
    with pytest.raises(OSError):
        ButtonStore(link)


def test_the_store_tightens_a_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.buttons.db"
    path.write_bytes(b"")
    path.chmod(0o644)
    ButtonStore(path).close()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_an_alert_is_known_by_its_state_across_escalation_steps() -> None:
    assert alert_key(digest("T-1")) == "needs_you:ceo"
    assert alert_key(digest("T-1", state=None)) == "needs_you|escalate"  # type: ignore[arg-type]


# --- buttons on the alert ------------------------------------------------------------------


def test_snoozable_items_get_three_buttons_each(rig: Rig) -> None:
    _, body = rig.send(digest("T-1", "T-2"))
    rows = body["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [
        ["1. Snooze 1d", "3d", "7d"],
        ["2. Snooze 1d", "3d", "7d"],
    ]
    for row in rows:
        for days, button in zip(CHAT_DAYS, row, strict=True):
            data = button["callback_data"]
            assert "T-" not in data and len(data.encode()) <= 64
            assert parse_callback(data, rig.store.key(), CHAT) is not None
            assert data.startswith(f"s{days}.")


def test_no_buttons_without_a_snoozable_item_or_on_a_resolved_alert(rig: Rig) -> None:
    plain = AlertEvent("loop_breaker", "stop s-1", "Runaway.", Severity.CRITICAL, at=NOW,
                       items=(AlertItem("T-9"),))  # fmt: skip
    _, body = rig.send(plain)
    assert "reply_markup" not in body  # agent health: nothing to snooze
    _, body = rig.send(digest("T-1", transition=Transition.RESOLVED))
    assert "reply_markup" not in body


def test_buttons_are_off_by_default_and_without_the_poller(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        entry = {"type": "telegram", "token_env": ENV, "chat_id": CHAT, "api_url": fake.url}
        off = load_telegram(entry, "t")
        assert not off.buttons and off.allowed_user_ids == ()
        TelegramChannel(off, TOKEN).send(digest("T-1"))
        on = load_telegram({**entry, "buttons": True, "allowed_user_ids": [ALICE]}, "t")
        on.build({ENV: TOKEN}).send(digest("T-1"))  # kelpie report: no control, no buttons
        delivery = load_alerts({"channels": [{**entry, "buttons": True,
                                              "allowed_user_ids": [ALICE]}]}).build(
            environ={ENV: TOKEN}
        )  # fmt: skip
        assert delivery is not None and delivery.pollers == ()
        delivery.close(0.5)
        assert all("reply_markup" not in b for b in fake.calls("sendMessage"))
        assert fake.calls("getUpdates") == []


# --- presses -------------------------------------------------------------------------------


def test_a_press_snoozes_answers_and_edits_the_alert(rig: Rig) -> None:
    sent = rig.send(digest("T-1", "T-2"))
    message_id, body = sent
    rig.press(sent, row=1, col=1)  # "3d" on item 2
    assert rig.sink.presses == [Press("telegram", "T-2", 3, ALICE, "Alice")]
    assert rig.answers() == ["Snoozed T-2 until 2026-10-22 10:00 UTC"]
    (edit,) = rig.fake.calls("editMessageText")
    assert edit["chat_id"] == int(CHAT) and edit["message_id"] == message_id
    assert edit["text"].endswith("\n✅ 2. Snoozed T-2 until 2026-10-22 10:00 UTC by Alice")
    assert "parse_mode" not in edit  # literal text: the person's name is never parsed
    rows = edit["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [["1. Snooze 1d", "3d", "7d"]]
    assert rows == body["reply_markup"]["inline_keyboard"][:1]
    assert rig.metrics.chat_actions[("telegram", "snoozed")] == 1
    rendered = rig.metrics.render()
    assert 'kelpie_chat_actions_total{channel="telegram",outcome="snoozed"} 1' in rendered
    assert TOKEN not in rendered


def test_the_edit_keeps_the_messages_own_formatting(rig: Rig) -> None:
    message_id, body = rig.send(digest("T-1"))
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    entities = [
        {"type": "bold", "offset": 2, "length": 5},
        {"type": "text_link", "offset": 10, "length": 4, "url": "https://board.example.org/t/1"},
        {"type": "text_link", "offset": 20, "length": 4, "url": "javascript:alert(1)"},
        {"type": "custom_emoji", "offset": 0, "length": 1, "custom_emoji_id": "1"},
    ]
    rig.raw_press(message_id, {**body, "text": "x" * 40, "entities": entities}, data)
    (edit,) = rig.fake.calls("editMessageText")
    # the same entities back (the line is added at the end), only the kinds alerts use and
    # only safe links
    assert edit["entities"] == entities[:2]
    assert edit["text"].startswith("x" * 40 + "\n✅ 1. Snoozed T-1 until ")
    assert edit["link_preview_options"] == {"is_disabled": True}
    assert "reply_markup" not in edit  # the only item's buttons are gone


def test_a_message_at_the_limit_only_loses_the_buttons(rig: Rig) -> None:
    message_id, body = rig.send(digest("T-1"))
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    rig.raw_press(message_id, {**body, "text": "x" * (MAX_TEXT - 10)}, data)
    assert rig.fake.calls("editMessageText") == []
    (edit,) = rig.fake.calls("editMessageReplyMarkup")
    assert edit["reply_markup"] == {"inline_keyboard": []}


def test_a_hostile_name_is_cleaned_and_never_mentions(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent, first_name="<b>@everyone</b>‮", last_name="​x")
    (edit,) = rig.fake.calls("editMessageText")
    line = edit["text"].rsplit("\n", 1)[1]
    assert f"by <b>{AT_SIGN}everyone</b> x" in line
    assert "@" not in line and "‮" not in line and "​" not in line
    assert rig.sink.presses[0].name == f"<b>{AT_SIGN}everyone</b> x"


def test_a_stranger_is_refused_without_saying_why(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent, user=MALLORY)
    assert rig.sink.presses == [] and rig.answers() == ["Not allowed."]
    assert rig.rejected() == {"unknown_user": 1}
    assert rig.fake.calls("editMessageText") == []


def test_a_press_from_another_chat_is_refused(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent, chat=-100999)
    assert rig.sink.presses == [] and rig.answers() == ["Not allowed."]
    assert rig.rejected() == {"wrong_chat": 1}


@pytest.mark.parametrize(
    "mangle",
    [lambda d: d[:-3], lambda d: d[:-1] + ("x" if d[-1] != "x" else "y"), lambda d: "rm -rf /"],
)
def test_a_forged_or_truncated_press_is_refused(rig: Rig, mangle: Callable[[str], str]) -> None:
    message_id, body = rig.send(digest("T-1"))
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    rig.raw_press(message_id, body, mangle(data))
    assert rig.sink.presses == [] and rig.answers() == ["Not allowed."]
    assert rig.rejected() == {"bad_ref": 1}


def test_a_reference_from_another_message_is_refused(rig: Rig) -> None:
    message_id, body = rig.send(digest("T-1"))
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    rig.raw_press(message_id + 7, body, data)  # pasted under another message
    assert rig.rejected() == {"bad_ref": 1}


def test_an_old_reference_expires(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.clock.now = NOW + REF_TTL + timedelta(seconds=1)
    rig.press(sent)
    assert rig.sink.presses == [] and rig.answers() == ["Not allowed."]
    assert rig.rejected() == {"expired_ref": 1}


def test_the_next_escalation_step_expires_the_older_buttons(rig: Rig) -> None:
    first = rig.send(digest("T-1"))
    rig.clock.now = NOW + timedelta(hours=4)
    second = rig.send(digest("T-1", transition=Transition.ESCALATED, age=timedelta(hours=4)))
    other = rig.send(digest("T-1", state="needs_you:cto"))  # another person's: not newer
    rig.press(first)
    assert rig.rejected() == {"expired_ref": 1}
    rig.press(second)
    rig.press(other)
    assert [p.task_id for p in rig.sink.presses] == ["T-1", "T-1"]


def test_a_reference_is_used_once(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent)
    rig.press(sent, col=2)  # the same item again (the edit had not landed yet)
    assert len(rig.sink.presses) == 1 and rig.rejected() == {"used_ref": 1}


def test_presses_are_rate_limited_per_user(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    for _ in range(10):
        rig.press(sent, user=MALLORY)
    rig.press(sent, user=MALLORY)
    assert rig.rejected() == {"unknown_user": 10, "rate_limited": 1}
    assert len(rig.answers()) == 10  # a rate-limited press gets no answer at all
    rig.press(sent)  # another person is not held back
    assert len(rig.sink.presses) == 1


def test_a_failed_write_is_said_and_counted(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.sink.fail = True
    rig.press(sent)
    assert rig.answers() == ["Could not snooze; see the Kelpie log."]
    assert rig.metrics.chat_actions[("telegram", "failed")] == 1
    rig.sink.fail = False
    rig.press(sent)  # not used up by the failure
    assert len(rig.sink.presses) == 1


def test_other_updates_are_ignored_and_counted(rig: Rig) -> None:
    rig.fake.push({"message": {"message_id": 1, "chat": {"id": int(CHAT)}, "text": "/start"}})
    wait(lambda: rig.rejected().get("not_callback"))
    rig.fake.push({"callback_query": {"id": "q", "from": {"id": "4242"}, "data": "x"}})
    wait(lambda: rig.rejected().get("malformed"))
    assert rig.sink.presses == [] and rig.answers() == ["Not allowed."]
    (first, *_) = rig.fake.calls("getUpdates")
    assert first["allowed_updates"] == ["callback_query"]


def test_a_restart_does_not_replay_a_press(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        rig = _rig(fake, tmp_path)
        sent = rig.send(digest("T-1"))
        rig.press(sent)
        (update,) = fake.updates
        taken = update["update_id"]
        rig.delivery.close(1.0)
        store = ButtonStore(tmp_path / "kelpie.buttons.db")
        assert store.offset("telegram") == taken + 1
        store.close()
        replays = [[update], [update]]  # a server that sends the press again anyway

        def replay(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            time.sleep(0.01)
            return 200, {"ok": True, "result": replays.pop() if replays else []}

        fake.routes["getUpdates"] = replay
        again = _rig(fake, tmp_path)
        try:
            wait(lambda: not replays)
            time.sleep(0.1)
            assert again.sink.presses == [] and again.handled() == 0
            assert all(b["offset"] == taken + 1 for b in fake.calls("getUpdates")[-2:])
        finally:
            again.delivery.close(1.0)
        assert len(rig.sink.presses) == 1


# --- the poller's own health ---------------------------------------------------------------


def test_the_poller_backs_off_on_errors(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        fake.routes["getUpdates"] = lambda body: (502, {"ok": False, "description": "bad gw"})
        rig = _rig(fake, tmp_path)
        try:
            wait(lambda: len(fake.calls("getUpdates")) >= 5, timeout=10)
        finally:
            rig.delivery.close(1.0)
        calls = rig.metrics.chat_api_errors[("telegram", "getUpdates", "server_error")]
        assert calls >= 5
        # 10, 20, 40, 80 ms...: far fewer calls than a busy loop would make
        assert len(fake.calls("getUpdates")) < 15


def test_close_holds_its_deadline_while_a_long_poll_is_open(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        release = threading.Event()

        def hang(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            release.wait(5)
            return 200, {"ok": True, "result": [{"update_id": 9, "callback_query": {}}]}

        fake.routes["getUpdates"] = hang
        rig = _rig(fake, tmp_path)
        wait(lambda: fake.calls("getUpdates"))
        started = time.monotonic()
        rig.delivery.close(0.3)
        assert time.monotonic() - started < 1.5
        assert rig.poller.abandoned
        release.set()
        time.sleep(0.2)
        assert rig.handled() == 0  # a late answer is ignored


def test_the_api_never_logs_the_token(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.fake.routes["answerCallbackQuery"] = lambda body: (
        400,
        {"ok": False, "description": f"bad request for bot{TOKEN}"},
    )
    sent = rig.send(digest("T-1"))
    rig.press(sent, user=MALLORY)
    wait(lambda: rig.metrics.chat_api_errors)
    assert TOKEN not in caplog.text and "<redacted>" in caplog.text
