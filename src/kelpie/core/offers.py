"""An agent's own options for a person's decision, read from its comment (#300).

An agent that needs a person's decision often lists the choices itself::

    Which schema should the migration target?
    Options:
    A) target schema v3
    B) keep v2 and add a shim
    Recommend: B

Rule 13 (``needs_you``) reads such an *offer* from the task owner's latest comment and puts
it on the digest item; a Telegram channel with ``answer_buttons`` then shows a button per
option, and a confirmed press writes one comment on the task saying which of the agent's
own options a person chose (docs/security.md, "Answering an agent's question from the
chat"). Everything here is pure: parsing, the question's key, the re-check before a write
and the comment's text, so the rule, the chat side and the adapter read an offer the same
way.

**The grammar** is strict, so a question without explicit options gets no buttons:

- exactly one line ``Options:`` (any case, nothing else on it), then 2 to 4 consecutive
  lines with labels of one style, in order: ``A)`` … ``D)``, ``A.`` … ``D.``, ``1)`` …
  ``4)`` or ``1.`` … ``4.``; each option at most :data:`MAX_OPTION` characters;
- optionally one line ``Recommend:`` or ``Recommended:`` naming one of those labels;
- refused as a whole (:class:`Refused`, with why): a second ``Options:`` line, fewer than
  2 or more than 4 options, mixed label styles, gaps or duplicates, a ``Recommend:`` naming
  no offered label (or two such lines), an option that is empty, too long, holds a control
  or invisible character, a link, an ``@`` mention or a backtick, or starts with ``/``; and a
  comment that may have been cut (longer than :data:`MAX_OFFER_COMMENT`, or ending in
  Kelpie's cut mark), so a digest is never computed over a cut offer.

**Which comment.** The latest comment on the task by its owner (the agent holding it, else
its assignee; on a multi-host farm the names are compared without their host prefix),
posted no earlier than :data:`OFFER_LEAD` before the block began, with no comment by a
person after it (a person settled it already). A later owner comment without options
withdraws the offer.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import ActionKind
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import CUT_MARK, Answer, Comment, Status, Task

MAX_OPTION = 200  # characters of one option
MAX_OPTIONS = 4
MIN_OPTIONS = 2
# A longer comment may have been cut by an adapter (Hermes keeps 2000 characters of one):
# its offer is refused rather than read short.
MAX_OFFER_COMMENT = 1900
# How long before the block began the offering comment may have been posted (an agent
# often comments, then blocks).
OFFER_LEAD = timedelta(minutes=15)

_HEADER = re.compile(r"\s*options\s*:\s*", re.IGNORECASE)
_HEADER_ANY = re.compile(r"\s*options\s*:", re.IGNORECASE)
_RECOMMEND = re.compile(r"\s*recommend(?:ed)?\s*:\s*(.*?)\s*", re.IGNORECASE)
_LABEL = re.compile(r"\s*([A-D]|[1-4])([).])\s+(.*?)\s*")
_LINK = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://|\bwww\.|\bt\.me/|\bmailto:"
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}/"  # a bare domain with a path
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|org|net|io|dev|app|me|co|info|xyz|ru|example)\b"
)
# Characters that look like "@" or show as nothing though Unicode calls them letters or
# symbols (Hangul fillers, the braille blank): an option must read as what it is.
_LOOKALIKES = frozenset("\uff20\ufe6b\u3164\u115f\u1160\uffa0\u2800")
_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
WORD_JOINER = "\u2060"


@dataclass(frozen=True, slots=True)
class Option:
    label: str  # "A", "B", ... or "1", "2", ...
    text: str  # the agent's words, stripped


@dataclass(frozen=True, slots=True)
class Offer:
    """An offer read from a comment: its options in order and the recommended label."""

    options: tuple[Option, ...]
    style: str  # ")" or "."
    recommended: str | None = None

    @property
    def digest(self) -> str:
        """What identifies the offer's content: any change to an option, its label style or
        the recommendation gives another digest."""
        lines = [f"{o.label}{self.style}\t{o.text}" for o in self.options]
        lines.append(f"recommend\t{self.recommended or ''}")
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:32]

    def option(self, index: int) -> Option | None:
        return self.options[index] if 0 <= index < len(self.options) else None

    def quoted(self, index: int) -> str:
        """An option as the agent wrote it: ``B) keep v2 and add a shim``."""
        o = self.options[index]
        return f"{o.label}{self.style} {o.text}"


@dataclass(frozen=True, slots=True)
class Refused:
    """A comment with an ``Options:`` line whose offer breaks the grammar: no buttons, and
    the item says why."""

    why: str


@dataclass(frozen=True, slots=True)
class Found:
    """The offer on a task: the comment it came from and what was read from it."""

    comment: Comment
    result: Offer | Refused

    @property
    def offer(self) -> Offer | None:
        return self.result if isinstance(self.result, Offer) else None


def parse_offer(text: str) -> Offer | Refused | None:
    """The offer in a comment's ``text``: an :class:`Offer`, :class:`Refused` (with why), or
    None when the comment has no ``Options:`` line (a plain question)."""
    lines = text.splitlines()
    headers = [i for i, line in enumerate(lines) if _HEADER_ANY.match(line)]
    if not headers:
        return None
    if text.endswith(CUT_MARK) or len(text) > MAX_OFFER_COMMENT:
        return Refused("the comment is too long and may have been cut")
    if len(headers) > 1:
        return Refused("a second Options: line")
    start = headers[0]
    if not _HEADER.fullmatch(lines[start]):
        return Refused("the options must follow the Options: line, one per line")
    found: list[tuple[str, str, str]] = []
    for line in lines[start + 1 :]:
        m = _LABEL.fullmatch(line)
        if m is None:
            break
        found.append((m[1], m[2], m[3]))
    if not MIN_OPTIONS <= len(found) <= MAX_OPTIONS:
        return Refused(f"{len(found)} option(s): an offer has {MIN_OPTIONS} to {MAX_OPTIONS}")
    styles = {(label.isdigit(), mark) for label, mark, _ in found}
    if len(styles) > 1:
        return Refused("mixed label styles")
    first = "1" if found[0][0].isdigit() else "A"
    expected = [chr(ord(first) + i) for i in range(len(found))]
    if [label for label, _, _ in found] != expected:
        return Refused("labels out of order, with a gap or a duplicate")
    options = []
    for label, _, words in found:
        problem = _option_problem(words)
        if problem is not None:
            return Refused(f"option {label}: {problem}")
        options.append(Option(label, words))
    recommends = [m[1] for line in lines if (m := _RECOMMEND.fullmatch(line))]
    recommended = None
    if len(recommends) > 1:
        return Refused("a second Recommend: line")
    if recommends:
        name = recommends[0].rstrip(").")
        if name not in expected:
            return Refused(f"Recommend: names {name[:10]!r}, which is not an offered label")
        recommended = name
    return Offer(tuple(options), found[0][1], recommended)


def _option_problem(text: str) -> str | None:
    if not text:
        return "empty"
    if len(text) > MAX_OPTION:
        return f"longer than {MAX_OPTION} characters"
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in text):
        return "a control or invisible character"
    if text.startswith("/"):
        return "starts with /"
    if "@" in text or "\uff20" in text or "\ufe6b" in text:
        return "an @ mention"
    if any(c in _LOOKALIKES for c in text):
        return "a character that hides what it says"
    if "`" in text:
        return "a backtick"
    if _LINK.search(text):
        return "a link"
    return None


def _local(name: str) -> str:
    """A name without a host prefix (``host-a:dev-1`` -> ``dev-1``), case-folded."""
    return name.strip().rsplit(":", 1)[-1].casefold()


def _same(author: str, owner: str) -> bool:
    """Whether a comment's author is the task's owner: the same name, or, on a multi-host
    farm where owner ids are ``<host>:<id>`` and comment authors are not, its local id."""
    a, o = author.strip().casefold(), owner.strip().casefold()
    return a == o or (":" in o and ":" not in a and a == _local(o))


def task_owner(task: Task) -> str | None:
    """Who asks on a task: the agent holding it, else its assignee."""
    return task.owner_id or task.assignee


def find_offer(task: Task) -> Found | None:
    """The offer on a blocked task (:class:`Found`), or None: the latest comment by the task's
    owner since :data:`OFFER_LEAD` before the block, unless a person commented after it;
    None too when that comment has no ``Options:`` line."""
    if task.status is not Status.BLOCKED or task.block is None or task.block.kelpie is not None:
        return None  # Kelpie's own stop waits for a person, not on an agent's question
    owner = task_owner(task)
    if not owner:
        return None
    earliest = task.block.since - OFFER_LEAD
    latest: Comment | None = None
    for c in task.comments:  # oldest first
        if c.human or not _same(c.author, owner) or c.at < earliest:
            continue
        if latest is None or c.at >= latest.at:
            latest = c
    if latest is None:
        return None
    if any(c.human and c.at > latest.at for c in task.comments):
        return None  # a person answered already
    result = parse_offer(latest.text)
    return None if result is None else Found(latest, result)


def question_key(task_id: str, asker: str, asked_at: datetime, digest: str) -> str:
    """One question: the task, who asked and when, and the offer's digest. Every digest
    message (and button) about it shares the key: one answer per question."""
    raw = "\n".join((task_id, _local(asker), asked_at.isoformat(), digest))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Expect:
    """What must still hold right before an answer is written: the task's owner, its block
    (when it began) and the offer's digest (``Action.params["expect"]``)."""

    owner: str
    block_since: datetime
    digest: str

    def params(self) -> dict[str, str]:
        return {
            "owner": self.owner,
            "block_since": self.block_since.isoformat(),
            "offer_digest": self.digest,
        }

    @classmethod
    def from_params(cls, value: object) -> Expect | None:
        if not isinstance(value, Mapping):
            return None
        owner, since, digest = (value.get(k) for k in ("owner", "block_since", "offer_digest"))
        if not (isinstance(owner, str) and isinstance(since, str) and isinstance(digest, str)):
            return None
        try:
            at = datetime.fromisoformat(since)
        except ValueError:
            return None
        if at.tzinfo is None or not owner or not digest:
            return None
        return cls(owner, at, digest)


