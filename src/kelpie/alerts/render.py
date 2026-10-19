"""How an alert reads: one neutral layout, rendered as plain text, Telegram HTML or Slack
Block Kit.

:func:`layout` turns an :class:`~kelpie.core.alerts.AlertEvent` and the
:class:`RenderSettings` (language, task link template) into a :class:`Layout`::

    🟡 <headline> · <transition>

    1. <title> — 44 h · t_6fb7a27a
       Needed: <first sentence or clause, at most 140 characters>
       Blocking: none · Open task
    2. …
    Snoozed: <title> until 2026-10-25 (<reason>, by <who>) · t_c3f82477
    Why: <the reason, without the sentence the headline already quotes>
    Kelpie did: …
    Rule: stale_blocks

The headline is the rule's own (``params.headline``), the catalog's line for a digest, or
else the reason's first sentence. The reason is never dropped: it stands under the headline
(labelled "Why" under a list of tasks), up to 1000 characters, unless every task says what
it needs under a headline that is not the rule's own. The fixed words come from
:mod:`kelpie.alerts.i18n`; everything else (titles, ids, what is needed, the reason) is the
rules' and agents' text, untrusted. The layout holds it
cleaned (no control or invisible format characters, @mentions broken, see :func:`defang`)
but not escaped: each renderer escapes every dynamic value for its own format and cuts
text before escaping it, so a cut never lands inside an entity or a tag. Telegram gets
HTML; Slack gets ``rich_text`` blocks, whose text elements Slack shows as they are (no
mrkdwn, no mentions, no links), with Kelpie's own emphasis as element styles. Nothing is
added to the text (no zero-width characters): ids, commands and URLs copy as they are.
Links are built only from the configured ``task_url`` (http(s), validated at load) with
the task id URL-quoted.

**Snoozes** (#176). Tasks a person snoozed that the alert leaves out
(``AlertEvent.snoozed``) are listed in a footer under the tasks, one line each (at most
:data:`MAX_SNOOZED`, the rest counted); a task back from a snooze (expired, or more work
blocked behind it) is marked under its item. A snooze's end is shown in
``RenderSettings.tz`` (``[report] timezone``): one that ends at midnight there as the last
day it covered ("through 2026-10-25", "Snooze ran through 2026-10-25"), the way ``kelpie
snooze --until 2026-10-25`` takes a date; else to the minute, with the zone ("until
2026-10-25 09:00 UTC"). "and N more snoozed" counts from ``AlertEvent.snoozed_total``. A
marker snooze always says "from marker".

The renderers return what a channel sends, not just a string: :func:`telegram_payload` the
``sendMessage`` fields (``text``, ``parse_mode``), :func:`slack_payload` the plain ``text``
fallback and the ``blocks`` list, one block per item, so per-item controls can be added
next to an item later.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from urllib.parse import quote, urlsplit

from kelpie.alerts import i18n
from kelpie.core.alerts import AlertEvent, AlertItem, Did, SnoozeNote, SnoozeState, Transition
from kelpie.core.snooze import snooze_end

MAX_TEXT = 4096  # Telegram's limit, in UTF-16 units
AT_SIGN = "\uff20"  # fullwidth commercial at: shown like "@", never a mention
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

MIN_AGE = timedelta(minutes=1)  # an item younger than this shows no age ("0 min" is noise)
MAX_ITEMS = 10  # items shown; the rest are counted ("and 3 more")
MAX_NOTE = 140  # characters of "Needed" and of a headline taken from the reason
MAX_HEADLINE = 100  # a rule's own headline (kelpie.rules._alerts)
MAX_TITLE = 120
MAX_ID = 100
MAX_REASON = 1000  # characters of the reason shown
MAX_DID = 1500
MAX_URL = 600
MAX_RULE = 64
MAX_SNOOZED = 5  # snoozed tasks listed in the footer; the rest are counted
MAX_BY = 60  # characters of who set a snooze
OWN_RULE = "kelpie"  # the engine's own notices: no "Rule:" line
SLACK_BLOCK = 3000  # characters of text in one Slack block, kept below Slack's limits
SLACK_BLOCKS = 50  # Slack's limit of blocks in a message
SLACK_TEXT = 4000  # the top-level fallback text
INDENT = "   "
_SPACE = re.compile(r"\s+")
# A sentence ends at . ! ? or …, with the closing quotes and brackets right after it.
_SENTENCE = re.compile(r"[.!?…][\"'»”’)\]]*(?=\s|$)")  # noqa: RUF001 (closing quotes)
_CLAUSE = re.compile("; | \u2014 | \u2013 ")
# Quotes a cut must not end inside: opening -> closing.
_QUOTES = {"'": "'", '"': '"', "«": "»", "“": "”", "\u2018": "\u2019", "„": "“"}
# Machine prefixes of a block reason ("waiting_human: needs_input: "): kinds, not words.
_PREFIX = re.compile(r"^(?:[a-z][a-z0-9]*_[a-z0-9_]*:\s*)+")
_URL_BAD = re.compile(r"[\s<>\"'|\\`{}^]")


def clean(text: str) -> str:
    """No lone surrogates (they cannot be encoded), no control characters and no invisible
    format characters (category Cf: bidi overrides, zero-width characters), which could
    hide or reorder what a human reads."""
    text = _CONTROL.sub(" ", text.encode("utf-8", "replace").decode("utf-8"))
    return "".join(c for c in text if unicodedata.category(c) != "Cf")


def defang(text: str) -> str:
    """``clean`` and break @mentions: in a chat, agent-controlled text must not ping people
    (``@everyone``, ``@here``); a fullwidth @ looks the same and notifies nobody."""
    return clean(text).replace("@", AT_SIGN)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


@dataclass(frozen=True, slots=True)
class RenderSettings:
    """``[alerts] language`` and ``[alerts] task_url`` (a link template with ``{id}``);
    ``tz`` is ``[report] timezone``, where a snooze's end is shown."""

    language: str = i18n.DEFAULT
    task_url: str | None = None
    tz: tzinfo = UTC


