"""The Slack channel against a local fake of an incoming webhook (no real network)."""

import logging
import time
from collections.abc import Callable
from datetime import timedelta

import pytest

from kelpie.alerts import Delivery, PermanentError, TransientError
from kelpie.alerts.config import load_alerts
from kelpie.alerts.slack import SlackChannel, SlackSettings, load_slack
from kelpie.alerts.slack import payload as render_payload
from kelpie.alerts.telegram import MAX_TEXT
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, AlertItem, Did, Transition
from kelpie.metrics import Metrics
from tests.alerts.fake_slack import FakeSlack, fake_slack
from tests.alerts.fake_telegram import fake_telegram

SECRET = "T0SECRETTEAM/B0SECRETBOT/XSECRETtokenValue0123456789"
PATH = f"/services/{SECRET}"
ENV = "KELPIE_TEST_SLACK_WEBHOOK"
TG_ENV = "KELPIE_TEST_TELEGRAM_TOKEN"
TG_TOKEN = "123456:ABCdef_SECRET-token-0123456789abc"


def settings(**extra: object) -> SlackSettings:
    entry = {"type": "slack", "webhook_env": ENV, "allow_host": "127.0.0.1", **extra}
    return load_slack(entry, "test")


def channel(fake: FakeSlack, **extra: object) -> SlackChannel:
    return settings(**extra).build({ENV: fake.origin + PATH})


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


def sections(payload: dict[str, object]) -> list[str]:
    """The text a person sees, per rich_text block."""
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return [
        "".join(e["text"] for section in b["elements"] for e in section["elements"]) for b in blocks
    ]


def test_message_has_the_telegram_layout_as_blocks() -> None:
    payload = render_payload(message(transition=Transition.ESCALATED, did=Did.SUGGESTED))
    head, tail = sections(payload)
    assert head == "\U0001f7e1 task-1 has had no owner for 3h. · still open"
    assert tail == "Kelpie did: nothing, mode suggest. A human decides.\nRule: orphans"
    assert all(b["type"] == "rich_text" for b in payload["blocks"])
    headline = payload["blocks"][0]["elements"][0]["elements"][1]
    assert headline == {
        "type": "text",
        "text": "task-1 has had no owner for 3h.",
        "style": {"bold": True},
    }
    assert payload["text"].splitlines() == [
        "\U0001f7e1 task-1 has had no owner for 3h. · still open",
        "Kelpie did: nothing, mode suggest. A human decides.",
        "Rule: orphans",
    ]
    assert payload["mrkdwn"] is False  # the fallback is plain text
    assert "failed" in sections(render_payload(message(did=Did.FAILED)))[-1]
    still = message(reason="no owner", transition=Transition.ESCALATED, age=timedelta(hours=4))
    assert sections(render_payload(still))[0] == "\U0001f7e1 no owner · still open after 4.0 h"


def test_one_block_per_item() -> None:
    items = (AlertItem("t-1", "One", blocking=0), AlertItem("t-2", "Two", needed="Do it."))
    payload = render_payload(message(rule="needs_you", items=items))
    assert len(payload["blocks"]) == 5  # headline, two items, the reason, the tail
    assert sections(payload)[-2] == "Why: task-1 has had no owner for 3h."  # not all say
    assert sections(payload)[1:-2] == [
        "1. One · t-1\n   Blocking: none",
        "2. Two · t-2\n   Needed: Do it.",
    ]


def test_the_fallback_is_escaped_and_the_blocks_carry_the_text_as_it_is() -> None:
    reason = "<!channel> <http://evil|x> a & b &lt; \x00\x1b"
    payload = render_payload(message(reason=reason))
    assert "&lt;!channel&gt; &lt;http://evil|x&gt; a &amp; b &amp;lt;" in payload["text"]
    assert "<" not in payload["text"] and ">" not in payload["text"]
    head = sections(payload)[0]
    assert "<!channel> <http://evil|x> a & b &lt;" in head  # a text element: not parsed
    assert "\x00" not in head and "\x1b" not in head
    links = [
        e
        for b in payload["blocks"]
        for section in b["elements"]
        for e in section["elements"]
        if e["type"] != "text"
    ]
    assert links == []  # agent text never becomes a link
    assert render_payload(message(text="a<b&c>d")) == {"text": "a&lt;b&amp;c&gt;d", "mrkdwn": False}


