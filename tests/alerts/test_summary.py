"""The daily report's chat summary: golden files per case and format, the limits, the
escaping, and its way through the delivery layer and the two channels."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from kelpie.alerts import Delivery, TransientError, summary
from kelpie.alerts.delivery import SUMMARY_RULE, SUMMARY_SUBJECT
from kelpie.alerts.render import (
    AT_SIGN,
    MAX_TEXT,
    SLACK_BLOCK,
    SLACK_BLOCKS,
    SLACK_TEXT,
    RenderSettings,
    check_task_url,
    plain_http_url,
    utf16_len,
)
from kelpie.alerts.slack import load_slack
from kelpie.alerts.telegram import load_telegram
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, Summary
from kelpie.metrics import Metrics
from tests.alerts.fake_slack import fake_slack
from tests.alerts.fake_telegram import fake_telegram
from tests.alerts.snapshot import assert_snapshot

EN = RenderSettings()
RU = RenderSettings("ru")
LINK = "https://reports.example.org/kelpie/2026-03-02?view=full&lang=en"
TITLE = "Kelpie daily report 2026-03-02"
HEADLINE = (
    "Cost of chaos: 48.0 hours lost, $120.00 in 1,234,567 tokens burned + $120.00 in stuck "
    "hours. 5 tasks completed, $24.00 per completed task."
)
ITEMS = (
    "Biggest wait: reviewer tasks spent 64.0 task-hours waiting for CI (4 tasks).",
    "Needs a human: 2 open condition(s).",
    "1. Cut the reviewer wait for CI",
    "2. Give the reviewer role a second slot",
    "3. Filter the output of `pytest -v`",
)
LONG_ITEM = (
    "1. Move the nightly backup of d1 off the shared runner: it retried three times and "
    "burned 40 minutes of CI each night. Нужно включить R2 в панели Cloudflare, затем "
    "перезапустить запуск и проверить, что архив появился в бакете и что ротация старых "
    "копий работает; до этого задача не может закрыться, а релиз ждёт её. " * 3  # noqa: RUF001 (Russian)
)


def summary_event(
    headline: str = HEADLINE,
    lines: tuple[str, ...] = ITEMS,
    link: str | None = LINK,
    title: str | None = TITLE,
    severity: Severity = Severity.WARNING,
) -> AlertEvent:
    return AlertEvent(
        SUMMARY_RULE,
        SUMMARY_SUBJECT,
        headline,
        severity,
        headline=headline,
        summary=Summary(lines, link, title),
    )


HOSTILE = summary_event(
    headline='<b>Cost</b> *of* _chaos_ ~gone~ ```fence``` & @everyone <!channel> pay‮evil‬ "q"',
    lines=(
        "<script>alert(1)</script> *now* @here &amp; <http://evil|click>",
        "1. Fix `<code>` ⁦x⁩",
    ),
    title="Kelpie daily report <2026-03-02> @all",
)
OVERFLOW = summary_event(
    lines=tuple(f"{n}. " + '"&<>' * 150 for n in range(40)),
)

CASES: dict[str, tuple[AlertEvent, RenderSettings]] = {
    "summary": (summary_event(), EN),
    "summary_no_link": (summary_event(link=None), EN),
    "summary_ru": (summary_event(), RU),
    "summary_ru_no_link": (summary_event(link=None), RU),
    "summary_long_item": (summary_event(lines=(*ITEMS[:2], LONG_ITEM)), EN),
    "summary_overflow_ru": (summary_event(lines=ITEMS * 3), RU),
    "summary_no_title_info": (summary_event(title=None, severity=Severity.INFO), EN),
    "summary_headline_only": (summary_event(lines=(), link=None, title=None), EN),
    "summary_hostile": (HOSTILE, EN),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_rendered_summaries_match_their_snapshots(case: str) -> None:
    event, settings = CASES[case]
    assert_snapshot(f"{case}.txt", summary.plain(event, settings))
    assert_snapshot(f"{case}.html", summary.telegram_html(event, settings))
    payload = summary.slack_payload(event, settings)
    assert_snapshot(f"{case}.json", json.dumps(payload, indent=2, ensure_ascii=False))


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.tags: set[str] = set()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.open.append(tag)
        self.tags.add(tag)
        if tag == "a":
            assert [k for k, _ in attrs] == ["href"]
            self.hrefs.append(dict(attrs)["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        assert self.open and self.open.pop() == tag


@pytest.mark.parametrize("case", [*sorted(CASES), "overflow"])
def test_every_rendering_is_well_formed_and_within_the_limits(case: str) -> None:
    event, settings = (OVERFLOW, EN) if case == "overflow" else CASES[case]
    html_text = summary.telegram_html(event, settings)
    parser = _Tags()
    parser.feed(html_text)
    parser.close()
    assert parser.open == [] and parser.tags <= {"b", "a"}
    assert parser.hrefs in ([], [LINK])
    assert utf16_len(html_text) <= MAX_TEXT
    assert utf16_len(summary.plain(event, settings)) <= MAX_TEXT
    payload = summary.slack_payload(event, settings)
    assert len(payload["text"]) <= SLACK_TEXT and payload["mrkdwn"] is False
    assert 0 < len(payload["blocks"]) <= SLACK_BLOCKS
    for block in payload["blocks"]:
        assert block["type"] == "rich_text"
        (section,) = block["elements"]
        assert section["type"] == "rich_text_section"
        assert sum(len(e["text"]) for e in section["elements"]) <= SLACK_BLOCK
        for e in section["elements"]:
            assert e["type"] in ("text", "link") and e["text"]
            assert set(e.get("style", {})) <= {"bold"}
    raw = json.dumps(payload, ensure_ascii=False)
    for text in (html_text, summary.plain(event, settings), raw):
        assert all(unicodedata.category(c) != "Cf" for c in text), case


def test_the_layout_is_title_headline_items_and_link() -> None:
    lines = summary.plain(summary_event(), EN).splitlines()
    assert lines[:3] == [TITLE, f"\U0001f7e1 {HEADLINE}", ""]
    assert lines[3:8] == list(ITEMS)
    assert lines[8:] == ["", f"Open report: {LINK}"]
    html_text = summary.telegram_html(summary_event(), EN)
    assert html_text.splitlines()[1] == f"\U0001f7e1 <b>{HEADLINE}</b>"
    assert html_text.splitlines()[-1] == f'<a href="{LINK.replace("&", "&amp;")}">Open report</a>'
    payload = summary.slack_payload(summary_event(), EN)
    head, body, link = payload["blocks"]
    assert [e["text"] for e in head["elements"][0]["elements"]] == [
        f"{TITLE}\n",
        "\U0001f7e1 ",
        HEADLINE,
    ]
    assert head["elements"][0]["elements"][2]["style"] == {"bold": True}
    assert body["elements"][0]["elements"] == [{"type": "text", "text": "\n".join(ITEMS)}]
    assert link["elements"][0]["elements"] == [{"type": "link", "url": LINK, "text": "Open report"}]


def test_the_fixed_words_follow_the_language_and_the_report_text_stays_english() -> None:
    ru = summary.plain(summary_event(lines=ITEMS * 3), RU)
    assert ru.splitlines()[-3:] == ["и ещё 5", "", f"Открыть отчёт: {LINK}"]
    assert HEADLINE in ru and ITEMS[0] in ru and TITLE in ru
    assert "Open report" not in ru
    assert "and 5 more" in summary.plain(summary_event(lines=ITEMS * 3), EN)


def test_the_severity_picks_the_icon() -> None:
    for severity, icon in (
        (Severity.INFO, "\U0001f535"),
        (Severity.WARNING, "\U0001f7e1"),
        (Severity.CRITICAL, "\U0001f534"),
    ):
        text = summary.plain(summary_event(severity=severity, title=None), EN)
        assert text.startswith(f"{icon} {HEADLINE}")


def test_a_link_must_be_a_plain_http_url() -> None:
    for bad in (
        "javascript:alert(1)",
        "ftp://reports.example.org/x",
        "https://u:p@reports.example.org/x",
        'https://reports.example.org/x"onmouseover="y',
        "https://reports.example.org/a b",
        "https://reports.example.org/<x>",
        "https:///nohost",
        "https://reports.example.org/" + "x" * 700,
        "",
    ):
        event = summary_event(link=bad)
        assert "Open report" not in summary.plain(event, EN), bad
        assert all(
            b["elements"][0]["elements"][0]["type"] != "link"
            for b in summary.slack_payload(event, EN)["blocks"]
        ), bad
    assert plain_http_url("http://localhost:8080/r/2026-03-02") is not None
    assert plain_http_url(None) is None and plain_http_url(LINK) == LINK


def test_hostile_text_is_escaped_everywhere() -> None:
    html_text = summary.telegram_html(HOSTILE, EN)
    assert "<script>" not in html_text and "<!channel>" not in html_text
    assert "<http" not in html_text and "@" not in html_text
    assert "‮" not in html_text and "⁦" not in html_text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_text
    assert "&amp;amp;" in html_text  # the report's own "&amp;" is shown, not decoded
    assert f"{AT_SIGN}here" in html_text
    plain = summary.plain(HOSTILE, EN)
    assert "@" not in plain and "‮" not in plain and "*now*" in plain
    payload = summary.slack_payload(HOSTILE, EN)
    assert "<!channel>" not in payload["text"] and "&lt;!channel&gt;" in payload["text"]
    raw = json.dumps(payload, ensure_ascii=False)
    assert "@" not in raw and "‮" not in raw
    parts = [e for b in payload["blocks"] for s in b["elements"] for e in s["elements"]]
    assert {"type": "text", "text": f"{AT_SIGN}here"} not in parts  # sits inside a longer text
    assert any("<script>alert(1)</script>" in e["text"] for e in parts)  # as it is, unparsed


def test_a_long_summary_drops_lines_from_the_end_and_counts_them() -> None:
    html_text = summary.telegram_html(OVERFLOW, EN)
    assert utf16_len(html_text) <= MAX_TEXT
    last = html_text.splitlines()[-3:]
    assert last[0].startswith("and ") and last[0].endswith(" more")
    assert html_text.startswith(TITLE) and "Open report" in html_text  # head and link survive
    plain = summary.plain(OVERFLOW, EN)
    counted = re.search(r"^and (\d+) more$", plain, re.M)
    assert counted and int(counted[1]) >= 30  # ten lines at most, the rest counted
    assert len(summary.view(OVERFLOW).lines) == summary.MAX_LINES
    pathological = summary_event(
        headline="&" * 5000,
        lines=("&" * 5000,) * 10,
        title="&" * 5000,
        link="https://reports.example.org/?" + "&" * 560,
    )
    assert utf16_len(summary.telegram_html(pathological, EN)) <= MAX_TEXT


def test_the_biggest_wait_line_is_shown_whole() -> None:
    """The longest line the report writes (a role, a repo and a suggestion) is not cut."""
    wait = (
        "Biggest wait: developer tasks spent 14.0 task-hours waiting for an unknown cause (no "
        "condition registered) (1 task(s), mostly on `example/app`), against 16.0 agent-hours "
        "of work. Suggestion: have agents register a condition when they wait (ci_status, "
        "human_task, ...), so Kelpie can tell what they wait for and wake them when it holds."
    )
    assert 300 < len(wait) < summary.MAX_LINE
    event = summary_event(lines=(wait, *ITEMS[1:]))
    assert wait in summary.plain(event, EN)
    assert wait in summary.telegram_html(event, EN).replace("&#x27;", "'")
    body = summary.slack_payload(event, EN)["blocks"][1]
    assert body["elements"][0]["elements"][0]["text"].startswith(wait + "\n")


def test_slack_lines_are_packed_into_blocks_below_the_limit() -> None:
    lines = tuple(f"{n}. " + "x" * 700 for n in range(10))
    blocks = summary.slack_payload(summary_event(lines=lines), EN)["blocks"]
    texts = [b["elements"][0]["elements"][0]["text"] for b in blocks[1:-1]]
    assert len(texts) >= 2 and all(len(t) <= SLACK_BLOCK for t in texts)
    assert "\n".join(texts).split("\n") == list(lines)
    assert blocks[-1]["elements"][0]["elements"][0]["type"] == "link"


def test_a_long_recommendation_list_is_cut_before_a_numbered_line_is_dropped() -> None:
    """Heavy escaping makes the HTML longer than the plain text; the longest lines are cut
    shorter so that the three recommendations stay."""
    heavy = '"&<' * 260  # 780 characters, about 4000 once escaped
    wait = "Biggest wait: " + heavy
    recs = tuple(f"{n}. Recommendation {n} " + heavy[:300] for n in (1, 2, 3))
    event = summary_event(lines=(wait, "Needs a human: 2 open condition(s).", *recs), link=LINK)
    html_text = summary.telegram_html(event, EN)
    assert utf16_len(html_text) <= MAX_TEXT
    assert "more" not in html_text.replace("Recommendation", "")  # no "and N more"
    for n in (1, 2, 3):
        assert f"\n{n}. Recommendation {n} " in html_text
    assert "Needs a human: 2 open condition(s).\n" in html_text
    assert "…" in html_text  # the long ones were cut instead
    assert html_text.endswith("Open report</a>")
    # a line is cut no further than MIN_LINE while lines are still dropped only after that
    many = summary_event(lines=("&" * 800,) * 10, link=None)
    shrunk = summary.telegram_html(many, EN)
    assert utf16_len(shrunk) <= MAX_TEXT and "more" not in shrunk
    assert len([x for x in shrunk.splitlines() if x.startswith("&amp;")]) == 10
    # when even the shortest lines do not fit, the last ones are counted
    too_many = summary_event(headline="&" * 800, title="&" * 400, lines=("&" * 800,) * 10)
    dropped = summary.telegram_html(too_many, EN)
    assert utf16_len(dropped) <= MAX_TEXT


def test_the_urls_of_alerts_and_summaries_share_one_check() -> None:
    template = "https://board.example.org/tasks/{id}"
    assert check_task_url(template) == template
    for bad, why in (
        ("https://u:p@board.example.org/{id}", "credentials"),
        ("ftp://board.example.org/{id}", "http"),
        ('https://board.example.org/{id}"x', "plain URL"),
        ("https://board.example.org/" + "x" * 700 + "{id}", "plain URL"),
        ("https://{id}.example.org/", "host"),
        ("https://board.example.org/", "{id}"),
    ):
        with pytest.raises(ValueError, match=why):
            check_task_url(bad)


def test_empty_or_invisible_input_still_renders_a_message() -> None:
    event = summary_event(headline="​", lines=("", "‮", "x"), title="​", link=None)
    assert summary.plain(event, EN) == "\U0001f7e1 Kelpie\n\nx"
    assert summary.slack_payload(event, EN)["blocks"][0]["elements"][0]["elements"][-1] == {
        "type": "text",
        "text": "Kelpie",
        "style": {"bold": True},
    }


# --- through the delivery layer ------------------------------------------------------------


class Recorder:
    def __init__(self, name: str, level: Severity = Severity.INFO) -> None:
        self.name = name
        self.min_severity = level
        self.sent: list[AlertEvent] = []
        self.errors: list[Exception] = []

    def send(self, message: AlertEvent) -> None:
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append(message)


def wait_for(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.005)


def test_send_summary_routes_like_send() -> None:
    a, b = Recorder("a", Severity.WARNING), Recorder("b", Severity.CRITICAL)
    delivery = Delivery([a, b], backoff=0.001)
    delivery.send_summary("h0", ["x"], "info")  # below both filters: nobody
    delivery.send_summary("h1", ["x", "y"], "warning", link=LINK, title=TITLE)  # a only
    delivery.send_summary("h2", [], "info", ["b"])  # named: the filter is not applied
    delivery.send_summary("h3", ["x"], "info", ["nope"])  # unknown: skipped, no raise
    delivery.send_summary("h4", ["x"], "critical", [])  # an empty list is no channel
    delivery.send_summary("h5", ["x"], "bogus-level")  # unknown severity counts as warning
    wait_for(lambda: len(a.sent) == 2 and len(b.sent) == 1)
    delivery.close()
    assert [m.headline for m in a.sent] == ["h1", "h5"]
    assert [m.headline for m in b.sent] == ["h2"]
    first = a.sent[0]
    assert first.summary == Summary(("x", "y"), LINK, TITLE) and first.severity is Severity.WARNING
    assert (first.rule, first.subject, first.text) == (SUMMARY_RULE, SUMMARY_SUBJECT, None)
    assert a.sent[1].severity is Severity.WARNING


def test_an_identical_summary_in_the_window_is_sent_once_a_changed_one_is_not_dropped() -> None:
    now = [0.0]
    a = Recorder("a")
    metrics = Metrics()
    delivery = Delivery(
        [a], metrics, window=timedelta(minutes=10), clock=lambda: now[0], backoff=0.001
    )
    for _ in range(2):
        delivery.send_summary("h", ["1. x"], "warning", link=LINK)
    delivery.send_summary("h", ["1. x"], "warning", link=None)  # another link: another summary
    delivery.send_summary("h", ["1. y"], "warning", link=None)
    now[0] = 11 * 60
    delivery.send_summary("h", ["1. y"], "warning", link=None)  # the window has passed
    delivery.send("h", "warning")  # free text with the same words is not the same alert
    wait_for(lambda: len(a.sent) == 5)
    delivery.close()
    assert metrics.alerts_dropped[("a", "duplicate")] == 1


def test_the_same_summary_of_another_day_is_not_a_duplicate() -> None:
    """Same headline and items, another title (another day), inside the dedup window."""
    now = [0.0]
    a = Recorder("a")
    metrics = Metrics()
    delivery = Delivery(
        [a], metrics, window=timedelta(minutes=10), clock=lambda: now[0], backoff=0.001
    )
    delivery.send_summary(HEADLINE, ITEMS, "warning", title="Kelpie daily report 2026-03-02")
    now[0] = 60.0
    delivery.send_summary(HEADLINE, ITEMS, "warning", title="Kelpie daily report 2026-03-03")
    wait_for(lambda: len(a.sent) == 2)
    delivery.send_summary(HEADLINE, ITEMS, "warning", title="Kelpie daily report 2026-03-03")
    delivery.close()  # the true repeat is dropped
    assert [m.summary.title for m in a.sent if m.summary] == [
        "Kelpie daily report 2026-03-02",
        "Kelpie daily report 2026-03-03",
    ]
    assert metrics.alerts_dropped[("a", "duplicate")] == 1


def test_a_summary_is_retried_and_counted_like_any_alert() -> None:
    a = Recorder("a")
    a.errors = [TransientError("server_error"), TransientError("server_error")]
    metrics = Metrics()
    delivery = Delivery([a], metrics, backoff=0.001)
    delivery.send_summary("h", ["1. x"], "warning")
    wait_for(lambda: len(a.sent) == 1)
    delivery.close()
    assert metrics.alerts_sent["a"] == 1


def test_a_summary_is_delivered_by_close_and_dropped_after_it() -> None:
    a = Recorder("a")
    metrics = Metrics()
    delivery = Delivery([a], metrics, backoff=0.001)
    delivery.send_summary("queued", ["x"], "warning")
    delivery.close()
    assert [m.headline for m in a.sent] == ["queued"]
    delivery.send_summary("late", ["x"], "warning")
    assert metrics.alerts_dropped[("a", "shutdown")] == 1 and len(a.sent) == 1


def test_send_summary_never_raises() -> None:
    delivery = Delivery([Recorder("a")], backoff=0.001)
    delivery.send_summary("h", None, "warning")  # type: ignore[arg-type]
    delivery.close()


# --- the two channels ----------------------------------------------------------------------

TG_ENV = "KELPIE_TEST_TELEGRAM_TOKEN"
TG_TOKEN = "123456:ABCdef_SECRET-token-0123456789abc"
SLACK_ENV = "KELPIE_TEST_SLACK_WEBHOOK"
SLACK_PATH = "/services/T0SECRETTEAM/B0SECRETBOT/XSECRETtokenValue0123456789"


def test_telegram_sends_the_summary_as_html_or_plain() -> None:
    entry = {"type": "telegram", "token_env": TG_ENV, "chat_id": "-100123"}
    with fake_telegram() as fake:
        html_channel = load_telegram({**entry, "api_url": fake.url}, "t").build(
            {TG_ENV: TG_TOKEN}, RU
        )
        plain_channel = load_telegram({**entry, "api_url": fake.url, "format": "plain"}, "t").build(
            {TG_ENV: TG_TOKEN}
        )
        delivery = Delivery([html_channel], backoff=0.001)
        delivery.send_summary(HEADLINE, ITEMS, "warning", link=LINK, title=TITLE)
        delivery.close()
        plain_channel.send(summary_event())
    (_, _, html_body), (_, _, plain_body) = fake.requests
    assert html_body["parse_mode"] == "HTML" and html_body["disable_web_page_preview"] is True
    assert html_body["text"] == summary.telegram_html(summary_event(), RU)
    assert "Открыть отчёт</a>" in html_body["text"]
    assert "parse_mode" not in plain_body
    assert plain_body["text"] == summary.plain(summary_event(), EN)


def test_slack_sends_the_summary_as_blocks_with_a_link_element() -> None:
    with fake_slack() as fake:
        channel = load_slack(
            {"type": "slack", "webhook_env": SLACK_ENV, "allow_host": "127.0.0.1"}, "t"
        ).build({SLACK_ENV: fake.origin + SLACK_PATH}, RU)
        delivery = Delivery([channel], backoff=0.001)
        delivery.send_summary(HEADLINE, ITEMS, "warning", link=LINK, title=TITLE)
        delivery.close()
    ((path, _, body),) = fake.requests
    assert path == SLACK_PATH
    assert body["blocks"] == summary.slack_payload(summary_event(), RU)["blocks"]
    assert body["mrkdwn"] is False and body["unfurl_links"] is False
    assert body["blocks"][-1]["elements"][0]["elements"] == [
        {"type": "link", "url": LINK, "text": "Открыть отчёт"}
    ]