DEFAULT_RENDERING = RenderSettings()


_URL_PROBLEMS = {
    "plain": "must be a plain URL (no spaces, quotes, <, >, | or braces)",
    "invalid": "is not a valid URL",
    "scheme": "must be an http(s) URL",
    "credentials": "must not carry credentials",
}


def _url_problem(url: str) -> str | None:
    """Why ``url`` is not a plain http(s) address (a key of ``_URL_PROBLEMS``), else None."""
    if _URL_BAD.search(url):
        return "plain"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "invalid"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "scheme"
    if parts.username or parts.password:
        return "credentials"
    return None


def plain_http_url(url: object) -> str | None:
    """``url`` if it is a plain http(s) address: no spaces, quotes, brackets or credentials,
    a host, at most ``MAX_URL`` characters; else None. The one check behind every link
    Kelpie puts into a chat message."""
    if not isinstance(url, str) or not 0 < len(url) <= MAX_URL or _url_problem(url):
        return None
    return url


def check_task_url(value: object, where: str = "[alerts] task_url") -> str:
    """A link template: an http(s) URL containing ``{id}``; a ``ValueError`` names the key."""
    if not isinstance(value, str) or "{id}" not in value:
        raise ValueError(f"{where} must be a string containing {{id}}")
    problem = "plain" if len(value) > MAX_URL else _url_problem(value.replace("{id}", "x"))
    if problem is not None:
        raise ValueError(f"{where} {_URL_PROBLEMS[problem]}")
    if "{id}" in urlsplit(value).netloc:
        raise ValueError(f"{where}: {{id}} belongs in the path or query, not the host")
    return value


# --- the layout ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Link:
    label: str
    url: str


@dataclass(frozen=True, slots=True)
class ItemView:
    """One task as an alert lists it. ``task_id`` is the stable id (for links and, later,
    per-item controls); ``shown_id`` is how it is displayed."""

    number: int
    task_id: str
    shown_id: str
    title: str | None  # None: the id stands in
    age: str | None
    needed: str | None  # the first sentence or clause of what is needed
    blocking: str | None  # "Blocking: none"
    link: Link | None
    snooze: str | None = None  # back from a snooze: "Snooze expired 2026-10-25"


