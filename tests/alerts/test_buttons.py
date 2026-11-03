"""Snooze buttons on Telegram alerts and the poller that takes the presses (#176), against a
local fake of the Bot API: who may press, the signed references, expiry, replay, the
acknowledgement, back-off and shutdown.

Waits are on events (a request answered, an update queued, a metric counted), never on
wall time: a timeout only guards against a hang. The poller's pauses go through its
``sleep``, which a test replaces to see them without waiting."""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.alerts import Delivery, buttons, render
from kelpie.alerts.buttons import (
    REF_TTL,
    AlreadySnoozed,
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
from kelpie.alerts.render import AT_SIGN, MAX_TEXT, SnoozeTarget, utf16_len
from kelpie.alerts.telegram import TelegramChannel, load_telegram
from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, AlertItem, Transition
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot
from kelpie.core.snooze import CHAT_DAYS
from kelpie.metrics import Metrics
from tests.alerts.fake_telegram import FakeTelegram, fake_telegram
from tests.fakes import FakeAdapter

TOKEN = "123456:ABCdef_SECRET-token-0123456789abc"
ENV = "KELPIE_TEST_TELEGRAM_TOKEN"
CHAT = "-100123"
ALICE = 4242
MALLORY = 666
NOW = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
KEY = bytes(range(32))
WAIT = 30.0  # only guards against a hang


@dataclass
class Clock:
    now: datetime = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Sink:
    presses: list[Press] = field(default_factory=list)
    fail: bool = False
    held_until: datetime | None = None  # a longer snooze from another source

    def __call__(self, press: Press, now: datetime) -> datetime:
        if self.fail:
            raise RuntimeError("disk full")
        if self.held_until is not None:
            raise AlreadySnoozed(self.held_until)
        self.presses.append(press)
        return now + timedelta(days=press.days)


class SignalingMetrics(Metrics):
    """Metrics that wake the fake's waiters whenever a press or a send is counted."""

    def __init__(self, fake: FakeTelegram) -> None:
        super().__init__()
        self.fake = fake

    def chat_action(self, channel: str, outcome: str) -> None:
        super().chat_action(channel, outcome)
        self.fake.notify()

    def chat_rejected(self, channel: str, reason: str) -> None:
        super().chat_rejected(channel, reason)
        self.fake.notify()

    def chat_api_error(self, channel: str, call: str, cause: str) -> None:
        super().chat_api_error(channel, call, cause)
        self.fake.notify()

    def alert_failed(self, channel: str, cause: str) -> None:
        super().alert_failed(channel, cause)
        self.fake.notify()

    def alert_sent(self, channel: str) -> None:
        super().alert_sent(channel)
        self.fake.notify()


def digest(
    *tasks: str, state: str = "needs_you:ceo", snoozable: bool = True, **kw: Any
) -> AlertEvent:
    items = tuple(
        AlertItem(
            t, f"Decide on {t}", timedelta(hours=5), "Pick one", 2,
            snooze_task=t if snoozable else None,
        )
        for t in tasks
    )  # fmt: skip
    return AlertEvent(
        "needs_you", "escalate", "Needs you (ceo).", Severity.WARNING,
        items=items, at=NOW, state=state, **kw,
    )  # fmt: skip


@dataclass
class Rig:
    fake: FakeTelegram
    store: ButtonStore
    sink: Sink
    clock: Clock
    metrics: Metrics
    delivery: Delivery
    poller: ButtonPoller

    def wait(self, cond: Callable[[], object]) -> None:
        self.fake.wait_for(cond)

    def send(self, event: AlertEvent) -> tuple[int, dict[str, Any]]:
        """Deliver ``event`` and wait until the channel is done with it (sent and recorded)."""
        before = sum(self.metrics.alerts_sent.values())
        self.delivery.deliver(event)
        self.wait(lambda: sum(self.metrics.alerts_sent.values()) > before)
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
        self.wait(lambda: self.handled() > before)

    def push(self, update: dict[str, Any]) -> None:
        """Queue a raw update and wait until the poller has counted it."""
        before = self.handled()
        self.fake.push(update)
        self.wait(lambda: self.handled() > before)

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


def _rig(
    fake: FakeTelegram,
    tmp_path: Path,
    allowed: tuple[int, ...] = (ALICE,),
    prepare: Callable[[ButtonPoller], None] | None = None,
    min_interval: float = 0.0,
    min_severity: str | None = None,  # None: the channel's default (warning)
) -> Rig:
    store = ButtonStore(tmp_path / "kelpie.buttons.db")
    sink, clock, metrics = Sink(), Clock(), SignalingMetrics(fake)
    metrics.set_chat_channels(["telegram"])
    control = ChatControl(
        store, sink, clock, metrics, poll_timeout=0, backoff=0.01, min_interval=min_interval
    )
    entry: dict[str, Any] = {
        "type": "telegram", "token_env": ENV, "chat_id": CHAT, "api_url": fake.url,
        "buttons": True, "allowed_user_ids": list(allowed),
    }  # fmt: skip
    if min_severity is not None:
        entry["min_severity"] = min_severity
    settings = load_telegram(entry, "test")
    channel, poller = settings.build_with_buttons(control, {ENV: TOKEN})
    if prepare is not None:
        prepare(poller)  # before the delivery starts it
    delivery = Delivery([channel], metrics, pollers=[poller])
    return Rig(fake, store, sink, clock, metrics, delivery, poller)


class Pauses:
    """A poller's ``sleep`` that records each pause and returns at once; after ``until``
    pauses it sets ``done`` and then really waits (until the poller is told to stop)."""

    def __init__(self, until: int) -> None:
        self.until = until
        self.seen: list[float] = []
        self.done = threading.Event()
        self.poller: ButtonPoller | None = None

    def install(self, poller: ButtonPoller) -> None:
        self.poller = poller
        poller.sleep = self

    def __call__(self, seconds: float) -> object:
        assert self.poller is not None
        if self.done.is_set():
            return self.poller._stopping.wait(seconds)
        self.seen.append(seconds)
        if len(self.seen) >= self.until:
            self.done.set()
        return False


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
        lambda d: d + "\n",
        lambda d: None,
    ],
)
def test_a_forged_or_truncated_reference_is_refused(mangle: Callable[[str], Any]) -> None:
    assert parse_callback(mangle(callback_data(KEY, CHAT, 3, 0x1F)), KEY, CHAT) is None


