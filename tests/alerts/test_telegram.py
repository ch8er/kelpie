"""The Telegram channel against a local fake of the Bot API (no real network)."""

import logging
import time
import unicodedata
from collections.abc import Callable
from datetime import timedelta

import pytest

from kelpie.alerts import Delivery, PermanentError, TransientError
from kelpie.alerts.config import load_alerts
from kelpie.alerts.telegram import (
    AT_SIGN,
    MAX_TEXT,
    TelegramChannel,
    TelegramSettings,
    clean,
    defang,
    format_message,
    load_telegram,
    truncate,
    utf16_len,
)
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, Did, Transition
from kelpie.metrics import Metrics
from tests.alerts.fake_telegram import FakeTelegram, fake_telegram

TOKEN = "123456:ABCdef_SECRET-token-0123456789abc"
ENV = "KELPIE_TEST_TELEGRAM_TOKEN"


def settings(url: str, **extra: object) -> TelegramSettings:
    entry = {"type": "telegram", "token_env": ENV, "chat_id": "-100123", "api_url": url, **extra}
    return load_telegram(entry, "test")


def channel(fake: FakeTelegram, **extra: object) -> TelegramChannel:
    return settings(fake.url, **extra).build({ENV: TOKEN})


def message(**kw: object) -> AlertEvent:
    base: dict[str, object] = {
        "rule": "orphans",
        "subject": "alert task-1",
        "reason": "task-1 has had no owner for 3h.",
        "severity": Severity.WARNING,
        "transition": Transition.NEW,
        "did": Did.NOTIFIED,
    }
    return AlertEvent(**{**base, **kw})  # type: ignore[arg-type]


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.005)


def test_message_shows_severity_transition_and_what_kelpie_did() -> None:
    text = format_message(message(transition=Transition.ESCALATED, did=Did.SUGGESTED))
    first, second, third = text.split("\n")
    assert first == "[WARNING] orphans: alert task-1"
    assert second == "Still open. task-1 has had no owner for 3h."
    assert third.startswith("Kelpie did: nothing, mode suggest")
    assert "only sent this alert" in format_message(message())
    assert "failed" in format_message(message(did=Did.FAILED))
    assert "no executor" in format_message(message(did=Did.NOT_EXECUTED))
    resolved = message(
        reason="gone.",
        severity=Severity.INFO,
        transition=Transition.RESOLVED,
        age=timedelta(hours=1),
    )
    # The transition is worded from the fields (core.alerts.lead_in), once.
    assert format_message(resolved).split("\n")[1] == "Resolved after 1.0h: gone."
    still = message(reason="no owner", transition=Transition.ESCALATED, age=timedelta(hours=4))
    assert format_message(still).split("\n")[1] == "Still open after 4.0h: no owner"
    changed = message(
        reason="now critical", severity=Severity.CRITICAL, transition=Transition.CHANGED
    )
    assert format_message(changed).split("\n")[1] == "Changed: now critical"


def test_untrusted_text_is_plain_and_control_characters_are_removed() -> None:
    text = format_message(message(reason="*bold* _it_ [x](http://evil) \x00\x1b[31m"))
    assert "*bold* _it_ [x](http://evil)" in text  # no parse_mode: shown literally
    assert "\x00" not in text and "\x1b" not in text


def test_text_is_cut_to_the_telegram_limit_in_utf16_units() -> None:
    assert truncate("x" * MAX_TEXT) == "x" * MAX_TEXT
    cut = truncate("x" * 10_000)
    assert len(cut) <= MAX_TEXT and cut.endswith("(cut)")
    astral = truncate("\U0001f600" * 5000)  # two UTF-16 units each
    assert utf16_len(astral) <= MAX_TEXT and astral.endswith("(cut)")
    assert utf16_len(format_message(message(reason="y" * 20_000))) <= MAX_TEXT


def test_sends_plain_text_to_the_configured_chat() -> None:
    with fake_telegram() as fake:
        channel(fake).send(message(severity=Severity.CRITICAL))
        channel(fake).send(message(severity=Severity.INFO))
    (path, headers, body), (_, _, quiet) = fake.requests
    assert path == f"/bot{TOKEN}/sendMessage"
    assert headers["Content-Type"] == "application/json"
    assert body["chat_id"] == "-100123"
    assert body["disable_web_page_preview"] is True
    assert "parse_mode" not in body
    assert body["text"].startswith("[CRITICAL] orphans: alert task-1")
    assert body["disable_notification"] is False and quiet["disable_notification"] is True