@dataclass(frozen=True, slots=True)
class Layout:
    icon: str
    headline: str
    transition: str | None
    body: str | None  # the reason (what the headline does not already say)
    items: tuple[ItemView, ...]
    hidden: int  # items not shown ("and 3 more")
    did: str | None  # "Kelpie did: …"
    rule: str | None = None  # the rule that raised it, so a human knows which one to tune
    language: str = i18n.DEFAULT
    snoozed: tuple[str, ...] = ()  # the footer: one line per snoozed task (#176)
    snoozed_hidden: int = 0  # snoozed tasks not listed ("and 2 more snoozed")

    @property
    def snoozed_label(self) -> str:
        return i18n.text(self.language, "label.snoozed")

    @property
    def snoozed_more(self) -> str | None:
        if not self.snoozed_hidden:
            return None
        return i18n.text(self.language, "label.snoozed_more", n=self.snoozed_hidden)

    def footer(self) -> list[str]:
        """The footer's lines, unescaped: "Snoozed: …" per task, then the count of the rest."""
        lines = [f"{self.snoozed_label}: {line}" for line in self.snoozed]
        return lines + ([more] if (more := self.snoozed_more) else [])

    @property
    def more(self) -> str | None:
        return i18n.text(self.language, "label.more", n=self.hidden) if self.hidden else None

    @property
    def body_label(self) -> str | None:
        """Under a list of tasks the reason is labelled ("Why: …"); alone it is not."""
        return i18n.text(self.language, "label.why") if self.items or self.hidden else None

    @property
    def needed_label(self) -> str:
        return i18n.text(self.language, "label.needed")

    def without_last_item(self) -> Layout:
        """The same layout with one item fewer shown (counted in ``more``)."""
        return replace(self, items=self.items[:-1], hidden=self.hidden + 1)


def layout(event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING) -> Layout:
    lang = settings.language
    reason = _flat(defang(event.reason))
    headline = _headline(event, lang)
    rest: str = reason
    cut = False
    if headline is None:  # the reason's first sentence, which is then not repeated under it
        first, left = summary(reason, MAX_NOTE)
        headline = first or _flat(defang(event.rule)) or "Kelpie"
        cut = left is None and bool(first)
        rest = reason if left is None else left  # a cut headline: the whole reason is needed
    # What is needed stands in for the reason only when every task says what it needs, in
    # full, the task link leads to the rest, and the headline is not a cut of the reason.
    # Under a rule's own headline the reason is the rule's account, which no task holds.
    replaced = (
        bool(event.items)
        and not cut
        and not (event.headline and _flat(defang(event.headline)))
        and settings.task_url is not None
        and all(i.needed and not _shortened(i.needed) for i in event.items)
    )
    body = _cut_words(rest, MAX_REASON) if rest and not replaced else None
    items = tuple(_item(n, item, settings) for n, item in enumerate(event.items[:MAX_ITEMS], 1))
    rule = _cut_words(_flat(defang(event.rule)), MAX_RULE)
    snoozed = tuple(_snoozed(i, settings) for i in event.snoozed[:MAX_SNOOZED])
    return Layout(
        icon=_icon(event),
        headline=headline,
        transition=_transition(event, lang),
        body=body,
        items=items,
        hidden=len(event.items) - len(items),
        did=_did(event, lang),
        rule=rule if rule and event.rule != OWN_RULE else None,
        language=lang,
        snoozed=snoozed,
        snoozed_hidden=max(event.snoozed_total or 0, len(event.snoozed)) - len(snoozed),
    )


def _icon(event: AlertEvent) -> str:
    if event.transition is Transition.RESOLVED:
        return i18n.RESOLVED_ICON
    return i18n.ICONS[event.severity]


def _transition(event: AlertEvent, lang: str) -> str | None:
    match event.transition:
        case Transition.ESCALATED | Transition.RESOLVED:
            name = "escalated" if event.transition is Transition.ESCALATED else "resolved"
            if event.age is None:
                return i18n.text(lang, f"transition.{name}.bare")
            return i18n.text(lang, f"transition.{name}", age=i18n.age(lang, event.age))
        case Transition.CHANGED:
            return i18n.text(lang, "transition.changed")
        case _:
            return None