@pytest.mark.parametrize(
    "data",
    [
        None, 1, b"s1.1.AAAAAAAAAAAAAAAAAAAAAA", "s1.1.AAAAAAAAAAAAAAAAAAAAAA", "x" * 10000,
        f"s{chr(0x661)}.1.AAAAAAAAAAAAAAAAAAAAAA", "s1.1.AAAAAAAAAAAAAAAAAAAAAA.", "s1..AAAA",
        "s1.0.AAAAAAAAAAAAAAAAAAAAAA", "s1.fffffffffffffff.AAAAAAAAAAAAAAAAAAAAAA",
        "s9.1.AAAAAAAAAAAAAAAAAAAAAA", "\ud800", f"s1.{chr(0xFF11)}.AAAAAAAAAAAAAAAAAAAAAA",
    ],
)  # fmt: skip
def test_parsing_any_callback_data_never_raises(data: Any) -> None:
    assert parse_callback(data, KEY, CHAT) is None


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


def test_the_store_refuses_a_file_swapped_before_sqlite_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kelpie.buttons.db"
    elsewhere = tmp_path / "elsewhere.db"
    elsewhere.write_bytes(b"")
    connect = sqlite3.connect

    def swap_then_connect(database: str, **kw: Any) -> sqlite3.Connection:
        path.unlink()  # checked, then replaced by a symlink before SQLite opens the path
        path.symlink_to(elsewhere)
        return connect(database, **kw)

    monkeypatch.setattr(buttons.sqlite3, "connect", swap_then_connect)
    with pytest.raises(ValueError, match="changed while it was opened"):
        ButtonStore(path)