def test_429_carries_retry_after_and_is_retried_by_delivery() -> None:
    limited = {"ok": False, "error_code": 429, "parameters": {"retry_after": 0.05}}
    with fake_telegram() as fake:
        fake.replies = [(429, limited)]
        with pytest.raises(TransientError) as info:
            channel(fake).send(message())
        assert info.value.retry_after == 0.05 and info.value.cause == "rate_limited"

        fake.replies = [(429, limited), (502, {"ok": False})]
        metrics = Metrics()
        delivery = Delivery([channel(fake)], metrics, backoff=0.01)
        delivery.alerter().send(AlertEvent("orphans", "alert task-1", "no owner", Severity.WARNING))
        wait_for(lambda: metrics.alerts_sent["telegram"] == 1)
        delivery.close()
    assert len(fake.requests) == 4  # 1 (above) + 429 + 502 + success


def test_other_statuses_are_permanent_or_transient() -> None:
    with fake_telegram() as fake:
        fake.replies = [(503, {"ok": False}), (401, {"ok": False, "description": "Unauthorized"})]
        with pytest.raises(TransientError) as server:
            channel(fake).send(message())
        assert server.value.cause == "server_error" and server.value.retry_after is None
        with pytest.raises(PermanentError) as rejected:
            channel(fake).send(message())
        assert rejected.value.cause == "rejected" and "Unauthorized" in str(rejected.value)


def test_a_redirect_is_refused_and_never_followed() -> None:
    with fake_telegram() as target, fake_telegram() as fake:
        fake.redirect_to = target.url
        with pytest.raises(PermanentError) as info:
            channel(fake).send(message())
    assert info.value.cause == "redirect"
    assert target.requests == []  # the token never went to the redirect target
    assert TOKEN not in str(info.value)


def test_connection_errors_and_timeouts_are_transient() -> None:
    with fake_telegram() as fake:
        url = fake.url
    with pytest.raises(TransientError) as info:  # nothing listens any more
        settings(url).build({ENV: TOKEN}).send(message())
    assert info.value.cause == "connection" and TOKEN not in str(info.value)


def test_the_token_never_appears_in_logs_errors_reprs_or_metrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    echo = {"ok": False, "description": f"Bad Request: bot{TOKEN} is not allowed"}
    with fake_telegram() as fake:
        fake.default = (400, echo)
        ch = channel(fake)
        metrics = Metrics()
        metrics.set_alert_channels(["telegram"])
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PermanentError) as info:
                ch.send(message())
            delivery = Delivery([ch], metrics, backoff=0.001)
            delivery.alerter().send(AlertEvent("r", "alert", "x", Severity.WARNING))
            wait_for(lambda: metrics.alerts_failed[("telegram", "rejected")] == 1)
            delivery.close()
        assert "<redacted>" in str(info.value)
        everything = [
            caplog.text,
            str(info.value),
            repr(info.value),
            repr(ch),
            repr(ch._settings),
            repr(delivery),
            metrics.render(),
        ]
        fake.default = (200, {"ok": True})
    for text in everything:
        assert TOKEN not in text and TOKEN.split(":")[1] not in text


def test_a_missing_or_malformed_token_stops_startup_naming_only_the_variable() -> None:
    cfg = settings("http://127.0.0.1:9")
    with pytest.raises(ValueError, match=ENV) as missing:
        cfg.build({})
    assert "not set" in str(missing.value)
    bad = "not-a-token-SECRETVALUE"
    with pytest.raises(ValueError, match=ENV) as malformed:
        cfg.build({ENV: bad})
    assert bad not in str(malformed.value) and "SECRET" not in str(malformed.value)
    assert cfg.describe().count(ENV) == 1 and TOKEN not in cfg.describe()
    alerts = load_alerts({"channels": [{"type": "telegram", "token_env": ENV, "chat_id": 5}]})
    assert alerts.problems({}) == [f"[alerts] channel telegram: ${ENV} is not set"]
    assert alerts.problems({ENV: TOKEN}) == []
    with pytest.raises(ValueError, match=ENV):
        alerts.build(environ={})
    assert alerts.build(environ={ENV: TOKEN}) is not None