def _headline(event: AlertEvent, lang: str) -> str | None:
    """The rule's own headline; else, for a digest (an alert listing several tasks, or one
    of a rule with a ``.digest`` line), the catalog's line; else None: the first sentence
    of the reason stands in. One rule raises alerts of several kinds (a stale block, an
    overdue wait), so its catalog line could misname a single alert; and effects
    (suggested, done, failed) say what was done in the reason's own words."""
    own = _flat(defang(event.headline)) if event.headline else ""
    if own:  # made only of invisible characters, it is no headline
        return _cut_outside_quotes(own, MAX_HEADLINE)
    key = f"headline.{event.rule}"
    digest = len(event.items) > 1 or (bool(event.items) and i18n.has(lang, f"{key}.digest"))
    if not (event.did is Did.NOTIFIED and event.rule and digest and i18n.has(lang, key)):
        return None
    oldest = max((i.age for i in event.items if i.age is not None), default=None)
    shown = oldest is not None and oldest >= MIN_AGE
    words = {
        "tasks": i18n.tasks(lang, len(event.items)),
        "oldest": i18n.text(lang, "label.oldest", age=i18n.age(lang, oldest)) if shown else "",
    }
    if i18n.has(lang, f"{key}.digest"):
        return _flat(i18n.text(lang, f"{key}.digest", **words))
    return _flat(i18n.text(lang, "headline.digest", headline=i18n.text(lang, key), **words))


def _item(number: int, item: AlertItem, settings: RenderSettings) -> ItemView:
    lang = settings.language
    task_id = _flat(defang(item.task_id))
    title = _flat(defang(item.title)) if item.title else None
    blocking = None
    if item.blocking is not None:
        if item.blocking == 0:
            count = i18n.text(lang, "label.none")
        else:
            count = i18n.tasks(lang, item.blocking)
        blocking = f"{i18n.text(lang, 'label.blocking')}: {count}"
    link = None
    if settings.task_url:
        url = settings.task_url.replace("{id}", quote(item.task_id, safe=""))
        if len(url) <= MAX_URL:
            link = Link(i18n.text(lang, "label.open_task"), url)
    return ItemView(
        number=number,
        task_id=item.task_id,
        shown_id=_cut_words(task_id, MAX_ID) or "?",
        title=_cut_words(title, MAX_TITLE) if title else None,
        age=i18n.age(lang, item.age) if item.age is not None and item.age >= MIN_AGE else None,
        needed=_needed(item, title),
        blocking=blocking,
        link=link,
        snooze=_snooze_mark(item.snooze, settings),
    )


def snooze_when(at: datetime, tz: tzinfo = UTC) -> str:
    """A snooze's end as a person set it (:func:`kelpie.core.snooze.snooze_end`): at
    midnight in ``tz``, the last day it covered; else to the minute, with the zone."""
    return snooze_end(at, tz)[0]


def _snooze_span(lang: str, at: datetime, tz: tzinfo, key: str = "snooze") -> str:
    """ "through 2026-10-25" for a whole day, "until 2026-10-25 09:00 UTC" otherwise (with
    ``key`` ``snooze.expired``: "Snooze ran through …", "Snooze ended …")."""
    when, through_day = snooze_end(at, tz)
    return i18n.text(lang, f"{key}.{'through' if through_day else 'until'}", when=when)


def _snooze_details(note: SnoozeNote, lang: str) -> str:
    """ " (reason, by who)", " (from marker)", or nothing."""
    if note.marker:
        parts = [i18n.text(lang, "snooze.from_marker")]
    else:
        reason = note_text(defang(note.reason)) if note.reason else ""
        who = one_line(note.by, MAX_BY) if note.by else ""
        parts = [reason] if reason else []
        parts += [i18n.text(lang, "snooze.by", who=who)] if who else []
    return f" ({', '.join(parts)})" if parts else ""