def test_the_store_refuses_a_file_swapped_back_after_sqlite_opened_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path is checked again, and so is the file SQLite really has open: a swap that is
    undone once SQLite opened the other file is caught by the second check."""
    path = tmp_path / "kelpie.buttons.db"
    aside, elsewhere = tmp_path / "aside.db", tmp_path / "elsewhere.db"
    elsewhere.write_bytes(b"")
    connect = sqlite3.connect

    def swap_open_swap_back(database: str, **kw: Any) -> sqlite3.Connection:
        path.rename(aside)
        path.symlink_to(elsewhere)
        db = connect(database, **kw)
        db.execute("PRAGMA schema_version").fetchone()  # SQLite opens elsewhere.db
        path.unlink()
        aside.rename(path)  # the checked file is back at the path
        return db

    monkeypatch.setattr(buttons.sqlite3, "connect", swap_open_swap_back)
    with pytest.raises(ValueError, match="changed while it was opened"):
        ButtonStore(path)


def test_the_store_refuses_a_file_of_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "kelpie.buttons.db"
    path.write_bytes(b"")
    owner = os.stat(path).st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(ValueError, match="belongs to another user"):
        ButtonStore(path)


def test_the_store_warns_about_a_directory_others_can_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    ButtonStore(shared / "kelpie.buttons.db").close()
    assert "its directory is writable by others" in caplog.text
    caplog.clear()
    ButtonStore(tmp_path / "kelpie.buttons.db").close()
    assert "writable by others" not in caplog.text


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
            # no task text: the MAC is random base64url and may hold any letters, the rest
            # is the action, the days and the record id
            assert re.fullmatch(r"s[137]\.[1-9a-f][0-9a-f]*\.[A-Za-z0-9_-]{22}", data)
            assert len(data.encode()) <= 64
            assert parse_callback(data, rig.store.key(), CHAT) is not None
            assert data.startswith(f"s{days}.")


@pytest.mark.parametrize(
    ("language", "label"),
    [("en", "1. Snooze H-1 1d"), ("ru", "1. Отложить H-1 на 1 д")],
)
def test_a_button_names_the_task_when_it_snoozes_another_than_the_item(
    language: str, label: str
) -> None:
    """Rule 2's overdue wait W-1 is snoozed through the human task H-1 it waits on."""
    event = AlertEvent(
        "wait_condition", "alert W-1", "W-1 has waited 30h.", Severity.WARNING, at=NOW,
        items=(AlertItem("W-1", "Deploy", snooze_task="H-1"),),
    )  # fmt: skip
    _, (target,) = render.telegram_message(
        event, replace(render.DEFAULT_RENDERING, language=language)
    )
    assert target == SnoozeTarget(1, "H-1", named=True)
    (row,) = render.keyboard([(target, [(1, "a"), (3, "b")])], language)["inline_keyboard"]
    assert row[0]["text"] == label and row[1]["text"] in {"3d", "3 д"}
    (plain,) = render.keyboard([(SnoozeTarget(1, "W-1"), [(1, "a")])], language)["inline_keyboard"]
    assert "W-1" not in plain[0]["text"]  # the item's own task: the number says which


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


def _name_entity(edit: dict[str, Any]) -> str:
    """The text the acknowledgement's ``code`` entity covers (offsets in UTF-16 units)."""
    (code,) = [e for e in edit["entities"] if e["type"] == "code"]
    units = edit["text"].encode("utf-16-le")
    start, end = 2 * code["offset"], 2 * (code["offset"] + code["length"])
    return units[start:end].decode("utf-16-le")


def test_a_press_snoozes_answers_and_edits_the_alert(rig: Rig) -> None:
    sent = rig.send(digest("T-1", "T-2"))
    message_id, body = sent
    rig.press(sent, row=1, col=1)  # "3d" on item 2
    assert rig.sink.presses == [Press("telegram", "T-2", 3, ALICE, "Alice")]
    assert rig.answers() == ["Snoozed T-2 until 2026-10-22 10:00 UTC"]
    (edit,) = rig.fake.calls("editMessageText")
    # the configured chat, never an id read from the update
    assert edit["chat_id"] == CHAT and edit["message_id"] == message_id
    assert edit["text"].endswith("\n✅ 2. Snoozed T-2 until 2026-10-22 10:00 UTC by Alice")
    assert "parse_mode" not in edit  # literal text: the person's name is never parsed
    assert _name_entity(edit) == "Alice"
    rows = edit["reply_markup"]["inline_keyboard"]
    assert [[b["text"] for b in row] for row in rows] == [["1. Snooze 1d", "3d", "7d"]]
    assert rows == body["reply_markup"]["inline_keyboard"][:1]
    assert rig.metrics.chat_actions[("telegram", "snoozed")] == 1
    rendered = rig.metrics.render()
    assert 'kelpie_chat_actions_total{channel="telegram",outcome="snoozed"} 1' in rendered
    assert TOKEN not in rendered