def test_caps_keep_every_block_below_slacks_limit_and_kelpie_did_survives() -> None:
    payload = render_payload(message(reason="y" * 20_000, subject="s" * 5000, did=Did.FAILED))
    assert "tried, and it failed" in sections(payload)[-1]
    assert all(len(t) <= 3000 for t in sections(payload)) and len(payload["text"]) <= 4000
    assert len(render_payload(message(text="<" * 20_000))["text"]) <= 5 * MAX_TEXT
    render_payload(message(subject="task \ud800 x", reason="r \udfff \ud83d"))


def test_posts_blocks_to_the_webhook_path() -> None:
    with fake_slack() as fake:
        channel(fake).send(message(severity=Severity.CRITICAL))
        channel(fake).send(message(text="free text"))
    ((path, headers, body), (_, _, free)) = fake.requests
    assert path == PATH
    assert headers["Content-Type"] == "application/json"
    assert body["text"].startswith("\U0001f534 task-1 has had no owner")
    assert body["blocks"][0]["type"] == "rich_text" and body["mrkdwn"] is False
    assert body["unfurl_links"] is False and body["unfurl_media"] is False
    assert set(body) == {"text", "mrkdwn", "blocks", "unfurl_links", "unfurl_media"}
    assert free == {
        "text": "free text",
        "mrkdwn": False,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def test_429_with_retry_after_is_retried_and_a_huge_one_fails_as_rate_limited() -> None:
    with fake_slack() as fake:
        fake.replies = [(429, "rate_limited", {"Retry-After": "0.05"})]
        with pytest.raises(TransientError) as info:
            channel(fake).send(message())
        assert info.value.retry_after == 0.05 and info.value.cause == "rate_limited"

        fake.replies = [(429, "", {"Retry-After": "0.01"}), (502, "bad gateway", {})]
        metrics = Metrics()
        delivery = Delivery([channel(fake)], metrics, backoff=0.01)
        delivery.alerter().send(AlertEvent("orphans", "alert task-1", "no owner", Severity.WARNING))
        wait_for(lambda: metrics.alerts_sent["slack"] == 1)
        delivery.close()
        assert len(fake.requests) == 4  # 1 (above) + 429 + 502 + success

        fake.replies = [(429, "", {"Retry-After": "99999999"})]
        with pytest.raises(TransientError) as huge:
            channel(fake).send(message())
        assert (huge.value.retry_after or 0) > 60  # over delivery's cap: no retry

        failing = Metrics()
        failing.set_alert_channels(["slack"])
        fake.replies = [(429, "", {"Retry-After": "99999999"})]
        delivery = Delivery([channel(fake)], failing, backoff=0.01)
        delivery.alerter().send(AlertEvent("orphans", "alert task-2", "no owner", Severity.WARNING))
        wait_for(lambda: failing.alerts_failed[("slack", "rate_limited")] == 1)
        delivery.close()
        assert len(fake.requests) == 6  # not retried

        fake.replies = [(429, "", {}), (429, "", {"Retry-After": "soon"})]
        for _ in range(2):  # missing or malformed header: backoff decides
            with pytest.raises(TransientError) as plain:
                channel(fake).send(message())
            assert plain.value.retry_after is None


def test_5xx_is_transient_and_4xx_is_permanent() -> None:
    with fake_slack() as fake:
        fake.replies = [
            (503, "", {}),
            (403, "invalid_token", {}),
            (404, "no_service", {}),
            (400, "<html>weird page</html>", {}),
        ]
        with pytest.raises(TransientError) as server:
            channel(fake).send(message())
        assert server.value.cause == "server_error" and server.value.retry_after is None
        for code in ("invalid_token", "no_service"):
            with pytest.raises(PermanentError) as rejected:
                channel(fake).send(message())
            assert rejected.value.cause == "rejected" and code in str(rejected.value)
        with pytest.raises(PermanentError) as odd:
            channel(fake).send(message())
        assert str(odd.value) == "HTTP 400"  # free text is not logged


def test_a_redirect_is_refused_and_never_followed() -> None:
    with fake_slack() as target, fake_slack() as fake:
        fake.redirect_to = target.origin
        with pytest.raises(PermanentError) as info:
            channel(fake).send(message())
    assert info.value.cause == "redirect"
    assert target.requests == []  # the webhook never went to the redirect target
    assert SECRET not in str(info.value)


def test_connection_errors_are_transient() -> None:
    with fake_slack() as fake:
        ch = channel(fake)
    with pytest.raises(TransientError) as info:  # nothing listens any more
        ch.send(message())
    assert info.value.cause == "connection" and SECRET not in str(info.value)


def test_the_webhook_never_appears_in_logs_errors_reprs_status_or_metrics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with fake_slack() as fake:
        fake.default = (400, f"{fake.origin}{PATH} {SECRET}", {})
        ch = channel(fake)
        url = fake.origin + PATH
        metrics = Metrics()
        metrics.set_alert_channels(["slack"])
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(PermanentError) as info:
                ch.send(message())
            delivery = Delivery([ch], metrics, backoff=0.001)
            delivery.alerter().send(AlertEvent("r", "alert", "x", Severity.WARNING))
            wait_for(lambda: metrics.alerts_failed[("slack", "rejected")] == 1)
            delivery.close()
        fake.default = (200, "ok", {})
        problems = load_alerts({"channels": [{"type": "slack", "webhook_env": ENV}]}).problems(
            {ENV: url}
        )
        everything = [
            caplog.text,
            str(info.value),
            repr(info.value),
            repr(ch),
            repr(ch._settings),
            ch._settings.describe(),
            repr(delivery),
            metrics.render(),
            *problems,
        ]
    for text in everything:
        assert SECRET not in text and "SECRETBOT" not in text
    assert len(problems) == 1  # no allow_host: the loopback fake is refused, without the value


def test_a_bad_webhook_stops_startup_naming_only_the_variable() -> None:
    cfg = settings()
    with pytest.raises(ValueError, match=ENV) as missing:
        cfg.build({})
    assert "not set" in str(missing.value)
    bad_values = [
        f"http://hooks.slack.com{PATH}",  # not https
        f"https://evil.example{PATH}",  # wrong host
        f"https://user:pw@hooks.slack.com{PATH}",
        f"https://hooks.slack.com{PATH}?x=1",
        f"https://hooks.slack.com:8443{PATH}",
        "https://hooks.slack.com",  # no path
        f"https://hooks.slack.com/../{SECRET}",
        f"http://localhost{PATH}",  # loopback, but not the allowed host
        "not a url SECRETVALUE",
    ]
    for bad in bad_values:
        with pytest.raises(ValueError, match=ENV) as info:
            cfg.build({ENV: bad})
        assert "SECRET" not in str(info.value)
    strict = load_slack({"webhook_env": ENV}, "x")  # no allow_host
    ok = f"https://hooks.slack.com{PATH}"
    strict.build({ENV: ok})
    with pytest.raises(ValueError):
        strict.build({ENV: f"http://127.0.0.1{PATH}"})
    with pytest.raises(ValueError, match="loopback"):  # allow_host never allows plain http
        load_slack({"webhook_env": ENV, "allow_host": "example.org"}, "x").build(
            {ENV: f"http://example.org{PATH}"}
        )
    load_slack({"webhook_env": ENV, "allow_host": "relay.example.org"}, "x").build(
        {ENV: f"https://relay.example.org{PATH}"}
    )
    alerts = load_alerts({"channels": [{"type": "slack", "webhook_env": ENV}]})
    assert alerts.problems({}) == [f"[alerts] channel slack: ${ENV} is not set"]
    assert alerts.problems({ENV: ok}) == []
    with pytest.raises(ValueError, match=ENV):
        alerts.build(environ={})
    assert cfg.describe().count(ENV) == 1


def test_config_validation() -> None:
    ok = {"type": "slack", "webhook_env": ENV}
    cfg = load_slack(ok, "x")
    assert (cfg.name, cfg.min_severity, cfg.timeout.total_seconds(), cfg.allow_host) == (
        "slack",
        Severity.WARNING,
        10.0,
        "",
    )
    for bad in (
        {"webhook_env": "bad name"},
        {"webhook_env": 5},
        {"name": "Not Lower"},
        {"timeout": "0s"},
        {"timeout": "1h"},
        {"min_severity": "loud"},
        {"allow_host": "http://example.org"},
        {"allow_host": "example.org:80"},
        {"surprise": 1},
    ):
        with pytest.raises(ValueError):
            load_slack({**ok, **bad}, "x")
    with pytest.raises(ValueError, match="unique"):
        load_alerts({"channels": [ok, ok]})
    two = load_alerts({"channels": [ok, {**ok, "name": "slack-ops", "min_severity": "critical"}]})
    assert [c.name for c in two.channels] == ["slack", "slack-ops"]
    with pytest.raises(ValueError, match="slack"):
        load_alerts({"channels": [{"type": "pager"}]})


def test_fan_out_to_telegram_and_slack_with_per_channel_filters() -> None:
    from kelpie.alerts.telegram import load_telegram

    tg_entry = {"type": "telegram", "token_env": TG_ENV, "chat_id": "-100123"}
    with fake_slack() as slack, fake_telegram() as tg:
        telegram_ch = load_telegram({**tg_entry, "api_url": tg.url}, "t").build({TG_ENV: TG_TOKEN})
        slack_ch = channel(slack, min_severity="critical")
        metrics = Metrics()
        delivery = Delivery([telegram_ch, slack_ch], metrics, backoff=0.001)
        delivery.send("warning for telegram only", "warning")
        delivery.send("<critical> for both", "critical")
        delivery.send("named slack report", "info", ["slack"])
        wait_for(lambda: len(tg.requests) == 2 and len(slack.requests) == 2)
        delivery.close()
    assert [r[2]["text"] for r in tg.requests] == [
        "warning for telegram only",
        "<critical> for both",
    ]
    assert [r[2]["text"] for r in slack.requests] == [
        "&lt;critical&gt; for both",
        "named slack report",
    ]
    assert metrics.alerts_sent["telegram"] == 2 and metrics.alerts_sent["slack"] == 2


def test_slack_mentions_in_agent_text_are_neutralised() -> None:
    hostile = "<!channel> <!here> <!everyone> <@U012ABCDEF> <#C012ABCDEF|general> <!subteam^S1>"
    item = AlertItem(hostile, hostile, needed=hostile + " @here @channel")
    alert = render_payload(message(reason=hostile, rule="<!here>", items=(item,)))
    # the fallback: Slack's escaping, nothing it could parse as a mention or a link
    for text in (alert["text"], render_payload(message(text=hostile))["text"]):
        assert "<!" not in text and "<@" not in text and "<#" not in text
        assert "@" not in text and "&lt;!channel&gt;" in text
    # the blocks: text elements (never parsed), and @ broken all the same
    for text in sections(alert):
        assert "@" not in text
    with fake_slack() as fake:
        channel(fake).send(message(reason=hostile))
    sent = fake.requests[0][2]
    assert sent["text"].count("&lt;!here&gt;") == 1
    assert "<@U012ABCDEF>" not in str(sent)  # broken: a fullwidth @
    assert "\u200b" not in str(render_payload(message(reason="a\u200b<!here>")))  # Cf removed


def test_slack_a_reason_that_looks_like_transition_wording_is_only_text() -> None:
    reason = "Changed: Still open after 4.0h: task-1 has no owner."
    assert sections(render_payload(message(reason=reason)))[0] == f"\U0001f7e1 {reason}"
    again = message(reason=reason, transition=Transition.ESCALATED, age=timedelta(hours=4))
    expected = f"\U0001f7e1 {reason} · still open after 4.0 h"
    assert sections(render_payload(again))[0] == expected


def test_a_huge_error_page_from_a_5xx_or_429_is_still_transient() -> None:
    with fake_slack() as fake:
        fake.replies = [(503, "x" * 200_000, {}), (429, "y" * 200_000, {"Retry-After": "2"})]
        for cause in ("server_error", "rate_limited"):
            with pytest.raises(TransientError) as info:
                channel(fake).send(message())
            assert info.value.cause == cause