def _snoozed(item: AlertItem, settings: RenderSettings) -> str:
    """A footer line, without its label: "<title> until 2026-10-25 (<reason>, by <who>) ·
    <id>"."""
    assert item.snooze is not None  # the engine keeps only entries with a snooze
    lang = settings.language
    title = _cut_words(_flat(defang(item.title)), MAX_TITLE) if item.title else ""
    shown = _cut_words(_flat(defang(item.task_id)), MAX_ID) or "?"
    until = _snooze_span(lang, item.snooze.until, settings.tz)
    details = _snooze_details(item.snooze, lang)
    return f"{title} {until}{details} · {shown}" if title else f"{shown} {until}{details}"


def _snooze_mark(note: SnoozeNote | None, settings: RenderSettings) -> str | None:
    """Why a snoozed task is back: its snooze expired, or more work is blocked behind it."""
    if note is None or note.state is SnoozeState.HELD:
        return None
    lang = settings.language
    if note.state is SnoozeState.EXPIRED:
        mark = _snooze_span(lang, note.until, settings.tz, "snooze.expired")
    else:
        grown = i18n.text(lang, f"snooze.grown.{i18n.plural(lang, note.grown)}", n=note.grown)
        mark = f"{grown[0].upper()}{grown[1:]} ({_snooze_span(lang, note.until, settings.tz)})"
    if note.marker:
        mark += f" ({i18n.text(lang, 'snooze.from_marker')})"
    return mark


def _needed(item: AlertItem, title: str | None) -> str | None:
    """What is needed, unless it only repeats the task's title."""
    if not item.needed:
        return None
    note = note_text(defang(item.needed))
    if not note or (title is not None and note.casefold() == title.casefold()):
        return None
    return note


def _did(event: AlertEvent, lang: str) -> str | None:
    """Only when Kelpie acted, would act, or failed: never for "only told a human"."""
    own = _flat(defang(event.did_text)) if event.did_text else ""
    if own:
        what = _cut_words(own, MAX_DID)
    elif event.did is not Did.NOTIFIED:
        what = i18n.text(lang, f"did.{event.did.value}")
    else:
        return None
    return f"{i18n.text(lang, 'label.kelpie_did')}: {what}"


# --- short text ----------------------------------------------------------------------------


def one_line(text: str, limit: int) -> str:
    """Untrusted ``text`` as one clean line: no control or invisible characters, @mentions
    broken (:func:`defang`), whitespace collapsed, cut at a word boundary to ``limit``."""
    return _cut_words(_flat(defang(text)), limit)


def _flat(text: str) -> str:
    return _SPACE.sub(" ", clean(text)).strip()


def _cut_words(text: str, limit: int) -> str:
    """At most ``limit`` characters, cut at a word boundary with "…" when cut."""
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    space = head.rfind(" ")
    if space >= limit // 2:
        head = head[:space]
    return head.rstrip(" ,;:\u2014\u2013-") + "…"


def summary(
    text: str, limit: int = MAX_NOTE, *, clauses: bool = True, close_quotes: bool = False
) -> tuple[str, str | None]:
    """The first sentence of ``text`` (else, with ``clauses``, its first clause, else a cut
    at a word boundary) in at most ``limit`` characters, and the rest after a whole first
    sentence (None when the short form is not a whole sentence, so the full text is still
    needed). A sentence ends with the closing quotes and brackets right after its stop, but
    only where the text goes on with a new sentence (see :func:`_ends_sentence`). A
    sentence, clause or cut never ends inside an open quote ('…', "…", «…», “…”): a cut
    stops before it, or, with ``close_quotes``, closes it when the quote opens early enough
    (:func:`_cut_outside_quotes`)."""
    text = _flat(text)
    if not text:
        return "", ""
    ends = (
        m.end()
        for m in _SENTENCE.finditer(text)
        if _ends_sentence(text, m) and _quote_open(text[: m.end()]) is None
    )
    sentence = text[: next(ends, len(text))]
    if len(sentence) <= limit:
        return sentence, text[len(sentence) :].strip()
    for m in _CLAUSE.finditer(sentence) if clauses else ():
        if _quote_open(sentence[: m.start()]) is None:
            clause = sentence[: m.start()].rstrip(" ,:")
            if 15 <= len(clause) <= limit:
                return clause, None
            break
    return _cut_outside_quotes(sentence, limit, close_quotes), None


