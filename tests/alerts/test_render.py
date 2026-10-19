"""Rendered alerts: golden files per case and format, plus the limits and escaping."""

from __future__ import annotations

import html
import json
import unicodedata
from datetime import timedelta
from html.parser import HTMLParser

import pytest

from kelpie.alerts import render
from kelpie.alerts.render import (
    AT_SIGN,
    MAX_TEXT,
    SLACK_BLOCK,
    RenderSettings,
    check_task_url,
    note_text,
    summary,
    utf16_len,
)
from kelpie.core.actions import Severity
from kelpie.core.alerts import AlertEvent, AlertItem, Did, Transition
from tests.alerts.snapshot import assert_snapshot

H = timedelta(hours=1)
URL = "https://board.example.org/tasks/{id}"
EN = RenderSettings(task_url=URL)
RU = RenderSettings("ru", URL)
NO_LINK = RenderSettings()

LONG_NEEDED = (
    "waiting_human: needs_input: CEO: backup-d1 на main (run 38992282776) — 403 10042 "
    "«Please enable R2 through the Cloudflare dashboard before the backup job can write to "
    "the bucket» — the job retried three times, then the agent stopped and asked for a "
    "person with access to the account. После включения R2 нужно перезапустить run, затем "
    "проверить, что архив появился в бакете и что ротация старых копий работает; до этого "
    "задача t_backup не может закрыться, а релиз ждёт её."  # noqa: RUF001 (Russian)
)
ISSUE_REASON = (
    "Task t_6fb7a27a has been blocked for 44h 21m on 'waiting_human: needs_input: CEO: "
    "backup-d1 на main (run 38992282776) — 403 10042 «Please enable R2 through the dashboard "
    "first.» Then rerun the job and tell the agent.' and has no condition Kelpie can check, so "
    "nothing will clear it automatically."
)
STALE_REASON = (
    "Task t_6fb7a27a has been blocked for 44h 21m on 'waiting_human: needs_input: CEO: "
    "backup-d1' and has no condition Kelpie can check, so nothing will clear it "
    "automatically."
)


def event(**kw: object) -> AlertEvent:
    base: dict[str, object] = {
        "rule": "stale_blocks",
        "subject": "alert t_6fb7a27a",
        "reason": STALE_REASON,
        "severity": Severity.WARNING,
    }
    return AlertEvent(**{**base, **kw})  # type: ignore[arg-type]


def single(**kw: object) -> AlertItem:
    base: dict[str, object] = {
        "task_id": "t_6fb7a27a",
        "title": "Nightly backup of d1",
        "age": 44 * H + timedelta(minutes=21),
        "blocking": 0,
    }
    return AlertItem(**{**base, **kw})  # type: ignore[arg-type]


DIGEST = event(
    rule="needs_you",
    subject="escalate",
    reason="Needs you (sam): 2 item(s) hold 2 agent task(s), 90.0 agent-hours waited so far.",
    items=(
        AlertItem(
            "t_6fb7a27a",
            "Enable R2 for backups",
            44 * H + timedelta(minutes=21),
            "Enable R2 in the Cloudflare dashboard. Then rerun the job.",
            0,
        ),
        AlertItem(
            "t_c3f82477",
            "Approve the store listing",
            46 * H,
            "approval: Approve the new store listing; the release waits for it",
            2,
        ),
    ),
)
HOSTILE = event(
    rule="<b>rule</b>",
    reason=(
        "<b>bold</b> *bold* _it_ ~gone~ ```fence``` &amp; @everyone <!channel> "
        'pay\u202eevil\u202c "quoted" <a href="x">'
    ),
    items=(
        AlertItem(
            't/1 ?&x="y"*_~`',
            'Fix <b>"login"</b> *bold* _it_ ~gone~ `code` & @here <!channel> \u2066x\u2069',
            2 * H,
            "<script>alert(1)</script> ```fence``` *now* @everyone &amp; <http://evil|click>",
            1,
        ),
    ),
    did=Did.SUGGESTED,
    did_text="Suggested: clear the block on <b>t/1</b> *now* @here.",
)
OVERFLOW_ITEMS = event(
    rule="needs_you",
    reason="Needs you.",
    items=tuple(
        AlertItem(f"t_{n:04d}", '"' * 300 + f" task {n}", n * H, "&" * 500, n) for n in range(40)
    ),
)
OVERFLOW_BODY = event(rule="resources", reason='"&<>' * 6000, did=Did.FAILED, did_text="x" * 3000)

