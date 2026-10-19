"""Snooze buttons (#176) wired into Kelpie: the snooze and ledger entry a press writes, the
config, startup refusals, ``kelpie status`` and ``kelpie doctor``."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.alerts.config import load_alerts
from kelpie.alerts.telegram import load_telegram
from kelpie.app import ConfigError, build_app, load_rules, load_settings, render_status
from kelpie.core import storage
from kelpie.core.actions import ActionKind, Severity
from kelpie.core.alerts import AlertEvent, AlertItem
from kelpie.core.config import Mode
from kelpie.core.ledger import Outcome
from kelpie.core.model import Snooze
from kelpie.core.snooze import CHAT_MAX_DAYS, SnoozeSettings, chat_snooze
from kelpie.core.storage import SqliteLedgerStore
from kelpie.doctor import check_chat_buttons
from tests.alerts.fake_telegram import fake_telegram

ENV = "KELPIE_TEST_TG_TOKEN"
TOKEN = "987654:ZZtop_SECRET-value-0123456789abcdef"
CHAT = "-100777"
ALICE = 4242
NOW = datetime(2026, 10, 19, 10, tzinfo=UTC)


# --- the snooze a press sets ---------------------------------------------------------------


def test_a_press_snoozes_from_now_for_its_days_at_most_seven() -> None:
    settings = SnoozeSettings()
    for days in (1, 3, 7):
        record, _ = chat_snooze("T-1", days, "Alice", ALICE, "telegram", NOW, settings)
        assert record.until - NOW == timedelta(days=days) <= timedelta(days=CHAT_MAX_DAYS)
    for bad in (0, 2, 8, 30, -1):
        with pytest.raises(ValueError):
            chat_snooze("T-1", bad, "Alice", ALICE, "telegram", NOW, settings)
    short = SnoozeSettings(max_days=2)  # [snooze] max_days caps a button too
    assert chat_snooze("T-1", 7, "A", 1, "t", NOW, short)[0].until == NOW + timedelta(days=2)
    with pytest.raises(ValueError):
        chat_snooze(" ", 1, "Alice", ALICE, "telegram", NOW, settings)


def test_the_press_is_a_chat_snooze_with_a_ledger_entry() -> None:
    record, entry = chat_snooze("T-1", 3, "Alice", ALICE, "telegram", NOW, SnoozeSettings())
    assert (record.source, record.by, record.created_at) == ("chat", "Alice", NOW)
    assert record.behind_at_snooze is None  # kelpie serve records the baseline
    assert entry.kind is ActionKind.SNOOZE and entry.outcome is Outcome.EXECUTED
    assert (entry.rule, entry.mode, entry.executor) == ("chat", Mode.ACT, "telegram")
    assert entry.subject == "chat:telegram"  # counts against no task's rate limit
    assert entry.task_id == "T-1" and entry.at == NOW
    assert entry.params == {
        "until": (NOW + timedelta(days=3)).isoformat(),
        "days": 3,
        "by": "Alice",
        "user_id": ALICE,
        "channel": "telegram",
        "source": "chat",
    }
    assert "Alice (user id 4242) snoozed T-1 until 2026-10-22" in entry.reason
    assert "dry-run" not in entry.reason
    nameless = chat_snooze("T-1", 1, "  ", ALICE, "telegram", NOW, SnoozeSettings())[0]
    assert nameless.by == "user 4242"
    dry = chat_snooze("T-1", 1, "A", ALICE, "telegram", NOW, SnoozeSettings(), dry_run=True)[1]
    assert dry.params["dry_run"] is True and "Stored under the global dry-run" in dry.reason


def test_a_chat_snooze_and_its_ledger_entry_are_written_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    try:
        record, entry = chat_snooze("T-1", 3, "Alice", ALICE, "telegram", NOW, SnoozeSettings())
        assert store.put_chat_snooze(record, entry) is None
        assert [s.task_id for s in store.snoozes()] == ["T-1"]
        assert [e.task_id for e in store.since(NOW - timedelta(days=1))] == ["T-1"]

        def broken(db: object, entry: object) -> None:
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(storage, "_insert_entry", broken)
        again, entry2 = chat_snooze("T-2", 1, "Alice", ALICE, "telegram", NOW, SnoozeSettings())
        with pytest.raises(sqlite3.OperationalError):
            store.put_chat_snooze(again, entry2)
        assert [s.task_id for s in store.snoozes()] == ["T-1"]  # no snooze without its entry
    finally:
        store.close()


def test_a_chat_snooze_never_cuts_a_longer_snooze_from_another_source(tmp_path: Path) -> None:
    store = SqliteLedgerStore(tmp_path / "kelpie.db")

    def operator(until: datetime, **kw: object) -> Snooze:
        return Snooze("T-1", until, "after the release", "ops", NOW - timedelta(hours=1), **kw)  # type: ignore[arg-type]

    def press(days: int = 3) -> Snooze | None:
        record, entry = chat_snooze("T-1", days, "Alice", ALICE, "telegram", NOW, SnoozeSettings())
        return store.put_chat_snooze(record, entry)

    try:
        longer = operator(NOW + timedelta(days=30))
        store.put_snooze(longer)
        held = press()
        assert held is not None and held.until == longer.until and held.source == "cli"
        assert store.snoozes()[0].source == "cli" and store.since(NOW - timedelta(days=1)) == []
        for replaceable in (
            operator(NOW + timedelta(days=1)),  # ends sooner than the press would
            operator(NOW - timedelta(minutes=1)),  # expired
            operator(NOW + timedelta(days=30), grown_at=NOW, grown_by=2),  # holds nothing
        ):
            store.put_snooze(replaceable)
            assert press() is None
            assert store.snoozes()[0].source == "chat"
        assert press(1) is None  # a chat snooze replaces a chat snooze, longer or not
        assert store.snoozes()[0].until == NOW + timedelta(days=1)
    finally:
        store.close()


# --- config --------------------------------------------------------------------------------


def entry(**extra: object) -> dict[str, object]:
    return {"type": "telegram", "token_env": ENV, "chat_id": CHAT, **extra}


def test_buttons_config_is_checked() -> None:
    on = load_telegram(entry(buttons=True, allowed_user_ids=[ALICE, ALICE, 7]), "t")
    assert on.buttons and on.allowed_user_ids == (ALICE, 7)
    assert "snooze buttons for 2 user(s)" in on.describe()
    for bad in ({"buttons": "yes"}, {"allowed_user_ids": "4242"}, {"allowed_user_ids": [True]},
                {"allowed_user_ids": [0]}, {"allowed_user_ids": ["4242"]},
                {"allowed_user_ids": list(range(1, 102))}):  # fmt: skip
        with pytest.raises(ValueError, match=r"buttons|allowed_user_ids"):
            load_telegram(entry(**bad), "t")
    empty = load_telegram(entry(buttons=True), "t")
    with pytest.raises(ValueError, match="needs allowed_user_ids"):
        empty.check({ENV: TOKEN})
    load_telegram(entry(), "t").check({ENV: TOKEN})  # off: nothing to check
    two = [entry(buttons=True, allowed_user_ids=[1]), entry(name="b", buttons=True,
                                                            allowed_user_ids=[1])]  # fmt: skip
    with pytest.raises(ValueError, match="more than one Telegram channel"):
        load_alerts({"channels": two})


# --- wired into kelpie serve ---------------------------------------------------------------


def write(tmp_path: Path, url: str, storage: str | None = None, ids: str = f"[{ALICE}]") -> Path:
    db = storage or str(tmp_path / "kelpie.db")
    path = tmp_path / "kelpie.toml"
    path.write_text(f"""