def _ends_sentence(text: str, m: re.Match[str]) -> bool:
    """A stop followed by closing quotes or brackets ends the sentence only at the end of
    the text or before a capital letter (or a letter without case: CJK and the like).
    Before anything else (a lowercase word, a digit, a dash, an opening quote or bracket)
    the sentence goes on: on 'needs_input: which region?' and has no condition…; called
    "make test." 40 times; 'no.' — then left."""
    if m.end() - m.start() == 1:
        return True  # a bare stop
    nxt = text[m.end() :].lstrip()[:1]
    return not nxt or (nxt.isalpha() and not nxt.islower())


def _open_quotes(text: str) -> list[tuple[str, int]]:
    """The quotes still open at the end of ``text``, outermost first: (closer, start). A
    straight quote opens only at the start of a word and closes only at its end, so an
    apostrophe (don't) is not a quote."""
    stack: list[tuple[str, int]] = []
    for i, c in enumerate(text):
        before = text[i - 1] if i else " "
        after = text[i + 1] if i + 1 < len(text) else " "
        if stack and c == stack[-1][0] and (c not in "'\"" or not after.isalnum()):
            stack.pop()
        elif c in _QUOTES and (c not in "'\"" or (not before.isalnum() and not after.isspace())):
            stack.append((_QUOTES[c], i))
    return stack


def _quote_open(text: str) -> int | None:
    """Where the outermost quote still open at the end of ``text`` starts, if one is."""
    opened = _open_quotes(text)
    return opened[0][1] if opened else None


QUOTE_ROOM = 30  # a quote opening this far before the limit is cut inside and closed


def _cut_outside_quotes(text: str, limit: int, close: bool = False) -> str:
    """``_cut_words``, never ending inside an open quote: the cut moves back to before it,
    unless that would leave less than 20 characters. With ``close`` (the "Needed" line,
    where the quote is often the ask itself), a quote that opens at least ``QUOTE_ROOM``
    characters before the limit is cut inside at a word boundary and closed instead
    ("«Please enable R2 through…»"), as is one that opens too early to cut before."""
    if len(text) <= limit:
        return text
    short = _cut_words(text, limit)
    opened = _open_quotes(short[:-1])  # the cut ends in "…"
    if not opened:
        return short
    start = opened[0][1]
    head = text[:start].rstrip(" ,;:")
    if (close and start <= limit - QUOTE_ROOM) or len(head) < 20:
        inner = _cut_words(text, limit - len(opened))
        return inner + "".join(closer for closer, _ in reversed(_open_quotes(inner[:-1])))
    return head + "…"


def _shortened(needed: str) -> bool:
    """Whether the "Needed" line shows less than the text (beyond its machine prefixes)."""
    flat = _flat(needed)
    return note_text(needed) != (_PREFIX.sub("", flat) or flat)


def note_text(text: str) -> str:
    """The "Needed" line: the first sentence, at most 140 characters (else cut at a word
    boundary: a clause such as "CEO: backup-d1 (run 1001)" before " — " would drop the
    actual ask), without a block reason's machine prefixes (``waiting_human: needs_input:``)."""
    flat = _flat(text)
    stripped = _PREFIX.sub("", flat)
    return summary(stripped or flat, MAX_NOTE, clauses=False, close_quotes=True)[0]


# --- renderers -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Style:
    esc: Callable[[str], str]
    bold: Callable[[str], str]
    code: Callable[[str], str]
    link: Callable[[str, str], str]  # (escaped label, raw url)
    measure: Callable[[str], int]


def _same(text: str) -> str:
    return text


def _html(text: str) -> str:
    return html.escape(text, quote=True)


