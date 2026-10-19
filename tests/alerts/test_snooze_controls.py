"""Snoozing from the alert (#176) as each channel renders it: Telegram buttons on the items a
snooze would quiet, and Slack's ready-to-copy ``kelpie snooze`` line under each of them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from kelpie.alerts import render
from kelpie.alerts.buttons import callback_data
from kelpie.alerts.render import (
    MAX_ITEMS,
    MAX_TEXT,
    RenderSettings,
    SnoozeTarget,
    keyboard,
    snooze_command,
    telegram_message,
    utf16_len,
)
from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, AlertItem, Transition
from kelpie.core.engine import alert_items
from kelpie.core.model import Snapshot
from kelpie.core.snooze import CHAT_DAYS
from tests.alerts.snapshot import assert_snapshot

H = timedelta(hours=1)
AT = datetime(2026, 10, 19, 9, 30, tzinfo=UTC)
URL = "https://board.example.org/tasks/{id}"
EN = RenderSettings(task_url=URL)
KEY = bytes(range(32))  # a fixed key, so the golden's references are stable
CHAT = "-1001234567890"

DIGEST = AlertEvent(
    rule="needs_you",
    subject="escalate",
    reason="Needs you (ceo): 3 item(s) hold 4 agent task(s), 31.0 agent-hours waited so far.",
    severity=Severity.WARNING,
    items=(
        AlertItem(
            "t_r2a1b2c3",
            "Enable R2 for backups",
            24 * H,
            "waiting_human: needs_input: CEO: enable R2 in the Cloudflare dashboard",
            2,
            snooze_task="t_r2a1b2c3",
        ),
        AlertItem("H-1", "Sign the vendor contract", 6 * H, None, 2, snooze_task="H-1"),
        # a block that can never clear: news, not a reminder, so nothing to snooze
        AlertItem("t_dead", "Publish the app", 1 * H, "its PR was closed unmerged", 0),
    ),
    at=AT,
    state="needs_you:ceo",
)


def telegram_with_buttons(event: AlertEvent, settings: RenderSettings) -> dict[str, object]:
    """What the channel sends: the message and its keyboard, references 1, 2, ..."""
    fields, targets = telegram_message(event, settings)
    rows = [
        (t, [(d, callback_data(KEY, CHAT, d, n)) for d in CHAT_DAYS])
        for n, t in enumerate(targets, 1)
    ]
    return {**fields, "reply_markup": keyboard(rows, settings.language)}


@pytest.mark.parametrize(("case", "settings"), [("en", EN), ("ru", RenderSettings("ru", URL))])
def test_the_controls_match_their_snapshots(case: str, settings: RenderSettings) -> None:
    name = f"digest_snooze_controls{'' if case == 'en' else '_ru'}"
    assert_snapshot(f"{name}.txt", render.plain(DIGEST, settings))
    payload = telegram_with_buttons(DIGEST, settings)
    assert_snapshot(f"{name}.tg.json", json.dumps(payload, indent=2, ensure_ascii=False))
    slack = render.slack_payload(DIGEST, settings)
    assert_snapshot(f"{name}.json", json.dumps(slack, indent=2, ensure_ascii=False))


def test_slack_shows_the_command_under_each_snoozable_item() -> None:
    slack = render.slack_payload(DIGEST, EN)
    text = str(slack["text"])
    assert "\n   kelpie snooze t_r2a1b2c3 --until 2026-10-22\n" in text
    assert "\n   kelpie snooze H-1 --until 2026-10-22\n" in text
    assert text.count("kelpie snooze") == 2  # not under the dead end
    blocks = slack["blocks"]
    assert isinstance(blocks, list)
    item = blocks[1]["elements"][0]["elements"]
    # its own plain text element, on its own line: it copies as it is
    assert item[-2:] == [
        {"type": "text", "text": "\n   "},
        {"type": "text", "text": "kelpie snooze t_r2a1b2c3 --until 2026-10-22"},
    ]


def test_telegram_and_plain_text_carry_no_command() -> None:
    assert "kelpie snooze" not in render.plain(DIGEST, EN)
    assert "kelpie snooze" not in render.telegram_html(DIGEST, EN)


def test_the_buttons_name_their_item_and_carry_only_a_reference() -> None:
    payload = telegram_with_buttons(DIGEST, EN)
    rows = payload["reply_markup"]["inline_keyboard"]  # type: ignore[index]
    assert [[b["text"] for b in row] for row in rows] == [
        ["1. Snooze 1d", "3d", "7d"],
        ["2. Snooze 1d", "3d", "7d"],
    ]
    assert all(len(b["callback_data"].encode()) <= 64 for row in rows for b in row)
    assert "t_r2" not in json.dumps(rows)


def test_the_command_date_is_the_alerts_day_in_the_report_time_zone() -> None:
    late = datetime(2026, 10, 19, 22, 30, tzinfo=UTC)  # already the 20th in Moscow
    moscow = RenderSettings(tz=ZoneInfo("Europe/Moscow"))
    assert snooze_command("T-1", late, moscow) == "kelpie snooze T-1 --until 2026-10-23"
    assert snooze_command("T-1", late, EN) == "kelpie snooze T-1 --until 2026-10-22"


@pytest.mark.parametrize(
    "task_id",
    [
        "T-1; rm -rf ~",
        "$(curl evil)",
        "T 1",
        "a`b`",
        "x\ny",
        "@here",
        "é",
        "a" * 101,
        "it's",
        "--help",
        "-rf",
    ],
)
def test_an_id_that_is_not_a_plain_token_gets_no_command(task_id: str) -> None:
    assert snooze_command(task_id, AT, EN) is None


def test_an_id_with_shell_characters_it_allows_is_quoted() -> None:
    assert snooze_command("host-a:T#1", AT, EN) == "kelpie snooze 'host-a:T#1' --until 2026-10-22"


def test_nothing_to_snooze_on_a_resolved_alert_or_without_the_tick() -> None:
    resolved = AlertEvent(**{**_fields(DIGEST), "transition": Transition.RESOLVED})
    assert telegram_message(resolved, EN)[1] == ()
    assert "kelpie snooze" not in str(render.slack_payload(resolved, EN))
    no_tick = AlertEvent(**{**_fields(DIGEST), "at": None})
    assert "kelpie snooze" not in str(render.slack_payload(no_tick, EN))
    assert len(telegram_message(no_tick, EN)[1]) == 2  # buttons need no date


def test_only_items_that_fit_the_message_get_buttons() -> None:
    many = tuple(
        AlertItem(f"T-{n}", "&" * 300, needed="<" * 400, snooze_task=f"T-{n}")  # escaped: long
        for n in range(MAX_ITEMS + 5)
    )
    event = AlertEvent("needs_you", "escalate", "r" * 3000, Severity.WARNING, items=many, at=AT)
    fields, targets = telegram_message(event, EN)
    assert utf16_len(str(fields["text"])) <= MAX_TEXT
    shown = [line for line in str(fields["text"]).splitlines() if line[:1].isdigit()]
    assert len(targets) == len(shown) < MAX_ITEMS
    assert targets[0] == SnoozeTarget(1, "T-0")


def test_the_engine_reads_the_target_from_params() -> None:
    board = Snapshot(AT)
    digest = Action(
        ActionKind.ESCALATE, "needs_you", "Needs you.",
        params={"items": [
            {"task_id": "H-1", "snooze_task": "H-1"},
            {"task_id": "H-2", "snooze_task": 3},
            {"task_id": "H-3", "snooze_task": " "},
            {"task_id": "H-4", "snooze_task": "x" * 201},
            {"task_id": "H-5"},
            # an agent's condition could name these: they would reach logs and buttons
            {"task_id": "H-6", "snooze_task": "H-6\nINFO forged log line"},
            {"task_id": "H-7", "snooze_task": "H-7‮"},
        ]},
    )  # fmt: skip
    assert [i.snooze_task for i in alert_items(digest, board)] == ["H-1"] + [None] * 6
    one = Action(
        ActionKind.ALERT, "wait_condition", "Overdue.", task_id="W-1",
        params={"snooze_task": "H-9"},
    )  # fmt: skip
    (item,) = alert_items(one, board)
    assert item.task_id == "W-1" and item.snooze_task == "H-9"


def _fields(event: AlertEvent) -> dict[str, object]:
    return {f: getattr(event, f) for f in AlertEvent.__dataclass_fields__}