def _answer_sends_with(rig: Rig, text: str, entities: list[dict[str, Any]]) -> None:
    """Make the fake answer ``sendMessage`` with ``text`` and ``entities``, as Telegram
    answers with the message it parsed."""
    send = rig.fake.routes["sendMessage"]

    def answer(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        status, reply = send(body)
        return status, {**reply, "result": {**reply["result"], "text": text, "entities": entities}}

    rig.fake.routes["sendMessage"] = answer


def test_the_edit_is_built_from_kelpies_copy_with_its_formatting(rig: Rig) -> None:
    entities = [
        {"type": "bold", "offset": 2, "length": 5},
        {"type": "text_link", "offset": 10, "length": 4, "url": "https://board.example.org/t/1"},
        {"type": "text_link", "offset": 20, "length": 4, "url": "javascript:alert(1)"},
        {"type": "custom_emoji", "offset": 0, "length": 1, "custom_emoji_id": "1"},
    ]
    _answer_sends_with(rig, "x" * 40, entities)
    message_id, body = rig.send(digest("T-1"))
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    # the copy a press carries is not used: Kelpie edits what Telegram answered it with
    echoed = {**body, "text": "forged", "entities": [{"type": "bold", "offset": 0, "length": 6}]}
    rig.raw_press(message_id, echoed, data)
    (edit,) = rig.fake.calls("editMessageText")
    # the same entities back (the line is added at the end), only the kinds alerts use and
    # only safe links, then the person's name as code
    assert edit["entities"][:-1] == entities[:2]
    assert edit["entities"][-1]["type"] == "code" and _name_entity(edit) == "Alice"
    assert edit["text"].startswith("x" * 40 + "\n✅ 1. Snoozed T-1 until ")
    assert edit["link_preview_options"] == {"is_disabled": True}
    assert "reply_markup" not in edit  # the only item's buttons are gone


def test_two_quick_presses_on_one_message_keep_both_acknowledgements(rig: Rig) -> None:
    message_id, body = rig.send(digest("T-1", "T-2"))
    rows = body["reply_markup"]["inline_keyboard"]
    before = rig.handled()
    with rig.fake.batch():  # one getUpdates answer: both carry the message before any edit
        rig.fake.press(message_id, body, rows[0][0]["callback_data"], ALICE, CHAT)
        rig.fake.press(message_id, body, rows[1][2]["callback_data"], ALICE, CHAT)
    rig.wait(lambda: rig.handled() == before + 2)
    assert [p.task_id for p in rig.sink.presses] == ["T-1", "T-2"]
    first, last = rig.fake.calls("editMessageText")
    assert "reply_markup" in first and "reply_markup" not in last  # no used button is back
    lines = last["text"].splitlines()[-2:]
    assert lines[0].startswith("✅ 1. Snoozed T-1 until 2026-10-20") and lines[0].endswith("Alice")
    assert lines[1].startswith("✅ 2. Snoozed T-2 until 2026-10-26") and lines[1].endswith("Alice")
    assert [e["type"] for e in last["entities"]][-2:] == ["code", "code"]


@pytest.mark.parametrize("text", ["x" * (MAX_TEXT - 10), ""], ids=["at-the-limit", "no-text"])
def test_an_acknowledgement_that_does_not_fit_is_a_reply(rig: Rig, text: str) -> None:
    _answer_sends_with(rig, text, [])
    sent = rig.send(digest("T-1"))
    rig.press(sent)
    assert rig.fake.calls("editMessageText") == []
    (edit,) = rig.fake.calls("editMessageReplyMarkup")
    assert edit == {"chat_id": CHAT, "message_id": sent[0], "reply_markup": {"inline_keyboard": []}}
    reply = rig.fake.sent()[-1][1]
    assert reply["text"].startswith("✅ 1. Snoozed T-1 until ") and reply["text"].endswith("Alice")
    assert reply["reply_parameters"]["message_id"] == sent[0] and reply["chat_id"] == CHAT
    assert _name_entity(reply) == "Alice"


def test_a_hostile_name_is_cleaned_and_never_mentions(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent, first_name="<b>@everyone</b>‮", last_name="​x")
    (edit,) = rig.fake.calls("editMessageText")
    line = edit["text"].rsplit("\n", 1)[1]
    assert f"by <b>{AT_SIGN}everyone</b> x" in line
    assert "@" not in line and "‮" not in line and "​" not in line
    assert rig.sink.presses[0].name == f"<b>{AT_SIGN}everyone</b> x"


def test_a_url_like_name_is_code_so_telegram_links_nothing(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent, first_name="evil.example.org/login", last_name="😀")
    (edit,) = rig.fake.calls("editMessageText")
    assert edit["text"].endswith(" by evil.example.org/login 😀")
    name = _name_entity(edit)  # UTF-16 offsets: the emoji counts twice
    assert name == "evil.example.org/login 😀"
    assert utf16_len(name) == len(name) + 1


def test_a_stranger_is_refused_without_an_answer(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent, user=MALLORY)
    assert rig.sink.presses == [] and rig.answers() == []  # counted, never answered
    assert rig.rejected() == {"unknown_user": 1}
    assert rig.fake.calls("editMessageText") == []


def test_a_press_from_another_chat_is_refused_without_an_answer(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent, chat=-100999)
    assert rig.sink.presses == [] and rig.answers() == []
    assert rig.rejected() == {"wrong_chat": 1}


def test_a_press_without_its_message_or_chat_is_refused(rig: Rig) -> None:
    message_id, body = rig.send(digest("T-1"))
    data = body["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    sender = {"id": ALICE, "first_name": "Alice"}
    inline = {"id": "q1", "from": sender, "inline_message_id": "abc", "data": data}
    rig.push({"callback_query": inline})  # an inline message: no chat to check
    as_text = {"message_id": message_id, "chat": {"id": CHAT}}  # the id as a string
    rig.push({"callback_query": {"id": "q2", "from": sender, "message": as_text, "data": data}})
    ours = {"message_id": message_id, "chat": {"id": int(CHAT)}}
    rig.push({"callback_query": {"id": "q3", "from": {"id": True}, "message": ours,
                                 "data": data}})  # fmt: skip
    rig.push({"callback_query": {"id": "q4", "from": sender, "message": ours, "data": data + "\n"}})
    assert rig.rejected() == {"wrong_chat": 2, "malformed": 1, "bad_ref": 1}
    assert rig.sink.presses == [] and rig.answers() == ["Not allowed."]  # only q4's


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


def test_an_old_reference_expires_says_so_and_loses_its_buttons(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.clock.now = NOW + REF_TTL + timedelta(seconds=1)
    rig.press(sent)
    assert rig.sink.presses == [] and rig.rejected() == {"expired_ref": 1}
    assert rig.answers() == [
        "This button has expired. To snooze T-1: kelpie snooze T-1 --until 2026-10-23"
    ]
    (strip,) = rig.fake.calls("editMessageReplyMarkup")  # none of its buttons works now
    assert strip == {
        "chat_id": CHAT,
        "message_id": sent[0],
        "reply_markup": {"inline_keyboard": []},
    }


def test_an_expired_button_of_a_task_id_unfit_for_a_shell_names_no_command(rig: Rig) -> None:
    sent = rig.send(digest("-rf"))
    rig.clock.now = NOW + REF_TTL + timedelta(seconds=1)
    rig.press(sent)
    assert rig.answers() == ["This button has expired."]


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
    # the older message lost its buttons when the newer one went out
    strip, *_ = rig.fake.calls("editMessageReplyMarkup")
    assert strip == {
        "chat_id": CHAT,
        "message_id": first[0],
        "reply_markup": {"inline_keyboard": []},
    }


def test_a_digest_with_nothing_to_snooze_expires_the_older_buttons(rig: Rig) -> None:
    first = rig.send(digest("T-1"))
    rig.clock.now = NOW + timedelta(hours=1)
    _, body = rig.send(digest("T-2", snoozable=False))
    assert "reply_markup" not in body
    strip, *_ = rig.fake.calls("editMessageReplyMarkup")  # taken off at once
    assert strip["message_id"] == first[0] and strip["reply_markup"] == {"inline_keyboard": []}
    rig.press(first)
    assert rig.sink.presses == [] and rig.rejected() == {"expired_ref": 1}


class Stuck:
    """A rule that reports a block (rule 4's way: one task, ``snooze_task`` in params)
    while ``on``."""

    name = "stale_blocks"

    def __init__(self) -> None:
        self.on = True

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        if not self.on:
            return []
        return [
            Action(
                ActionKind.ALERT, self.name, "T-1 has been blocked for 10h.", task_id="T-1",
                key="unlinked:T-1", severity=Severity.WARNING, state="stale_blocks:T-1:x",
                since=NOW, params={"snooze_task": "T-1"},
            )
        ]  # fmt: skip


@pytest.mark.parametrize("shown", [False, True], ids=["default-severity", "info-shown"])
def test_a_resolved_alert_expires_its_buttons_whether_or_not_the_chat_shows_it(
    tmp_path: Path, shown: bool
) -> None:
    """The real engine resolves the state: its notice is ``info``, below a Telegram
    channel's default ``min_severity``, and still the buttons stop and go."""
    with fake_telegram() as fake:
        fake.bot()
        rig = _rig(fake, tmp_path, min_severity="info" if shown else None)
        rule = Stuck()
        engine = Engine([rule], adapters=[FakeAdapter()], alerter=rig.delivery.alerter(),
                        config=EngineConfig(dry_run=False, default_mode=Mode.ACT))  # type: ignore[list-item]  # fmt: skip
        try:
            engine.tick(lambda: Snapshot(now=NOW))
            rig.wait(lambda: rig.metrics.alerts_sent["telegram"] == 1)
            first = rig.fake.sent()[-1]
            assert first[1]["reply_markup"]["inline_keyboard"]  # rule 4's alert has buttons
            rule.on = False
            rig.clock.now = NOW + timedelta(hours=1)
            engine.tick(lambda: Snapshot(now=NOW + timedelta(hours=1)))  # resolved
            done = 2 if shown else 1  # alerts sent once the resolve is handled
            rig.wait(
                lambda: (
                    rig.fake.calls("editMessageReplyMarkup")
                    and rig.metrics.alerts_sent["telegram"] == done
                )
            )
            (strip,) = rig.fake.calls("editMessageReplyMarkup")
            assert strip["message_id"] == first[0]
            assert strip["reply_markup"] == {"inline_keyboard": []}
            assert len(rig.fake.sent()) == (2 if shown else 1)  # the notice only where shown
            rig.press(first)
            assert rig.sink.presses == [] and rig.rejected() == {"expired_ref": 1}
        finally:
            rig.delivery.close(1.0)


def resolve_notice() -> AlertEvent:
    """A resolve as the engine sends it: ``info``, no items, the state it closes."""
    return replace(digest("T-1"), transition=Transition.RESOLVED, items=(), severity=Severity.INFO)


def test_a_resolve_whose_notice_fails_still_expires_the_buttons(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        rig = _rig(fake, tmp_path, min_severity="info")
        try:
            first = rig.send(digest("T-1"))
            rig.clock.now = NOW + timedelta(hours=1)
            send = fake.routes["sendMessage"]
            fake.routes["sendMessage"] = lambda body: (
                400,
                {"ok": False, "description": "Bad Request: chat not found"},
            )
            rig.delivery.deliver(resolve_notice())
            rig.wait(lambda: rig.metrics.alerts_failed)  # the notice never arrived...
            fake.routes["sendMessage"] = send
            rig.press(first)
            assert rig.sink.presses == [] and rig.rejected() == {"expired_ref": 1}  # ...still
        finally:
            rig.delivery.close(1.0)


def test_a_resolve_that_cannot_be_queued_still_expires_the_buttons(rig: Rig) -> None:
    first = rig.send(digest("T-1"))
    rig.clock.now = NOW + timedelta(hours=1)
    (worker,) = rig.delivery._workers
    worker.offer = lambda message: "queue_full"  # type: ignore[method-assign]
    rig.delivery.deliver(resolve_notice())  # done in the caller, in the store only
    del worker.offer
    rig.press(first)
    assert rig.sink.presses == [] and rig.rejected() == {"expired_ref": 1}


def test_a_channel_without_buttons_queues_nothing_for_a_resolve(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        entry = {"type": "telegram", "token_env": ENV, "chat_id": CHAT, "api_url": fake.url}
        channel = load_telegram(entry, "t").build({ENV: TOKEN})
        assert not channel.expiring
        delivery = Delivery([channel])
        (worker,) = delivery._workers
        worker.offer = lambda message: pytest.fail("queued")  # type: ignore[method-assign]
        delivery.deliver(resolve_notice())  # info, below warning: nothing at all
        del worker.offer
        delivery.close(1.0)


def test_an_acknowledgement_on_a_message_superseded_meanwhile_leaves_no_buttons(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = rig.send(digest("T-1", "T-2"))
    answers = iter([True, False])  # latest when checked, superseded when acknowledged
    monkeypatch.setattr(rig.store, "is_latest", lambda ref: next(answers))
    rig.press(sent)
    (edit,) = rig.fake.calls("editMessageText")
    assert "reply_markup" not in edit  # T-2's buttons are not put back on an old message
    assert edit["text"].splitlines()[-1].startswith("✅ 1. Snoozed T-1")


def test_an_edit_that_fails_falls_back_to_a_reply(rig: Rig) -> None:
    rig.fake.routes["editMessageText"] = lambda body: (
        400,
        {"ok": False, "description": "Bad Request: message to edit not found"},
    )
    sent = rig.send(digest("T-1", "T-2"))
    rig.press(sent)
    (strip,) = rig.fake.calls("editMessageReplyMarkup")
    assert [[b["text"] for b in row] for row in strip["reply_markup"]["inline_keyboard"]] == [
        ["2. Snooze 1d", "3d", "7d"]
    ]
    reply = rig.fake.sent()[-1][1]
    assert reply["text"].startswith("✅ 1. Snoozed T-1") and "reply_parameters" in reply
    rig.press(sent, row=1)  # Kelpie's copy was not changed by the failed edit
    assert len(rig.sink.presses) == 2


@pytest.mark.parametrize("release", ["raises", "not-claimed"])
def test_a_reference_that_cannot_be_given_back_is_logged(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, release: str
) -> None:
    sent = rig.send(digest("T-1"))
    rig.sink.fail = True

    def give_back(ref_id: int, at: datetime) -> bool:
        if release == "raises":
            raise sqlite3.OperationalError("database is locked")
        return False

    monkeypatch.setattr(rig.store, "release", give_back)
    rig.press(sent)
    assert rig.answers() == ["Could not snooze; see the Kelpie log."]
    expected = "not released" if release == "raises" else "was not claimed any more"
    assert expected in caplog.text


def test_a_store_failure_in_the_poll_loop_backs_off_and_goes_on(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    offset = rig.store.offset
    failures = iter([True])

    def flaky(channel: str) -> int:
        if next(failures, False):
            raise sqlite3.OperationalError("disk I/O error")
        return offset(channel)

    monkeypatch.setattr(rig.store, "offset", flaky)
    rig.wait(lambda: rig.metrics.chat_api_errors[("telegram", "poller", "error")])
    sent = rig.send(digest("T-1"))
    rig.press(sent)  # still taking presses
    assert len(rig.sink.presses) == 1


def _old_rows(path: Path) -> ButtonStore:
    """A store with a reference, a latest message and a message copy from 8 days ago."""
    store = ButtonStore(path)
    long_ago = NOW - timedelta(days=8)
    (ref,) = store.issue("telegram", CHAT, "a", [SnoozeTarget(1, "T-1")], long_ago)
    shown = buttons.Shown("text", [], {"inline_keyboard": []})
    store.delivered("telegram", CHAT, "a", [ref], 7, long_ago, shown)
    return store


def _rows(store: ButtonStore) -> dict[str, int]:
    return {
        table: store._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("refs", "latest", "messages")
    }


def test_the_store_prunes_what_is_older_than_a_week(tmp_path: Path) -> None:
    store = _old_rows(tmp_path / "kelpie.buttons.db")
    try:
        assert _rows(store) == {"refs": 1, "latest": 1, "messages": 1}
        assert store.prune(NOW - timedelta(days=2)) == 0  # 6 days old: kept
        assert store.prune(NOW) == 3
        assert _rows(store) == {"refs": 0, "latest": 0, "messages": 0}
    finally:
        store.close()


def test_the_poller_prunes_at_start_and_then_every_hour(tmp_path: Path) -> None:
    _old_rows(tmp_path / "kelpie.buttons.db").close()
    pruned = threading.Event()
    calls: list[datetime] = []
    ticks = iter(range(0, 10**9, int(buttons.PRUNE_EVERY)))  # an hour per loop

    def prepare(poller: ButtonPoller) -> None:
        prune = poller.store.prune

        def counted(at: datetime) -> int:
            calls.append(at)
            if len(calls) >= 2:
                pruned.set()
            return prune(at)

        poller.store.prune = counted  # type: ignore[method-assign]
        poller.monotonic = lambda: float(next(ticks))

    with fake_telegram() as fake:
        fake.bot()
        rig = _rig(fake, tmp_path, prepare=prepare)
        try:
            assert pruned.wait(WAIT)
            assert _rows(rig.store) == {"refs": 0, "latest": 0, "messages": 0}
        finally:
            rig.delivery.close(1.0)


def test_removing_older_buttons_is_best_effort(rig: Rig) -> None:
    rig.fake.routes["editMessageReplyMarkup"] = lambda body: (
        400,
        {"ok": False, "description": "Bad Request: message is not modified"},
    )
    first = rig.send(digest("T-1"))
    rig.send(digest("T-2", snoozable=False))  # delivered all the same
    rig.press(first)
    assert rig.rejected() == {"expired_ref": 1}
    assert rig.metrics.alerts_sent["telegram"] == 2


def test_a_message_without_an_id_expires_the_older_buttons(rig: Rig) -> None:
    first = rig.send(digest("T-1"))
    rig.fake.routes["sendMessage"] = lambda body: (200, {"ok": True, "result": {}})
    rig.delivery.deliver(digest("T-1", transition=Transition.ESCALATED))
    rig.wait(lambda: rig.metrics.alerts_sent["telegram"] == 2)
    rig.press(first)
    assert rig.rejected() == {"expired_ref": 1}


def test_a_reference_is_used_once(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.press(sent)
    rig.press(sent, col=2)  # the same item again (the edit had not landed yet)
    assert len(rig.sink.presses) == 1 and rig.rejected() == {"used_ref": 1}


def test_a_reference_is_claimed_before_the_snooze_is_written(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = rig.send(digest("T-1"))

    def locked(ref_id: int, at: datetime) -> bool:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rig.store, "use", locked)
    rig.press(sent)
    assert rig.sink.presses == []  # no claim, no snooze
    assert rig.answers() == ["Could not snooze; see the Kelpie log."]
    assert rig.metrics.chat_actions[("telegram", "failed")] == 1


def test_a_failed_acknowledgement_leaves_the_snooze_counted(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = rig.send(digest("T-1"))

    def broken(*args: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(rig.store, "shown", broken)
    rig.press(sent)
    assert len(rig.sink.presses) == 1
    assert rig.metrics.chat_actions[("telegram", "snoozed")] == 1  # written: that is what counts


def test_a_longer_snooze_from_another_source_is_kept(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    rig.sink.held_until = NOW + timedelta(days=30)  # an operator's kelpie snooze
    rig.press(sent)
    assert rig.sink.presses == [] and rig.rejected() == {"already_snoozed": 1}
    assert rig.answers() == ["T-1 is already snoozed until 2026-11-18 10:00 UTC"]
    assert rig.fake.calls("editMessageText") == []
    rig.sink.held_until = None
    rig.press(sent)  # the reference was given back
    assert len(rig.sink.presses) == 1


def test_presses_are_rate_limited_per_user(rig: Rig) -> None:
    sent = rig.send(digest("T-1"))
    for _ in range(10):
        rig.press(sent, user=MALLORY)
    rig.press(sent, user=MALLORY)
    assert rig.rejected() == {"unknown_user": 10, "rate_limited": 1}
    assert rig.answers() == []
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
    rig.push({"message": {"message_id": 1, "chat": {"id": int(CHAT)}, "text": "/start"}})
    rig.push({"callback_query": {"id": "q", "from": {"id": "4242"}, "data": "x"}})
    assert rig.rejected() == {"not_callback": 1, "malformed": 1}
    assert rig.sink.presses == [] and rig.answers() == []
    (first, *_) = rig.fake.calls("getUpdates")
    assert first["allowed_updates"] == ["callback_query"]


def test_the_offset_moves_past_updates_nobody_can_take(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        answers = [[{"update_id": 0, "callback_query": {}}, "junk", 42]]

        def odd(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            return 200, {"ok": True, "result": answers.pop() if answers else []}

        fake.routes["getUpdates"] = odd
        rig = _rig(fake, tmp_path)
        try:
            rig.wait(lambda: len(fake.calls("getUpdates")) >= 2)
        finally:
            rig.delivery.close(1.0)
        assert rig.rejected() == {"malformed": 3}
        assert fake.calls("getUpdates")[1]["offset"] == 1  # update 0 is not asked for again


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
            return 200, {"ok": True, "result": replays.pop() if replays else []}

        fake.routes["getUpdates"] = replay
        before = len(fake.calls("getUpdates"))
        again = _rig(fake, tmp_path)
        try:
            # two polls answered with the replay, then one more: both were handled by then
            again.wait(lambda: len(fake.calls("getUpdates")) >= before + 3)
            assert again.sink.presses == [] and again.handled() == 0
            assert all(b["offset"] == taken + 1 for b in fake.calls("getUpdates")[before:])
        finally:
            again.delivery.close(1.0)
        assert len(rig.sink.presses) == 1


# --- the poller's own health ---------------------------------------------------------------


def test_the_poller_backs_off_on_errors(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        fake.routes["getUpdates"] = lambda body: (502, {"ok": False, "description": "bad gw"})
        pauses = Pauses(until=6)
        rig = _rig(fake, tmp_path, prepare=pauses.install)
        try:
            assert pauses.done.wait(30)
        finally:
            rig.delivery.close(1.0)
        assert pauses.seen == [0.01, 0.02, 0.04, 0.08, 0.16, 0.32]  # doubles, from backoff
        assert rig.metrics.chat_api_errors[("telegram", "getUpdates", "server_error")] >= 6


def test_a_poll_that_returns_early_is_followed_by_a_pause(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        fake.routes["getUpdates"] = lambda body: (200, {"ok": True, "result": []})
        pauses = Pauses(until=3)

        def prepare(poller: ButtonPoller) -> None:
            pauses.install(poller)
            poller.monotonic = lambda: 100.0  # every poll took no time at all

        rig = _rig(fake, tmp_path, prepare=prepare, min_interval=1.0)
        try:
            assert pauses.done.wait(30)
        finally:
            rig.delivery.close(1.0)
        assert pauses.seen == [1.0, 1.0, 1.0]


def test_close_holds_its_deadline_while_a_long_poll_is_open(tmp_path: Path) -> None:
    with fake_telegram() as fake:
        fake.bot()
        release = threading.Event()

        def hang(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            release.wait(5)
            return 200, {"ok": True, "result": [{"update_id": 9, "callback_query": {}}]}

        fake.routes["getUpdates"] = hang
        rig = _rig(fake, tmp_path)
        fake.wait_for(lambda: fake.requests)
        started = time.monotonic()
        rig.delivery.close(0.3)
        assert time.monotonic() - started < 1.5
        assert rig.poller.abandoned
        release.set()
        thread = rig.poller._thread
        assert thread is not None
        thread.join(30)  # the late answer has been read by now
        assert not thread.is_alive() and len(fake.calls("getUpdates")) == 1
        assert rig.handled() == 0  # ...and ignored


def test_the_api_never_logs_the_token(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    rig.fake.routes["answerCallbackQuery"] = lambda body: (
        400,
        {"ok": False, "description": f"bad request for bot{TOKEN}"},
    )
    message_id, body = rig.send(digest("T-1"))
    rig.raw_press(message_id, body, "s1.1.AAAAAAAAAAAAAAAAAAAAAA")  # refused, answered
    rig.wait(lambda: rig.metrics.chat_api_errors)
    assert TOKEN not in caplog.text and "<redacted>" in caplog.text