def slack_escape(text: str) -> str:
    """Slack's required escaping; ``&`` first so the entities are not escaped again."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PLAIN = Style(_same, _same, _same, lambda label, url: f"{label}: {url}", utf16_len)
TELEGRAM_HTML = Style(
    _html,
    lambda t: f"<b>{t}</b>",
    lambda t: f"<code>{t}</code>",
    lambda label, url: f'<a href="{_html(url)}">{label}</a>',
    utf16_len,
)
# Slack's fallback text: plain (sent with ``mrkdwn: false``), only Slack's required escaping.
SLACK_PLAIN = Style(
    slack_escape, _same, _same, lambda label, url: f"{label}: {slack_escape(url)}", len
)

# Output budgets per field (after escaping), so that the headline, one item and the did
# line always fit a message together; the rest of the room goes to the body or more items.
_BUDGET = {"headline": 400, "title": 400, "id": 200, "note": 500, "did": 1600}
_BUDGET["snooze"] = 300  # a footer line or the mark on a task back from a snooze
MIN_BODY = 300  # a list gives up its last tasks before the reason gets less room than this


def fit(text: str, limit: int, style: Style) -> str:
    """``text`` escaped for ``style`` in at most ``limit`` units; cut (with "…") before
    escaping, so the cut never splits an entity."""
    out = style.esc(text)
    if style.measure(out) <= limit:
        return out
    kept: list[str] = []
    used = style.measure("…")
    for char in text:
        used += style.measure(style.esc(char))
        if used > limit:
            break
        kept.append(char)
    head = "".join(kept)
    space = head.rfind(" ")
    if space >= len(head) // 2:
        head = head[:space]
    return style.esc(head.rstrip()) + "…"


def _head_line(view: Layout, style: Style) -> str:
    line = f"{view.icon} {style.bold(fit(view.headline, _BUDGET['headline'], style))}"
    if view.transition:
        line += f" · {style.esc(view.transition)}"
    return line


def _item_lines(item: ItemView, style: Style, needed_label: str) -> list[str]:
    shown = style.code(fit(item.shown_id, _BUDGET["id"], style))
    if item.title:
        first = f"{item.number}. {style.bold(fit(item.title, _BUDGET['title'], style))}"
        first += f" — {style.esc(item.age)} · {shown}" if item.age else f" · {shown}"
    else:
        first = f"{item.number}. {shown}" + (f" — {style.esc(item.age)}" if item.age else "")
    lines = [first]
    if item.needed:
        note = fit(item.needed, _BUDGET["note"], style)
        lines.append(f"{INDENT}{style.esc(needed_label)}: {note}")
    if item.snooze:
        lines.append(f"{INDENT}{fit(item.snooze, _BUDGET['snooze'], style)}")
    parts = [style.esc(item.blocking)] if item.blocking else []
    if item.link is not None:
        parts.append(style.link(style.esc(item.link.label), item.link.url))
    if parts:
        lines.append(INDENT + " · ".join(parts))
    return lines


def _tail(view: Layout, style: Style) -> list[str]:
    """What Kelpie did, and the rule."""
    lines = [fit(view.did, _BUDGET["did"], style)] if view.did else []
    if view.rule:
        label = style.esc(i18n.text(view.language, "label.rule"))
        lines.append(f"{label}: {style.code(fit(view.rule, MAX_RULE * 3, style))}")
    return lines


def _body(view: Layout, style: Style, room: int) -> str:
    """The reason in ``room`` units, labelled under a list of tasks."""
    assert view.body is not None
    label = f"{style.esc(view.body_label)}: " if view.body_label else ""
    return label + fit(view.body, max(room - style.measure(label), 1), style)


def text(view: Layout, style: Style = PLAIN, limit: int = MAX_TEXT) -> str:
    """The layout as one text in ``style``, at most ``limit`` units. The reason yields the
    room the other parts take; tasks that would leave it less than ``MIN_BODY`` (or that do
    not fit at all) are counted in "and N more", the first one last."""
    while True:
        out, roomy = _text(view, style, limit)
        fits = style.measure(out) <= limit
        if not view.items or (fits and (roomy or len(view.items) == 1)):
            return out
        view = view.without_last_item()


def _text(view: Layout, style: Style, limit: int) -> tuple[str, bool]:
    """The text, and whether the reason (if any) got the room it needs."""
    lines = [_head_line(view, style), ""]
    for item in view.items:
        lines += _item_lines(item, style, view.needed_label)
    if view.more:
        lines.append(style.esc(view.more))
    lines += [fit(line, _BUDGET["snooze"], style) for line in view.footer()]
    tail = _tail(view, style)
    roomy = True
    if view.body:
        room = limit - style.measure("\n".join(lines + tail)) - 1
        roomy = room >= min(style.measure(style.esc(view.body)) + 10, MIN_BODY)
        lines.append(_body(view, style, room))
    if len(lines) == 2:
        lines.pop()  # no blank line under a headline alone
    return "\n".join(lines + tail), roomy


def plain(event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING) -> str:
    """The alert as plain text (Telegram without formatting, logs, the demo)."""
    return text(layout(event, settings), PLAIN)


def telegram_html(event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING) -> str:
    return text(layout(event, settings), TELEGRAM_HTML)


def telegram_payload(
    event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING, *, html_format: bool = True
) -> dict[str, Any]:
    """The message fields of a ``sendMessage`` request (the channel adds the chat and the
    delivery options)."""
    if html_format:
        return {"text": telegram_html(event, settings), "parse_mode": "HTML"}
    return {"text": plain(event, settings)}


def el(text: str, **style: bool) -> dict[str, Any]:
    """A rich_text text element: Slack shows its text as it is."""
    element: dict[str, Any] = {"type": "text", "text": text}
    if style:
        element["style"] = style
    return element


def rich(elements: list[dict[str, Any]]) -> dict[str, Any]:
    """One rich_text block; an empty text element is never sent (Slack refuses it)."""
    kept = [e for e in elements if e["type"] != "text" or e["text"]]
    return {"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": kept}]}


def _item_elements(item: ItemView, needed_label: str) -> list[dict[str, Any]]:
    out = [el(f"{item.number}. ")]
    if item.title:
        out += [el(item.title, bold=True), el(f" — {item.age} · " if item.age else " · ")]
        out.append(el(item.shown_id, code=True))
    else:
        out.append(el(item.shown_id, code=True))
        if item.age:
            out.append(el(f" — {item.age}"))
    if item.needed:
        out += [el(f"\n{INDENT}{needed_label}: "), el(item.needed)]
    if item.snooze:
        out.append(el(f"\n{INDENT}{item.snooze}"))
    if item.blocking or item.link is not None:
        out.append(el(f"\n{INDENT}"))
        if item.blocking:
            out.append(el(item.blocking))
        if item.link is not None:
            if item.blocking:
                out.append(el(" · "))
            out.append({"type": "link", "url": item.link.url, "text": item.link.label})
    return out


def slack_blocks(view: Layout) -> list[dict[str, Any]]:
    """Block Kit ``rich_text``: the headline, one block per item, the reason, then what
    Kelpie did and the rule. Every value is a text element (never parsed); Kelpie's own
    emphasis is a style (bold headline and titles, code for ids and the rule). The layout
    already capped every field, so a block stays below ``SLACK_BLOCK`` characters and a
    message below ``SLACK_BLOCKS`` blocks."""
    head = [el(f"{view.icon} "), el(view.headline, bold=True)]
    if view.transition:
        head.append(el(f" · {view.transition}"))
    blocks = [rich(head)]
    blocks += [rich(_item_elements(i, view.needed_label)) for i in view.items]
    if view.more:
        blocks.append(rich([el(view.more)]))
    if footer := view.footer():
        blocks.append(rich([el("\n".join(line[: _BUDGET["snooze"]] for line in footer))]))
    if view.body:
        label = [el(f"{view.body_label}: ", bold=True)] if view.body_label else []
        blocks.append(rich([*label, el(view.body[:SLACK_BLOCK])]))
    tail: list[dict[str, Any]] = []
    if view.did:
        tail.append(el(view.did))
    if view.rule:
        label = i18n.text(view.language, "label.rule")
        tail += [el(f"{chr(10) if tail else ''}{label}: "), el(view.rule, code=True)]
    if tail:
        blocks.append(rich(tail))
    blocks = [b for b in blocks if b["elements"][0]["elements"]]
    return blocks[:SLACK_BLOCKS]


def slack_payload(
    event: AlertEvent, settings: RenderSettings = DEFAULT_RENDERING
) -> dict[str, Any]:
    """``text`` (the notification and fallback: plain, escaped, ``mrkdwn: false``) and
    ``blocks``."""
    view = layout(event, settings)
    return {
        "text": text(view, SLACK_PLAIN, SLACK_TEXT),
        "mrkdwn": False,
        "blocks": slack_blocks(view),
    }