def test_config_validation() -> None:
    ok = {"type": "telegram", "token_env": ENV, "chat_id": "@kelpie_alerts"}
    cfg = load_telegram(ok, "x")
    assert (cfg.min_severity, cfg.timeout, cfg.api_url) == (
        Severity.WARNING,
        timedelta(seconds=10),
        "https://api.telegram.org",
    )
    for bad in (
        {"token_env": "bad name"},
        {"token_env": ENV, "chat_id": "has space"},
        {"chat_id": True},
        {"timeout": "0s"},
        {"timeout": "1h"},
        {"min_severity": "loud"},
        {"api_url": "http://example.org"},
        {"api_url": "https://user:pw@api.telegram.org"},
        {"api_url": "https://api.telegram.org/bot123:abc"},
        {"surprise": 1},
    ):
        with pytest.raises(ValueError):
            load_telegram({**ok, **bad}, "x")
    assert load_telegram({**ok, "api_url": "http://127.0.0.1:8081"}, "x").api_url.startswith("http")
    with pytest.raises(ValueError, match="unique"):
        load_alerts({"channels": [ok, ok]})
    with pytest.raises(ValueError, match="type"):
        load_alerts({"channels": [{"type": "pager"}]})
    with pytest.raises(ValueError, match="queue_size"):
        load_alerts({"queue_size": 0})
    assert load_alerts(None).channels == ()


def test_free_text_is_sent_as_it_is_and_cut_to_the_limit() -> None:
    with fake_telegram() as fake:
        delivery = Delivery([channel(fake)], backoff=0.001)
        delivery.send("Daily report\n" + "x" * 10_000, "info", ["telegram"])
        wait_for(lambda: bool(fake.requests))
        delivery.close()
    text = fake.requests[0][2]["text"]
    assert text.startswith("Daily report\nxxx") and utf16_len(text) <= MAX_TEXT
    assert "Kelpie did" not in text


def test_lone_surrogates_do_not_crash_formatting_or_sending() -> None:
    bad = message(subject="task \ud800 x", reason="reason \udfff \ud83d end")
    text = format_message(bad)
    text.encode("utf-8")  # must be encodable
    assert "reason" in text and "end" in text
    assert utf16_len("\ud800") == 1
    format_message(message(text="free \ud800 text")).encode("utf-8")
    with fake_telegram() as fake:
        channel(fake).send(bad)
    assert len(fake.requests) == 1


def test_a_huge_body_never_cuts_off_what_kelpie_did() -> None:
    text = format_message(message(reason="y" * 20_000, subject="s" * 5000, did=Did.FAILED))
    assert "tried, and it failed" in text.split("\n")[-1]
    assert utf16_len(text) <= MAX_TEXT


def test_invisible_format_characters_are_removed() -> None:
    sneaky = "pay‮evil‬ ok​‍go﻿ ⁦x⁩"  # bidi, zero-width, BOM
    text = format_message(message(reason=sneaky, subject="task​-1"))
    assert all(unicodedata.category(c) != "Cf" for c in text)
    assert "payevil okgo x" in text and "task-1" in text
    assert clean("a­b") == "ab"  # soft hyphen too


def test_mentions_in_agent_controlled_text_are_broken() -> None:
    body = "ping @everyone and @some_user, mail a@b.example"
    text = format_message(message(reason=body, subject="@admin task", rule="@rule"))
    assert "@" not in text  # none left in the agent's fields
    assert all(f"{AT_SIGN}{n}" in text for n in ("everyone", "some_user", "admin"))
    assert "@" not in format_message(message(text="Daily report for @team"))
    assert defang("x@y") == f"x{AT_SIGN}y"


def test_the_chat_id_is_not_affected_by_mention_breaking() -> None:
    with fake_telegram() as fake:
        channel(fake, chat_id="@kelpie_alerts").send(message(reason="@everyone"))
    body = fake.requests[0][2]
    assert body["chat_id"] == "@kelpie_alerts"
    assert "@everyone" not in body["text"]


def test_retry_horizon_config() -> None:
    assert load_alerts(None).retry_horizon == timedelta(minutes=15)
    assert load_alerts({"retry_horizon": "30m"}).retry_horizon == timedelta(minutes=30)
    assert load_alerts({"retry_horizon": "0s"}).retry_horizon == timedelta(0)
    for bad in ("soon", "-5m", "2d", True):
        with pytest.raises(ValueError, match="retry_horizon"):
            load_alerts({"retry_horizon": bad})


def test_a_huge_error_page_from_a_5xx_or_429_is_still_transient() -> None:
    huge = {"ok": False, "description": "x" * 200_000}
    with fake_telegram() as fake:
        fake.replies = [(502, huge), (429, huge)]
        for cause in ("server_error", "rate_limited"):
            with pytest.raises(TransientError) as info:
                channel(fake).send(message())
            assert info.value.cause == cause
