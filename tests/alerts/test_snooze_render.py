"""Snoozes in an alert (#176): the digest's footer of snoozed tasks and the mark on a task
that is back, from rule 13's params through the engine to every channel's rendering."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kelpie.alerts import render
from kelpie.alerts.render import (
    AT_SIGN,
    MAX_SNOOZED,
    MAX_TEXT,
    RenderSettings,
    snooze_when,
    utf16_len,
)
from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, AlertItem, SnoozeNote, SnoozeState
from kelpie.core.engine import alert_items, snoozed_items
from kelpie.core.model import Snapshot
from tests.alerts.snapshot import assert_snapshot

H = timedelta(hours=1)
NOW = datetime(2026, 10, 27, 10, tzinfo=UTC)
URL = "https://board.example.org/tasks/{id}"
EN = RenderSettings(task_url=URL)
RU = RenderSettings("ru", URL)
UNTIL = datetime(2026, 10, 26, tzinfo=UTC)  # --until 2026-10-25: through that day

DIGEST = AlertEvent(
    rule="needs_you",
    subject="escalate",
    reason=(
        "Needs you (ceo): 2 item(s) hold 3 agent task(s), 30.0 agent-hours waited so far. "
        "Snoozed, not counted: 2 item(s)."
    ),
    severity=Severity.WARNING,
    items=(
        AlertItem(
            "t_r2a1b2c3",
            "Enable R2 for backups",
            24 * H,
            "waiting_human: needs_input: CEO: enable R2 in the Cloudflare dashboard",
            0,
            snooze=SnoozeNote(SnoozeState.EXPIRED, UNTIL, "deferred to the release", "ceo"),
        ),
        AlertItem(
            "H-1",
            "Sign the vendor contract",
            6 * H,
            None,
            3,
            snooze=SnoozeNote(
                SnoozeState.GROWN, UNTIL + 7 * 24 * H, "after the release", "ceo", grown=2
            ),
        ),
    ),
    snoozed=(
        AlertItem(
            "t_qa4d5e6f",
            "Manual QA pass before release",
            20 * H,
            None,
            1,
            snooze=SnoozeNote(SnoozeState.HELD, UNTIL + 7 * 24 * H, "after the freeze", "ceo"),
        ),
        AlertItem(
            "t_dx0e1f2a",
            "Approve the data export",
            30 * H,
            None,
            0,
            snooze=SnoozeNote(SnoozeState.HELD, datetime(2026, 11, 3, 9, tzinfo=UTC), marker=True),
        ),
    ),
)


@pytest.mark.parametrize(
    ("case", "settings"), [("digest_snoozed", EN), ("digest_snoozed_ru", RU)], ids=["en", "ru"]
)
def test_the_digest_footer_and_the_marks_match_their_snapshots(
    case: str, settings: RenderSettings
) -> None:
    assert_snapshot(f"{case}.txt", render.plain(DIGEST, settings))
    assert_snapshot(f"{case}.html", render.telegram_html(DIGEST, settings))
    payload = render.slack_payload(DIGEST, settings)
    assert_snapshot(f"{case}.json", json.dumps(payload, indent=2, ensure_ascii=False))


def test_the_footer_reads_as_the_issue_asks() -> None:
    text = render.plain(DIGEST, EN)
    assert (
        "Snoozed: Manual QA pass before release through 2026-11-01 (after the freeze, by ceo)"
        " · t_qa4d5e6f"
    ) in text
    assert "Snoozed: Approve the data export until 2026-11-03 09:00 UTC (from marker)" in text
    assert "   Snooze ran through 2026-10-25\n" in text
    assert "   2 more tasks blocked since the snooze (through 2026-11-01)" in text
    # the footer stands under the tasks, before the reason
    assert text.index("Snoozed: Manual") < text.index("Why: ") < text.index("Rule: ")


def test_a_snoozes_end_is_shown_as_the_person_set_it() -> None:
    assert snooze_when(UNTIL) == "2026-10-25"  # midnight: the last day it covered
    assert snooze_when(datetime(2026, 10, 25, 9, 30, tzinfo=UTC)) == "2026-10-25 09:30 UTC"
    moscow = ZoneInfo("Europe/Moscow")
    assert snooze_when(datetime(2026, 10, 25, 21, tzinfo=UTC), moscow) == "2026-10-25"
    assert snooze_when(datetime(2026, 10, 25, 9, tzinfo=UTC), moscow) == "2026-10-25 12:00 MSK"
    shown = render.plain(DIGEST, RenderSettings(tz=moscow))
    assert "Snooze ended 2026-10-26 03:00 MSK" in shown  # not midnight there


def test_a_hostile_snooze_reason_is_cleaned_and_escaped() -> None:
    note = SnoozeNote(SnoozeState.HELD, UNTIL, "<b>@everyone</b> ‮pay", "<i>@here</i>")
    event = AlertEvent(
        "needs_you", "escalate", "Needs you.", Severity.WARNING,
        items=(AlertItem("t_1", "x"),),
        snoozed=(AlertItem("t_2", "<script>", snooze=note),),
    )  # fmt: skip
    html = render.telegram_html(event, EN)
    assert "<script>" not in html and "<b>@" not in html and "‮" not in html
    assert "&lt;script&gt;" in html and f"{AT_SIGN}everyone" in html and f"{AT_SIGN}here" in html
    blocks = json.dumps(render.slack_payload(event, EN)["blocks"])
    assert "@everyone" not in blocks and "@here" not in blocks


def test_the_footer_lists_at_most_five_and_counts_the_rest() -> None:
    note = SnoozeNote(SnoozeState.HELD, UNTIL, "x" * 900, "y" * 900)
    many = tuple(AlertItem(f"t_{n}", "t" * 300, snooze=note) for n in range(MAX_SNOOZED + 3))
    event = AlertEvent(
        "needs_you", "escalate", "r" * 5000, Severity.WARNING,
        items=tuple(AlertItem(f"a_{n}", "a" * 300, needed="n" * 600) for n in range(12)),
        snoozed=many,
    )  # fmt: skip
    text = render.plain(event, EN)
    assert text.count("\nSnoozed: ") == MAX_SNOOZED
    assert "and 3 more snoozed" in text
    assert utf16_len(text) <= MAX_TEXT
    assert utf16_len(render.telegram_html(event, EN)) <= MAX_TEXT
    ru = render.plain(event, RU)
    assert ru.count("\nОтложено: ") == MAX_SNOOZED and "и ещё 3 отложено" in ru  # noqa: RUF001
    for block in render.slack_payload(event, EN)["blocks"]:
        assert len(json.dumps(block)) < 4000


def test_a_task_without_a_title_is_named_by_its_id() -> None:
    note = SnoozeNote(SnoozeState.HELD, UNTIL)
    event = AlertEvent(
        "needs_you", "escalate", "Needs you.", Severity.WARNING,
        items=(AlertItem("t_1", "x"),), snoozed=(AlertItem("t_2", None, snooze=note),),
    )  # fmt: skip
    assert "\nSnoozed: t_2 through 2026-10-25\n" in render.plain(event, EN)


def test_an_expired_marker_says_it_was_a_marker() -> None:
    note = SnoozeNote(SnoozeState.EXPIRED, UNTIL, marker=True)
    event = AlertEvent(
        "needs_you", "escalate", "x", Severity.WARNING, items=(AlertItem("t", "t", snooze=note),)
    )
    assert "   Snooze ran through 2026-10-25 (from marker)\n" in render.plain(event, EN)
    assert "   Отсрочка действовала по 2026-10-25 включительно (по маркеру)\n" in render.plain(
        event, RU
    )


def test_russian_growth_counts_agree() -> None:
    def mark(n: int) -> str:
        note = SnoozeNote(SnoozeState.GROWN, UNTIL, grown=n)
        event = AlertEvent(
            "needs_you",
            "escalate",
            "x",
            Severity.WARNING,
            items=(AlertItem("t", "t", snooze=note),),
        )
        return render.plain(event, RU).splitlines()[3].strip()

    assert mark(1) == "После отсрочки заблокирована ещё 1 задача (по 2026-10-25 включительно)"
    assert mark(3) == "После отсрочки заблокированы ещё 3 задачи (по 2026-10-25 включительно)"
    assert mark(5) == "После отсрочки заблокировано ещё 5 задач (по 2026-10-25 включительно)"


# --- from the action's params --------------------------------------------------------------


def action(**params: object) -> Action:
    return Action(ActionKind.ESCALATE, "needs_you", "Needs you.", params=params)


def test_the_engine_reads_the_footer_and_the_marks_from_params() -> None:
    board = Snapshot(NOW)
    snooze = {
        "state": "snoozed",
        "until": UNTIL.isoformat(),
        "reason": " later ",
        "by": "",
        "source": "cli",
    }
    found = action(
        items=[
            {
                "task_id": "t_1",
                "snooze": {"state": "expired", "until": UNTIL.isoformat(), "source": "marker"},
            },
            {"task_id": "t_2", "snooze": {"state": "bogus", "until": UNTIL.isoformat()}},
            {
                "task_id": "t_3",
                "snooze": {"state": "grown", "until": UNTIL.isoformat(), "grown": 2},
            },
        ],
        snoozed=[
            {"task_id": "t_4", "title": "QA", "snooze": snooze},
            {"task_id": "t_5"},  # no snooze: not a snoozed task
            {"task_id": "t_6", "snooze": {**snooze, "until": "not a date"}},
            {"task_id": "t_7", "snooze": {**snooze, "until": "2026-10-26T00:00:00"}},  # no zone
            {"task_id": "t_8", "snooze": {**snooze, "grown": -1}},
            {"task_id": "t_9", "snooze": {**snooze, "by": 3}},
            "junk",
        ],
    )
    items = alert_items(found, board)
    assert [i.snooze for i in items] == [
        SnoozeNote(SnoozeState.EXPIRED, UNTIL, marker=True),
        None,  # a malformed snooze is dropped, the item kept
        SnoozeNote(SnoozeState.GROWN, UNTIL, grown=2),
    ]
    (held,) = snoozed_items(found, board)
    assert held.task_id == "t_4" and held.snooze == SnoozeNote(SnoozeState.HELD, UNTIL, "later")
    assert snoozed_items(action(snoozed="junk"), board) == ()
    assert snoozed_items(action(), board) == ()
    for edge in ("9999-12-31T23:00:00-05:00", "0001-01-01T00:30:00+01:00"):  # out of range
        far = {**snooze, "until": edge}
        assert snoozed_items(action(snoozed=[{"task_id": "t", "snooze": far}]), board) == ()


def test_and_n_more_snoozed_counts_the_total_the_rule_sent() -> None:
    from kelpie.core.engine import Engine
    from tests.fakes import FakeAdapter, FakeAlerter

    note = {"state": "snoozed", "until": UNTIL.isoformat()}
    sent = [{"task_id": f"t_{n}", "snooze": note} for n in range(MAX_SNOOZED + 1)]
    escalation = Action(
        ActionKind.ESCALATE, "needs_you", "Needs you.",
        params={"items": [{"task_id": "a"}], "snoozed": sent, "snoozed_total": 250},
    )  # fmt: skip
    alerter = FakeAlerter()
    Engine([], adapters=[FakeAdapter()], alerter=alerter)._alert(escalation, Snapshot(NOW))
    (event,) = alerter.events
    assert event.snoozed_total == 250 and len(event.snoozed) == MAX_SNOOZED + 1
    assert f"and {250 - MAX_SNOOZED} more snoozed" in render.plain(event, EN)
    fewer = AlertEvent("needs_you", "e", "x", Severity.WARNING, snoozed=event.snoozed)
    assert "and 1 more snoozed" in render.plain(fewer, EN)  # no total: as many as listed


def test_a_single_tasks_alert_carries_its_snooze_from_params() -> None:
    alert = Action(
        ActionKind.ALERT, "stale_blocks", "Task t_1 is overdue.", task_id="t_1",
        params={"snooze": {"state": "expired", "until": UNTIL.isoformat(), "by": "ceo"}},
    )  # fmt: skip
    (item,) = alert_items(alert, Snapshot(NOW))
    assert item.snooze == SnoozeNote(SnoozeState.EXPIRED, UNTIL, by="ceo")
    event = AlertEvent("stale_blocks", "alert t_1", alert.reason, Severity.WARNING, items=(item,))
    assert "Snooze ran through 2026-10-25" in render.plain(event, EN)


def test_the_footer_is_part_of_the_fingerprint() -> None:
    base = AlertEvent("needs_you", "escalate", "x", Severity.WARNING)
    other = AlertEvent("needs_you", "escalate", "x", Severity.WARNING, snoozed=DIGEST.snoozed)
    assert base.fingerprint != other.fingerprint


def test_alerts_show_a_snoozes_end_in_the_reports_time_zone(tmp_path: Path) -> None:
    from kelpie.alerts.config import load_alerts
    from kelpie.app import load_settings

    moscow = ZoneInfo("Europe/Moscow")
    assert load_alerts(None, tz=moscow).rendering.tz is moscow
    assert load_alerts({"language": "ru"}, tz=moscow).rendering.tz is moscow
    assert load_alerts(None).rendering.tz is UTC
    path = tmp_path / "kelpie.toml"
    path.write_text('[storage]\npath = ":memory:"\n\n[report]\ntimezone = "Europe/Moscow"\n')
    assert str(load_settings(path).alerts.rendering.tz) == "Europe/Moscow"