[storage]
path = "{db}"

[traces]
enabled = false

[[alerts.channels]]
type = "telegram"
token_env = "{ENV}"
chat_id = "{CHAT}"
api_url = "{url}"
min_severity = "info"
buttons = true
allowed_user_ids = {ids}
""")
    return path


def wait(cond: object, timeout: float = 30.0) -> None:  # the timeout only guards a hang
    end = time.monotonic() + timeout
    while not cond():  # type: ignore[operator]
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_a_press_writes_the_snooze_and_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    with fake_telegram() as fake:
        fake.bot()
        settings = load_settings(write(tmp_path, fake.url))
        status = render_status(settings, load_rules(settings.engine))
        assert "snooze buttons for 1 user(s)" in status and TOKEN not in status
        app = build_app(settings, clock=lambda: NOW)
        try:
            assert app.alerts is not None and len(app.alerts.pollers) == 1
            assert (tmp_path / "kelpie.buttons.db").exists()
            item = AlertItem("H-1", "Sign", timedelta(hours=5), snooze_task="H-1")
            app.engine.alerter.send(  # type: ignore[union-attr]
                AlertEvent(
                    "needs_you",
                    "escalate",
                    "Needs you.",
                    Severity.WARNING,
                    items=(item,),
                    at=NOW,
                    state="needs_you:ceo",
                )
            )
            wait(lambda: app.metrics.alerts_sent["telegram"])
            message_id, body = fake.sent()[0]
            data = body["reply_markup"]["inline_keyboard"][0][2]["callback_data"]  # 7d
            # An operator's longer snooze is kept: the press is refused and says until when.
            operator = SqliteLedgerStore(tmp_path / "kelpie.db")
            operator.put_snooze(Snooze("H-1", NOW + timedelta(days=30), None, "ops", NOW))
            fake.press(message_id, body, data, ALICE, CHAT, last_name="Smith")
            wait(lambda: fake.calls("answerCallbackQuery"))
            assert fake.calls("answerCallbackQuery")[0]["text"] == (
                "H-1 is already snoozed until 2026-11-18 10:00 UTC"
            )
            assert operator.delete_snooze("H-1")
            operator.close()
            fake.press(message_id, body, data, ALICE, CHAT, last_name="Smith")  # given back
            wait(lambda: app.metrics.chat_actions[("telegram", "snoozed")])  # fully handled
            rendered = app.metrics.render()
        finally:
            app.close()
    assert 'kelpie_chat_actions_total{channel="telegram",outcome="snoozed"} 1' in rendered
    assert 'kelpie_chat_rejected_total{channel="telegram",reason="already_snoozed"} 1' in rendered
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    try:
        (snooze,) = store.snoozes()
        assert (snooze.task_id, snooze.source, snooze.by) == ("H-1", "chat", "Alice Smith")
        assert snooze.until == NOW + timedelta(days=7)
        (press,) = [e for e in store.since(NOW - timedelta(days=1)) if e.kind is ActionKind.SNOOZE]
        assert press.params["user_id"] == ALICE and press.params["by"] == "Alice Smith"
        assert press.task_id == "H-1" and press.executor == "telegram"
        assert press.params["dry_run"] is True  # stored under the global dry-run, and says so
    finally:
        store.close()


def test_buttons_without_a_database_or_without_people_do_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV, TOKEN)
    memory = load_settings(write(tmp_path, "http://127.0.0.1:9", storage=":memory:"))
    with pytest.raises(ConfigError, match="need a database"):
        build_app(memory)
    nobody = load_settings(write(tmp_path, "http://127.0.0.1:9", ids="[]"))
    with pytest.raises(ConfigError, match="needs allowed_user_ids"):
        build_app(nobody)
    status = render_status(nobody, load_rules(nobody.engine))
    assert "NOT ready: [alerts] channel telegram: buttons = true needs allowed_user_ids" in status


# --- kelpie doctor -------------------------------------------------------------------------


def test_doctor_checks_the_allow_list_the_database_and_the_webhook(tmp_path: Path) -> None:
    env = {ENV: TOKEN}
    with fake_telegram() as fake:
        fake.bot()
        settings = load_settings(write(tmp_path, fake.url))
        (ok,) = check_chat_buttons(settings, env)
        assert ok.status == "PASS" and "no webhook; 1 user(s) may snooze" in ok.detail
        fake.webhook = "https://hooks.example.org/secret-path"
        (hooked,) = check_chat_buttons(settings, env)
        assert hooked.status == "FAIL" and "webhook" in hooked.detail
        assert "secret-path" not in hooked.detail
        nobody = load_settings(write(tmp_path, fake.url, ids="[]"))
        (empty,) = check_chat_buttons(nobody, env)
        assert empty.status == "FAIL" and "allowed_user_ids is empty" in empty.detail
        assert "@userinfobot" in (empty.fix or "")
        memory = load_settings(write(tmp_path, fake.url, storage=":memory:"))
        assert check_chat_buttons(memory, env)[0].status == "FAIL"
        (no_token,) = check_chat_buttons(settings, {})
        assert no_token.status == "FAIL" and ENV in no_token.detail
        assert TOKEN not in str([ok, hooked, empty, no_token])
    (down,) = check_chat_buttons(settings, env)  # the fake is gone
    assert down.status == "FAIL" and "did not answer" in down.detail
    off = tmp_path / "off.toml"
    off.write_text('[storage]\npath = ":memory:"\n')
    assert check_chat_buttons(load_settings(off), env) == []