def stale(task: Task | None, expect: Expect) -> str | None:
    """Why the question changed since the press (None: it holds): the task is gone or no
    longer blocked, another agent holds it, it was blocked again, or the owner's offer is
    not the one pressed."""
    if task is None:
        return "the task is gone"
    if task.status is not Status.BLOCKED or task.block is None:
        return f"the task is {task.status.value}, not blocked"
    owner = task_owner(task)
    if owner is None or owner.strip().casefold() != expect.owner.strip().casefold():
        return "another agent holds the task"
    if abs((task.block.since - expect.block_since).total_seconds()) >= 1:
        return "the task was blocked again"
    found = find_offer(task)
    if found is None or found.offer is None or found.offer.digest != expect.digest:
        return "the agent's options changed"
    return None


def answer_body(offer: Offer, index: int, who: str) -> str:
    """The comment an answer writes: Kelpie's fixed text and the agent's own option, quoted
    inert (in a fence longer than any backtick run in it, indented, a word joiner after
    every ``@``), so it pings nobody and starts no command. ``who`` is Kelpie's own words
    for the person ("Telegram user 123456789"), never their chat name."""
    quoted = offer.quoted(index)
    label = offer.options[index].label
    runs = [len(m) for m in re.findall(r"`+", quoted)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    inert = "\n".join(f"    {line}" for line in quoted.splitlines()).replace("@", "@" + WORD_JOINER)
    head = (
        f"Answer from a person (via Kelpie, {who}): option {label}, of the options you "
        "offered. Your option, quoted:"
    ).replace("@", "@" + WORD_JOINER)
    return f"{head}\n{fence}\n{inert}\n{fence}"


def offer_params(
    task: Task, found: Found, approver: str | None, answers: Mapping[str, Answer]
) -> dict[str, Any]:
    """What a digest item carries about an offer (``params.items[].offer``), or why it was
    refused (``offer_refused``). ``approver``: the person the digest is for (None:
    unassigned); ``answers``: the answers given so far, by question key."""
    if found.offer is None:
        assert isinstance(found.result, Refused)
        return {"offer_refused": found.result.why}
    offer = found.offer
    assert task.block is not None
    owner = found.comment.author
    question = question_key(task.id, owner, found.comment.at, offer.digest)
    out: dict[str, Any] = {
        "options": [{"label": o.label, "text": o.text} for o in offer.options],
        "style": offer.style,
        "recommended": offer.recommended,
        "digest": offer.digest,
        "question": question,
        "owner": task_owner(task) or owner,
        "block_since": task.block.since.isoformat(),
        "approver": approver,
    }
    if (answer := answers.get(question)) is not None:
        out["answered"] = {
            "label": answer.label,
            "by": answer.by,
            "at": answer.at.isoformat(),
            "pending": answer.outcome == Outcome.PENDING.value,
            "failed": answer.outcome == Outcome.FAILED.value,
        }
    return {"offer": out}


def offer_from_params(value: object) -> Offer | None:
    """An :class:`Offer` from ``params.offer`` (as :func:`offer_params` wrote it), checked
    again against the grammar's bounds; None when malformed."""
    if not isinstance(value, Mapping):
        return None
    raw, style, rec = value.get("options"), value.get("style"), value.get("recommended")
    if not isinstance(raw, list) or style not in (")", ".") or not MIN_OPTIONS <= len(raw) <= 4:
        return None
    options = []
    for i, o in enumerate(raw):
        if not isinstance(o, Mapping):
            return None
        label, text = o.get("label"), o.get("text")
        if not isinstance(label, str) or not isinstance(text, str):
            return None
        first = "1" if label.isdigit() else "A"
        if label != chr(ord(first) + i) or _option_problem(text) is not None:
            return None
        options.append(Option(label, text))
    if rec is not None and rec not in {o.label for o in options}:
        return None
    offer = Offer(tuple(options), style, rec)
    return offer if value.get("digest") == offer.digest else None


def answers_from_ledger(entries: Iterable[LedgerEntry]) -> tuple[Answer, ...]:
    """``Snapshot.answers`` from the ledger's chat answer entries (kind ``answer``): those
    written, still pending, or whose write failed (it may have reached the board: the
    question stays claimed for a person to look at). A refused or dry-run answer gave the
    question back."""
    out = []
    for e in entries:
        if e.kind is not ActionKind.ANSWER or e.outcome not in _KEPT or e.task_id is None:
            continue
        p = e.params
        question, label, by = p.get("question"), p.get("label"), p.get("by")
        if not (isinstance(question, str) and isinstance(label, str) and isinstance(by, str)):
            continue
        out.append(Answer(e.task_id, question, label, by, e.at, e.outcome.value))
    return tuple(out)


_KEPT = frozenset({Outcome.PENDING, Outcome.EXECUTED, Outcome.FAILED})


@dataclass(frozen=True, slots=True)
class Answered:
    """How an item's question was answered: the option's label, by whom, when; ``pending``
    while the comment is still being written."""

    label: str
    by: str
    at: datetime
    pending: bool = False
    failed: bool = False  # the write failed: it may or may not be on the task


@dataclass(frozen=True, slots=True)
class ItemOffer:
    """A digest item's offer as the alert layer gets it (``AlertItem.offer``): the options,
    the question's key, what a confirmed answer re-checks (``expect``), the person the item
    is for (``approver``; None: unassigned) and, once answered, how."""

    offer: Offer
    question: str
    expect: Expect
    approver: str | None = None
    answered: Answered | None = None


_QUESTION = re.compile(r"[0-9a-f]{32}")


def item_offer(value: object) -> ItemOffer | None:
    """An :class:`ItemOffer` from a digest item's ``params.offer``; None when malformed."""
    offer = offer_from_params(value)
    if offer is None or not isinstance(value, Mapping):
        return None
    question, approver = value.get("question"), value.get("approver")
    expect = Expect.from_params(
        {
            "owner": value.get("owner"),
            "block_since": value.get("block_since"),
            "offer_digest": offer.digest,
        }
    )
    if not isinstance(question, str) or not _QUESTION.fullmatch(question) or expect is None:
        return None
    if approver is not None and (not isinstance(approver, str) or not approver.strip()):
        return None
    answered = None
    raw = value.get("answered")
    if isinstance(raw, Mapping):
        label, by, at = raw.get("label"), raw.get("by"), raw.get("at")
        if isinstance(label, str) and isinstance(by, str) and isinstance(at, str):
            try:
                when = datetime.fromisoformat(at)
            except ValueError:
                when = None
            if when is not None and when.tzinfo is not None:
                answered = Answered(
                    label, by, when, raw.get("pending") is True, raw.get("failed") is True
                )
    return ItemOffer(offer, question, expect, approver, answered)
