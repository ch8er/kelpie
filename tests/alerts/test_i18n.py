"""The template language catalog and the [alerts] settings that choose it."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.alerts import i18n
from kelpie.alerts.config import load_alerts
from kelpie.alerts.render import RenderSettings
from kelpie.core.alerts import Did

RULES = Path(__file__).parents[2] / "src" / "kelpie" / "rules"


def test_every_english_key_exists_in_every_language() -> None:
    for lang, catalog in i18n.CATALOG.items():
        assert set(i18n.EN) <= set(catalog), lang
        for key, template in catalog.items():
            fields = set(re.findall(r"{(\w+)}", template))
            if key in i18n.EN:
                assert fields == set(re.findall(r"{(\w+)}", i18n.EN[key])), (lang, key)


def test_every_rule_that_alerts_has_a_headline() -> None:
    alerting = set()
    for path in (p for p in RULES.glob("*.py") if not p.name.startswith("_")):
        source = path.read_text(encoding="utf-8")
        if "ActionKind.ALERT" in source or "ActionKind.ESCALATE" in source:
            name = re.search(r'^NAME = "(\w+)"', source, re.M)
            assert name, path
            alerting.add(name.group(1))
    assert alerting and all(f"headline.{rule}" in i18n.EN for rule in alerting)


def test_every_did_but_notified_has_a_line() -> None:
    for did in Did:
        assert (f"did.{did.value}" in i18n.EN) is (did is not Did.NOTIFIED)


@pytest.mark.parametrize(
    ("n", "en", "ru"),
    [
        (0, "0 tasks", "0 задач"),
        (1, "1 task", "1 задача"),
        (2, "2 tasks", "2 задачи"),
        (4, "4 tasks", "4 задачи"),
        (5, "5 tasks", "5 задач"),
        (11, "11 tasks", "11 задач"),
        (12, "12 tasks", "12 задач"),
        (21, "21 tasks", "21 задача"),
        (22, "22 tasks", "22 задачи"),
        (111, "111 tasks", "111 задач"),
    ],
)
def test_plural_forms(n: int, en: str, ru: str) -> None:
    assert (i18n.tasks("en", n), i18n.tasks("ru", n)) == (en, ru)


def test_ages_read_as_minutes_or_hours() -> None:
    assert i18n.age("en", timedelta(seconds=30)) == "0 min"
    assert i18n.age("en", timedelta(minutes=45)) == "45 min"
    assert i18n.age("en", timedelta(hours=4, minutes=33)) == "4.5 h"
    assert i18n.age("ru", timedelta(hours=4, minutes=33)) == "4,5 ч"
    assert i18n.age("en", timedelta(hours=44, minutes=21)) == "44 h"
    assert i18n.age("ru", timedelta(minutes=5)) == "5 мин"
    assert i18n.age("en", -timedelta(hours=1)) == "0 min"


def test_text_falls_back_to_english() -> None:
    assert i18n.text("xx", "label.needed") == "Needed"
    assert i18n.has("ru", "headline.stale_blocks") and not i18n.has("ru", "headline.nope")


def test_alerts_language_and_task_url_config() -> None:
    assert load_alerts(None).rendering == RenderSettings()
    assert load_alerts({"language": "ru"}).rendering.language == "ru"
    for bad in ("de", "EN", 1):
        with pytest.raises(ValueError, match=r"\[alerts\] language"):
            load_alerts({"language": bad})
    url = "https://board.example.org/t/{id}"
    assert load_alerts({"task_url": url}).rendering.task_url == url
    for bad in ("https://board.example.org/t/", "javascript:x/{id}", 3):
        with pytest.raises(ValueError, match=r"\[alerts\] task_url"):
            load_alerts({"task_url": bad})


def test_task_url_defaults_to_the_reports_when_that_is_a_web_link(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = "https://board.example.org/report/{id}"
    assert load_alerts(None, report).rendering.task_url == report
    assert load_alerts({}, report).rendering.task_url == report
    own = "https://board.example.org/t/{id}"
    assert load_alerts({"task_url": own}, report).rendering.task_url == own
    # a report link that is not http(s) only links the report, and Kelpie says so
    with caplog.at_level(logging.WARNING):
        ignored = load_alerts({}, "board://t/{id}")
    assert ignored.rendering.task_url is None and "no task links" in caplog.text
    assert ignored.links_note is not None and "[report] task_url" in ignored.links_note
    assert load_alerts({}, report).links_note is None