CASES: dict[str, tuple[AlertEvent, RenderSettings]] = {
    "single_titled": (event(items=(single(),)), EN),
    "single_untitled": (event(items=(single(title=None, blocking=None),)), EN),
    "single_needed": (event(items=(single(needed="Approve the deploy to production."),)), EN),
    "digest": (DIGEST, EN),
    "digest_no_link": (DIGEST, NO_LINK),
    "digest_ru": (DIGEST, RU),
    "single_ru": (event(items=(single(blocking=5),), did=Did.SUGGESTED), RU),
    "long_mixed_needed": (event(rule="needs_you", items=(single(needed=LONG_NEEDED),)), EN),
    "no_items": (event(rule="resources", reason="Disk host-a is 95% full. It fills in 3h."), EN),
    "no_catalog_entry": (event(rule="custom", reason="Something odd. Look at it soon."), EN),
    "escalated": (event(transition=Transition.ESCALATED, age=44 * H, items=(single(),)), EN),
    "escalated_no_age": (event(transition=Transition.ESCALATED), EN),
    "changed": (event(transition=Transition.CHANGED, severity=Severity.CRITICAL), EN),
    "resolved": (
        event(
            transition=Transition.RESOLVED,
            severity=Severity.INFO,
            age=3 * H,
            reason='task t_6fb7a27a: no longer reported: "blocked".',
        ),
        EN,
    ),
    "resolved_ru": (
        event(transition=Transition.RESOLVED, severity=Severity.INFO, age=3 * H, reason="ok."),
        RU,
    ),
    "did_notified": (event(items=(single(),)), EN),
    "did_suggested": (event(items=(single(),), did=Did.SUGGESTED), EN),
    "did_failed": (event(items=(single(),), did=Did.FAILED), EN),
    "did_not_executed": (event(items=(single(),), did=Did.NOT_EXECUTED), EN),
    "did_done": (event(items=(single(),), did=Did.DONE), EN),
    "did_text": (
        event(
            rule="loop_breaker",
            severity=Severity.CRITICAL,
            reason="Runaway run: session S-1 made 10 identical calls.",
            items=(single(task_id="t_loop", title="Heartbeat", age=None, blocking=None),),
            did_text="Stopped run S-1; the worker exits within ~2 min.",
        ),
        EN,
    ),
    "headline_param": (event(headline="Backups stopped on d1", items=(single(),)), EN),
    "hostile": (HOSTILE, EN),
    "issue_example": (
        event(
            reason=ISSUE_REASON,
            transition=Transition.ESCALATED,
            age=44 * H + timedelta(minutes=24),
            items=(single(title="Nightly backup"),),
        ),
        EN,
    ),
    "overflow_items": (OVERFLOW_ITEMS, EN),
    "overflow_body": (OVERFLOW_BODY, EN),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_rendered_alerts_match_their_snapshots(case: str) -> None:
    alert, settings = CASES[case]
    assert_snapshot(f"{case}.txt", render.plain(alert, settings))
    assert_snapshot(f"{case}.html", render.telegram_html(alert, settings))
    payload = render.slack_payload(alert, settings)
    assert_snapshot(f"{case}.json", json.dumps(payload, indent=2, ensure_ascii=False))


class _Tags(HTMLParser):
    """Checks that Telegram HTML is well formed and uses only the tags the layout writes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.tags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.open.append(tag)
        self.tags.add(tag)
        if tag == "a":
            assert [k for k, _ in attrs] == ["href"]
            assert (dict(attrs)["href"] or "").startswith("https://board.example.org/")

    def handle_endtag(self, tag: str) -> None:
        assert self.open and self.open.pop() == tag


def shown(payload: dict[str, object]) -> str:
    """What a person sees of Slack's rich_text blocks: one line group per block."""
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return "\n".join(
        "".join(e["text"] for section in b["elements"] for e in section["elements"]) for b in blocks
    )


def elements(payload: dict[str, object]) -> list[dict[str, object]]:
    blocks = payload["blocks"]
    assert isinstance(blocks, list)
    return [e for b in blocks for section in b["elements"] for e in section["elements"]]


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_rendering_is_well_formed_and_within_the_limits(case: str) -> None:
    alert, settings = CASES[case]
    html_text = render.telegram_html(alert, settings)
    parser = _Tags()
    parser.feed(html_text)
    parser.close()
    assert parser.open == [] and parser.tags <= {"b", "code", "a"}
    assert utf16_len(html_text) <= MAX_TEXT
    assert utf16_len(render.plain(alert, settings)) <= MAX_TEXT
    payload = render.slack_payload(alert, settings)
    assert len(payload["text"]) <= render.SLACK_TEXT and payload["mrkdwn"] is False
    assert 0 < len(payload["blocks"]) <= render.SLACK_BLOCKS
    for block in payload["blocks"]:
        assert block["type"] == "rich_text"
        (section,) = block["elements"]
        assert section["type"] == "rich_text_section"
        assert sum(len(e["text"]) for e in section["elements"]) <= SLACK_BLOCK
        for e in section["elements"]:
            assert e["type"] in ("text", "link") and e["text"]
            assert set(e.get("style", {})) <= {"bold", "code"}
            if e["type"] == "link":
                assert e["url"].startswith("https://board.example.org/")
    raw = json.dumps(payload, ensure_ascii=False)
    for text in (html_text, render.plain(alert, settings), raw):
        text.encode("utf-8")
        # nothing invisible is added (and agent text had it removed)
        assert all(unicodedata.category(c) != "Cf" for c in text), case


def test_hostile_text_is_escaped_everywhere() -> None:
    html_text = render.telegram_html(HOSTILE, EN)
    assert "<script>" not in html_text and "<!channel>" not in html_text
    assert "&lt;b&gt;&quot;login&quot;&lt;/b&gt;" in html_text
    assert "&amp;amp;" in html_text  # the agent's own "&amp;" is shown, not decoded
    assert "@" not in html_text and "\u202e" not in html_text and "\u2066" not in html_text
    url = "https://board.example.org/tasks/t%2F1%20%3F%26x%3D%22y%22%2A_~%60"
    assert f'href="{url}"' in html_text
    plain = render.plain(HOSTILE, EN)
    assert "@" not in plain and "\u202e" not in plain
    payload = render.slack_payload(HOSTILE, EN)
    # the fallback is plain text with Slack's escaping, sent with mrkdwn off
    assert "<!channel>" not in payload["text"] and "&lt;!channel&gt;" in payload["text"]
    assert payload["mrkdwn"] is False
    # the blocks: agent text only ever sits in text elements, which Slack does not parse
    parts = elements(payload)
    links = [e for e in parts if e["type"] == "link"]
    assert links == [{"type": "link", "url": url, "text": "Open task"}]
    title = f'Fix <b>"login"</b> *bold* _it_ ~gone~ `code` & {AT_SIGN}here <!channel> x'
    assert {"type": "text", "text": title, "style": {"bold": True}} in parts  # as it is
    assert {"type": "text", "text": 't/1 ?&x="y"*_~`', "style": {"code": True}} in parts
    needed = (
        f"<script>alert(1)</script> ```fence``` *now* {AT_SIGN}everyone &amp; <http://evil|click>"
    )
    assert {"type": "text", "text": needed} in parts
    raw = json.dumps(payload, ensure_ascii=False)
    assert "@" not in raw and "\u202e" not in raw
    # plain text and Telegram show the characters as they are
    assert "*bold* _it_ ~gone~ `code`" in plain


def test_ids_commands_and_urls_copy_as_they_are() -> None:
    """Nothing is inserted into text: what a person copies is what the agent wrote."""
    alert = event(
        reason="Blocked on https://ci.example.org/my_org/my_repo/runs/12 for 3h. See my_branch.",
        did=Did.SUGGESTED,
        did_text="Suggested: requeue t_6fb7a27a (hermes kanban unblock t_loop).",
        items=(single(title="Fix flaky_test in *core*"),),
    )
    payload = render.slack_payload(alert, EN)
    seen = shown(payload)
    for text in (render.plain(alert, EN), html.unescape(render.telegram_html(alert, EN)), seen):
        for needle in (
            "t_6fb7a27a",
            "https://ci.example.org/my_org/my_repo/runs/12",
            "hermes kanban unblock t_loop",
            "Fix flaky_test in *core*",
            "my_branch",
        ):
            assert needle in text, (needle, text)
    assert "Rule: stale_blocks" in seen
    assert {"type": "text", "text": "t_6fb7a27a", "style": {"code": True}} in elements(payload)
    assert {"type": "text", "text": "stale_blocks", "style": {"code": True}} in elements(payload)


def test_a_digest_is_capped_and_counts_the_rest() -> None:
    many = event(rule="needs_you", items=tuple(AlertItem(f"t_{n}", f"Task {n}") for n in range(13)))
    text = render.plain(many, NO_LINK)
    assert "10. Task 9 · t_9" in text and "11." not in text
    assert text.splitlines()[-3:] == ["and 3 more", f"Why: {STALE_REASON}", "Rule: needs_you"]
    assert "Waiting for you: 13 tasks" in text


def test_an_overflowing_digest_drops_items_and_stays_valid() -> None:
    html_text = render.telegram_html(OVERFLOW_ITEMS, EN)
    assert utf16_len(html_text) <= MAX_TEXT
    more = [line for line in html_text.splitlines() if line.startswith("and ")]
    assert len(more) == 1 and more[0].endswith(" more")
    assert html_text.splitlines()[-2] == "Why: Needs you."  # every "needed" there was cut
    body = render.plain(OVERFLOW_BODY, EN)
    assert body.splitlines()[-2].startswith("Kelpie did: xxx") and body.endswith("resources")


def test_the_did_line_appears_only_when_kelpie_acted_or_would_act() -> None:
    assert "Kelpie did" not in render.plain(event())
    for did in (Did.SUGGESTED, Did.FAILED, Did.NOT_EXECUTED, Did.DONE):
        assert render.plain(event(did=did)).splitlines()[-2].startswith("Kelpie did: ")
    with_text = render.plain(event(did_text="Stopped run S-1."))
    assert with_text.splitlines()[-2:] == ["Kelpie did: Stopped run S-1.", "Rule: stale_blocks"]


def test_needed_is_the_first_sentence_or_clause_within_140_characters() -> None:
    assert note_text("Enable R2. Then rerun.") == "Enable R2."
    assert note_text(LONG_NEEDED) == "CEO: backup-d1 на main (run 38992282776)"
    assert note_text("waiting_human: Approve it") == "Approve it"
    assert note_text("note: keep this prefix") == "note: keep this prefix"
    cut = note_text("word " * 60)
    assert len(cut) <= 140 and cut.endswith("…") and not cut.endswith(" …")
    assert summary("") == ("", "")
    assert summary("One. Two.") == ("One.", "Two.")


def test_the_headline_falls_back_to_the_first_sentence_of_the_reason() -> None:
    text = render.plain(event(rule="custom", reason="Odd thing. More detail."))
    assert text.splitlines()[:3] == ["🟡 Odd thing.", "", "More detail."]
    assert render.plain(event(rule="custom", reason="")).splitlines() == [
        "🟡 custom",
        "Rule: custom",
    ]
    assert render.plain(event(rule="", reason="")).splitlines() == ["🟡 Kelpie"]
    effect = render.plain(event(did=Did.SUGGESTED, reason="Clear it. It is stale."))
    assert effect.startswith("🟡 Clear it.")  # an effect: not the rule's alert headline


GATE = (
    "Human task t_store ('Store listing', owner alice, todo for 5h) gates 5 agent task(s), "
    "about 12h of agent work (a, b, c, d, e). Do it first, or remove the link if the agent "
    "work does not need it. Kelpie does not change dependencies."
)
COACH = (
    "This session ran the same check 5 times within 10 min, which looks like waiting inside "
    "the session. Please register a `ci` condition and exit: the task will show as waiting "
    "rather than dead, and Kelpie will wake it when the condition holds."
)


@pytest.mark.parametrize("did", [Did.NOTIFIED, Did.SUGGESTED])
def test_the_whole_reason_survives_with_items(did: Did) -> None:
    """Only ``needed`` is cut to a short line; the reason itself is never dropped."""
    for reason, rule in ((GATE, "bottlenecks"), (COACH, "wait_condition")):
        alert = event(rule=rule, reason=reason, did=did, items=(single(),))
        first, rest = summary(reason)
        assert rest is not None
        for text in (
            render.plain(alert, EN),
            html.unescape(render.telegram_html(alert, EN)),
            html.unescape(render.slack_payload(alert, EN)["text"]),
            shown(render.slack_payload(alert, EN)),
        ):
            lines = text.splitlines()
            assert first in lines[0]  # the headline quotes the first sentence...
            assert f"Why: {rest}" in lines  # ...and the rest follows, whole
    # a headline that is a cut of the reason leaves the whole reason under it
    long = "word " * 40 + "end. Next."
    alert = event(rule="custom", reason=long, items=(single(),))
    assert f"Why: {long.strip()}" in render.plain(alert).splitlines()
    # when every task says in full what it needs and links to the rest, that stands in
    needed = event(reason="Short. More.", items=(single(needed="Approve it."),))
    assert "Why:" not in render.plain(needed, EN)
    assert "Needed: Approve it." in render.plain(needed, EN)
    # ...but not without a link to the task, nor when what is needed was cut
    assert "Why:" in render.plain(needed, NO_LINK)
    cut = event(items=(single(needed="Approve it. Then deploy it."),))
    assert "Needed: Approve it." in render.plain(cut, EN) and "Why:" in render.plain(cut, EN)
    prefixed = event(reason="Short.", items=(single(needed="waiting_human: Approve it."),))
    assert "Why:" not in render.plain(prefixed, EN)  # a machine prefix is not a cut


def test_catalog_headlines_are_for_digests_only() -> None:
    """One rule raises alerts of several kinds: a single alert says its own first sentence."""
    one = render.plain(event(items=(single(),))).splitlines()[0]
    assert one.startswith("🟡 Task t_6fb7a27a has been blocked for 44h 21m")
    two = event(items=(single(), single(task_id="t_2")))
    assert render.plain(two).startswith("🟡 Block Kelpie cannot clear: 2 tasks (oldest 44 h)")
    # a rule with a ``.digest`` line uses it for a single task too
    inbox = render.plain(event(rule="needs_you", items=(single(),)))
    assert inbox.startswith("🟡 Waiting for you: 1 task (oldest 44 h)")
    assert render.plain(event(rule="needs_you")).startswith("🟡 Task t_6fb7a27a")  # no items


def test_the_engines_own_notices_carry_no_rule_line() -> None:
    notice = event(rule="kelpie", reason="Action ledger unavailable; actions are held back.")
    assert "Rule:" not in render.plain(notice)
    assert "Rule:" not in shown(render.slack_payload(notice))


def test_an_invisible_headline_or_line_is_no_text_at_all() -> None:
    """Text made only of invisible characters is empty once cleaned: never an empty element."""
    blank = "\u200b\u202e"
    for alert in (
        event(headline=blank, reason="Real reason."),
        event(rule="r", reason=blank),
        event(reason="Real reason.", did=Did.DONE, did_text=blank),
        event(items=(single(title=blank, needed=blank),)),
    ):
        payload = render.slack_payload(alert, EN)
        assert all(e["text"] for e in elements(payload) if e["type"] == "text")
        assert all(b["elements"][0]["elements"] for b in payload["blocks"])
    head = render.plain(event(headline=blank, reason="Real reason.")).splitlines()[0]
    assert head == "\U0001f7e1 Real reason."  # the reason's sentence stands in
    did = render.plain(event(reason="Real reason.", did=Did.DONE, did_text=blank))
    assert "Kelpie did: carried it out." in did  # the fixed wording stands in


def test_a_cut_headline_keeps_the_whole_reason_even_when_every_task_says_what_it_needs() -> None:
    long = "word " * 40 + "end. Next."
    alert = event(rule="custom", reason=long, items=(single(needed="Approve it."),))
    assert render.plain(alert, EN).splitlines()[0].endswith("…")
    assert f"Why: {long.strip()}" in render.plain(alert, EN).splitlines()
    whole = event(rule="custom", reason="Short. More.", items=(single(needed="Approve it."),))
    assert "Why:" not in render.plain(whole, EN)  # a whole sentence: "Needed" stands in


def test_a_cut_never_ends_inside_an_open_quote() -> None:
    headline = render.plain(CASES["issue_example"][0]).splitlines()[0]
    assert headline == (
        "🟡 Task t_6fb7a27a has been blocked for 44h 21m on… · still open after 44 h"
    )
    # a sentence end inside a quote is not the end of the sentence
    first, rest = summary("He said 'stop. now' and left. Then more.")
    assert (first, rest) == ("He said 'stop. now' and left.", "Then more.")
    # an apostrophe is not a quote
    assert summary("Don't merge it. Wait.")[0] == "Don't merge it."
    # a clause boundary inside a quote is not a clause boundary
    long = "The task is blocked on «waiting — " + "x " * 80 + "» and more text."
    assert summary(long)[0] == "The task is blocked on…"


def test_task_url_must_be_an_http_url_with_an_id() -> None:
    assert check_task_url(URL) == URL
    for bad in (
        "https://board.example.org/tasks/",
        "javascript:alert(1)//{id}",
        "ftp://board.example.org/{id}",
        "https://board.example.org/{id} x",
        'https://board.example.org/{id}"',
        "https://user:pw@board.example.org/{id}",
        "https://board.example.org/{id}|x",
        "https://{id}",
        "/relative/{id}",
        5,
    ):
        with pytest.raises(ValueError, match="task_url"):
            check_task_url(bad)
