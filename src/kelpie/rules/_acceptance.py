"""Acceptance criteria people state on a task, and the evidence that answers them (rule 1,
incident 18).

A person asked for "20 runs in a row, 20 of 20" before automerge; the reviewer confirmed
green CI and a clean merge only, and the PR merged without that evidence. Criteria come
from text a person wrote: the task's description when a person created it
(``Task.body_author``), and comments by people (``Comment.human``, or an author the
snapshot knows as a human owner). Every line (or sentence) of it that matches one of the
configured patterns is one criterion, as written (a question is none).

Evidence must address the criterion: name its reference (``Criterion.ref``, "AC-" and six
hex digits; rule 1 announces it on the task while it is worked on) or quote it. A summary,
a CI result or a reviewer's approval that does not address it answers nothing, however it
is worded: free text cannot be matched to a criterion safely, and addressing it is a
deliberate claim that a person can check.

Evidence is a comment (an agent's or a person's), the summary of one of the task's sessions,
an artifact's file name ("AC-1a2b3c-met-20-of-20-passed.txt") or a CI build of the task's
branch whose workflow names the reference (it answers by passing), each written after the
criterion. What answers is the text right after the reference, or after the quoted
paragraph or ">" block (the rest of it is the person's own words), up to a blank line or the
next reference or quote, and it must open with a verdict: "met" or a check mark ("AC-1a2b3c:
met, 20/20 passed, <link>"). Then one sentence there must report the result:

- the criterion asks for a ratio ("20 of 20", "20/20"): that ratio of runs next to pass
  wording ("20/20 passed", "all 20 green", "20 runs green"), not of tests or checks;
- it asks for a count ("20 runs"): that number next to run wording, and pass wording;
- it has no number: each of its words of three letters or more (common ones aside) and
  result wording ("checked on a tablet: works").

A failure, a partial result ("17/20"), another run count ("out of 21"), runs left out
("excluding run 7"), something still to do (a plan, "pending", "retried", an unchecked
"[ ]") or a request ("please post the log") anywhere in the text's own words, asides
included (what it quotes aside), answers nothing and takes an earlier answer back; so does
a verdict of "not met", "met?" or a cross, and a person's text that addresses it without
"met". The verdict sentence itself must be clean: no "no", "not", "should" in it.
The latest text that addresses a criterion decides; any failure wording or partial result
in a person's later comment on the task takes an answer back too (not told apart from an
unrelated one), and a request ("please post the log") answers nothing, whoever writes it.
A person's later line that only repeats a criterion, or opens with a verdict ("AC-1a2b3c:
met", "not met"), is no new criterion; any other line that matches the patterns is one
(a bare "saw the log, 20/20" too: the safe way). Links and paths are no wording.
Quoted lines and references in a person's comment are not read as criteria.

A reviewer's "CI green at 20:14, merges cleanly" answers none of them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from kelpie.core.model import BuildStatus, OwnerKind, Snapshot, Task

MAX_CRITERION = 300  # characters of a criterion kept
MAX_TEXT = 8000  # characters of one text searched for criteria or evidence
_SPLIT = re.compile(r"(?:\n+|(?<=[!?;])\s+|(?<=[.])\s+(?=[A-Z0-9]))")
_RATIO = re.compile(r"\b(\d+)\s*(?:/|of|out of)\s*(\d+)\b", re.IGNORECASE)
# A count of runs: "20 runs", "20 times", "20 consecutive runs"; not a time (20:14), a version
# (17.2) or a share (20%).
_COUNT = re.compile(r"\b(\d+)(?![:.%]?\d)\s+(?:\w+\s+)?(?:runs?|times|iterations)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\b\d+\b")
_WORD = re.compile(r"\b[^\W_]{3,}\b")
# A sentence that is no result report: a question, a plan, a requirement, a failure, a refusal.
_NOT_A_RESULT = re.compile(
    r"\?|\b(?:will|i'll|going to|plan(?:s|ned)?|todo|later|should|could|would|"
    r"requires?|required|needs?|need to|waiting|asked|must|remember|reminder|"
    r"fail\w*|flak\w*|broke\w*|errors?|timed? ?out|red|not|no|never|didn't|did not|without|"
    r"couldn't|can't|cannot|unable|still|retr(?:y|ied|ies)|re-?runs?|re-?ran|"
    # Not done yet, or not all of it: "20 runs queued", "all green except one".
    r"queued|pending|scheduled|kicked|so far|except|apart from|the rest|first \d+|"
    r"but (?:\d+|one|two|three|a few)|all but|save one|bar one|until|"
    # A plan, a target or a checklist item still to do.
    r"target|goal|expect\w*|remaining|next steps?|once|ensure|make sure|"
    r"before (?:auto)?merg\w*|only (?:after|when|if|with)|gate\w*|hold|is the bar|"
    r"hung|hang\w*|crash\w*|abort\w*|killed|panic\w*|skipped|cancel\w*)\b|\[ \]",
    re.IGNORECASE,
)
# Failure wording in a later sentence takes a report back: "20/20 green. Then 2 failed."
_FAILURE = re.compile(
    r"\b(?:fail\w*|flak\w*|broke\w*|error\w*|timed? ?out|timeouts?|red|hung|hang\w*|"
    r"crash\w*|abort\w*|killed|panic\w*|cancel\w*|exception\w*|segfault\w*|oom|threw|"
    r"not re-?run|quarantin\w*|disabled|xfail\w*|skip\w*|not met|unmet)\b|(?-i:[a-z]Error)\b"
    r"|[\u274c\u2717\u2718]",
    re.IGNORECASE,
)
# A sentence that reports a result.
_RESULT = re.compile(
    r"\b(?:pass(?:ed|es|ing)?|green|succeeded|success(?:ful)?|ok|okay|works?|worked|"
    r"verified|confirmed|done|did|completed|finished|checked|tested|ran|clean)\b|[✓✅]",
    re.IGNORECASE,
)
_STOP = frozenset(
    {
        "the", "and", "for", "with", "this", "that", "from", "have", "must", "should",
        "before", "after", "please", "make", "sure", "need", "needs", "there", "their",
        "they", "them", "then", "than", "when", "what", "which", "will", "would", "could",
        "into", "also", "only", "each", "every", "merge", "merged", "merging", "automerge",
        "done", "task", "check", "checks", "checked", "test", "tests", "tested", "testing",
        "verify", "verified", "review", "reviewed", "pass", "passes", "passed", "look",
        "looks", "good", "fine", "okay", "again", "first", "run", "runs", "ran", "times",
        "row", "all", "but", "are", "was", "were", "has", "its", "you", "our", "can", "not",
        "out", "consecutive", "consecutively", "flaky", "green",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class Criterion:
    text: str
    asked_by: str
    at: datetime
    ref_digits: int = 6  # 8 when two criteria of one task would share a reference

    @property
    def key(self) -> str:
        return hashlib.sha256(self.text.casefold().encode()).hexdigest()[:12]

    @property
    def ref(self) -> str:
        """How evidence names it: ``AC-`` and six hex digits."""
        return f"AC-{self.key[: self.ref_digits]}"

    def addressed(self, text: str) -> list[str]:
        """The parts of ``text`` that address this criterion, each up to the next blank
        line or the next criterion named or quoted:

        - what follows its ``ref`` (not one inside a path or a link: "logs/AC-1a2b3c.txt");
        - what follows a quote of it, once the quoted paragraph or ">" block is over (at a
          blank line): the rest of it is the person's own words, not evidence.
        """
        text = text[:MAX_TEXT]
        parts = []
        ref = rf"(?<![\w/.=-]){re.escape(self.ref)}\b"
        for m in re.finditer(ref, text, re.IGNORECASE):
            parts.append(_until_break(text, m.end()))
        for m in self._quotes(text):
            gap = _BLANK.search(text, m.end())  # the quoted paragraph or block is over
            parts.append(_until_break(text, gap.end() if gap else len(text)))
        return parts

    def verdicts(self, text: str) -> list[str]:
        """The parts that address it and open with a verdict ("AC-1a2b3c: met, 20/20
        passed"): only those can answer it. Restated requirements ("AC-1a2b3c: 20/20 green
        before automerge") open with none."""
        return [p[m.end() :] for p in self.addressed(text) if (m := _VERDICT.match(p))]

    def _quotes(self, text: str) -> list[re.Match[str]]:
        quote = r"\W+".join(re.escape(w) for w in re.findall(r"\w+", self.text))
        return list(re.finditer(rf"\b{quote}\b", text, re.IGNORECASE))

    def answered_by(self, evidence: str) -> bool | None:
        """True when ``evidence`` addresses this criterion and reports its result there;
        False when it addresses it and reports, anywhere in its own words, a failure, a
        partial result or something still to do (it takes an earlier answer back); None
        when it does not address it, or addresses it without a result ("AC-1a2b3c: see the
        log above")."""
        evidence = _unlinked(evidence[:MAX_TEXT])  # a URL's path is no wording
        parts = self.addressed(evidence)
        if not parts:
            return None
        if any(_UNMET.match(p) for p in parts):
            return False  # "AC-1a2b3c: not met", a cross
        if self._vetoes(self.own_words(evidence)):
            return False  # anywhere in it: before the reference, after a blank line, an aside
        if any(self._answers(s) for p in self.verdicts(evidence) for s in _SPLIT.split(p)):
            return True
        return None

    def example_result(self) -> str | None:
        """A result that answers this criterion after "<ref>: met, " ("20/20 passed" for
        "20 runs in a row, 20 of 20"), for telling people how to report it; None when no
        simple one does (it needs numbers of its own, such as a version)."""
        tries = [f"{a}/{b} passed" for a, b in _RATIO.findall(self.text)[:1]] + ["passed"]
        return next((t for t in tries if self.answered_by(f"{self.ref}: met, {t}")), None)

    def _vetoes(self, text: str) -> bool:
        """A failure, a partial or pending result, a run count other than its own, something
        still to do or a request, anywhere in ``text``, asides included."""
        mine = set(_NUMBER.findall(self.text))
        for s in _SPLIT.split(text):
            plain = _NONE_FAILED.sub(" ", s)
            if not plain.strip():
                continue
            if (
                _FAILURE.search(plain)
                or _partial(s)
                or _PLAN.search(plain)
                or _REQUEST.search(plain)
                or _NOT_ALL.search(plain)
                or _CHERRY.search(plain)
                or any(n not in mine for n in _run_counts(plain))
            ):
                return True
        return False

    def own_words(self, text: str) -> str:
        """``text`` without what it quotes: ">" lines and the quoted criterion itself. Only
        those: the words around a quote may be the writer's own failure report."""
        text = re.sub(r"(?m)^[ \t]*>.*$", " ", text)
        spans = [m.span() for m in self._quotes(text)]
        for start, end in reversed(spans):  # all found before any cut: offsets hold
            text = text[:start] + " " + text[end:]
        return text

    def _answers(self, sentence: str) -> bool:
        plain = _plain(sentence)
        if _NOT_A_RESULT.search(plain) or not _RESULT.search(plain):
            return False
        ratios = set(_RATIO.findall(self.text))
        # A ratio reported with its result: "20/20 passed", "passed 20 of 20".
        if ratios and not ratios <= _reported_ratios(sentence):
            return False
        counts = set(_COUNT.findall(self.text)) - {b for _, b in ratios}  # 20/20 is 20 runs
        if counts and not (counts <= _counts(sentence) and re.search(_PASS_WORDS, plain, re.I)):
            return False  # "ran it 20 times" says nothing of how they went
        numbers = set(_NUMBER.findall(self.text)) - {n for pair in ratios for n in pair} - counts
        numbers -= set(_COUNT.findall(self.text))
        if not numbers <= set(_NUMBER.findall(sentence)):
            return False  # other numbers (a version, an issue) must be there too
        if ratios or counts:
            # "unit tests: 20/20 passed" counts tests, not runs; "build: <link>" after it is
            # fine.
            first = min((m.start() for m in (*_RATIO.finditer(plain), *_COUNT.finditer(plain))),
                        default=0)  # fmt: skip
            return not _NOT_RUNS.search(plain[max(0, first - 40) : first])
        # No number: its words ("checked it on a tablet: works").
        words = _words(self.text)
        return bool(words) and words <= _words(sentence)

    def about(self, line: str) -> bool:
        """Whether a person's later ``line`` repeats this criterion rather than adding one:
        no number it lacks, and no word beyond its own and reminder wording ("Reminder: 20
        runs in a row"). "Also 20 runs on the iOS simulator" is a new criterion."""
        line = _REFS.sub(" ", line)
        numbers = set(_NUMBER.findall(line))
        if not numbers <= set(_NUMBER.findall(self.text)):
            return False
        if not numbers and not _words(self.text) <= _words(line):
            return False
        return _words(line) - _words(self.text) <= _REMINDER

    @property
    def answerable(self) -> bool:
        """It names something to look for: a number, or a word beyond the common ones."""
        return bool(_NUMBER.search(self.text) or _words(self.text))


_ASK = re.compile(r"\s*(?:can|could|would|will) (?:you|we|someone)\b|\s*please\b", re.IGNORECASE)
_PAREN = re.compile(r"\([^)]*\)")
_NONE_FAILED = re.compile(
    # A count of none, of failures as a noun: "0 failures", "no new flakes"; not "no, it
    # failed", "nothing failed except run 7".
    r"\b(?:0|no|zero|not a single|without(?: any)?)\s+(?:(?:new|more|test|run)\s+)?"
    r"(?:failures?|flakes?|errors?|retries|issues?|regressions?)\b"
    r"|\b0 failed\b|\bfailed:\s*0\b|\b(?:no|nothing|zero)\s+(?:\w+\s+)?(?:red|flaky|broken)\b"
    # The task's subject, not a result: "looped the flaky test 20 times".
    r"|\bflak\w*\s+(?:\w+\s+)?tests?\b",
    re.IGNORECASE,
)
_PASS_WORDS = r"(?:passed|pass|green|ok|succeeded|successful|clean|confirmed|verified|✓|✅)"
# Words a person adds when repeating a criterion (first five letters): no new criterion.
_REMINDER = frozenset(
    {
        "remin", "frien", "don", "forge", "still", "waiti", "clear", "kindl", "just", "gentl",
        "again", "follo", "note", "ping", "befor",
    }
)  # fmt: skip
# A person saying no: "isn't met", "not yet met", "met? no", "nope", "no, that's not 20".
_NOT_MET = re.compile(
    r"\b(?:not|never|no|yet)\W+(?:\w+\W+){0,3}met\b|n't\W+(?:\w+\W+){0,3}met\b"
    r"|\bmet\s*\?|^\W*(?:no|nope)\b|\bredo\b",
    re.IGNORECASE,
)
# What makes the rest of a verdict line a new ask.
_ASKS_MORE = re.compile(
    r"\b(?:but|before|after|also|then|next|must|need|needs|should|please|only|until|"
    r"run it|do)\b",
    re.IGNORECASE,
)
# Where the evidence for one criterion ends: a blank line, another criterion named, a quote.
_BREAK = re.compile(
    r"\n[ \t]*\n|(?<![\w/.=-])AC-[0-9a-f]{6}|^[ \t]*>", re.IGNORECASE | re.MULTILINE
)
_BLANK = re.compile(r"\n[ \t]*\n")
# How an answer opens, right after the reference or the quote: "met", "✅".
_VERDICT = re.compile(
    r"[\s:\-\u2013\u2014,(\"'*`]*(?:met(?![?\w-])|\u2705|\u2713)[\s:\-\u2013\u2014,.)*`]*", re.I
)
# A verdict that says no: "not met", "unmet", "met?", a cross.
_UNMET = re.compile(
    r"[\s:\-\u2013\u2014,(\"'*`]*(?:not met|unmet|met\s*\?|\u274c|\u2717|\u2718)", re.I
)
_LINK = re.compile(
    r"https?://[^\s<>()]*[^\s<>().,;:!?'\"]|<[^<>\s]+>|\]\([^()]*\)"  # URLs, <links>, md targets
    r"|(?<![\w.~/-])(?:~|\.{1,2})?/[\w.~/-]*"  # /abs, ./rel, ~/home paths
    r"|(?<![\w./-])[\w-]+(?:\.[\w-]+)+/[\w./-]*"  # host/path: ci.example/run/42
    r"|(?<![\w./-])(?:[\w-]+/)+[\w-]+\.[A-Za-z]\w{0,4}\b"  # dir/file.ext
)
# Results that leave runs out: "excluding run 7", "1 run discarded", "in a mock".
_CHERRY = re.compile(
    r"\b(?:exclud\w*|ignor\w*|discard\w*|dropped|lost|died|stuck|invalid|bad runs?|outliers?|"
    r"partial\w*|halfway|mostly|mock\w*|untested|hasn't|unmet|not met)\b|[\u274c\u2717\u2718]",
    re.IGNORECASE,
)
# Not all of the runs, or not yet, anywhere in an evidence text ("(so far)", "except run
# 7"); unlike _NOT_A_RESULT, plain "no" or "not" elsewhere ("no other changes") is fine.
_NOT_ALL = re.compile(
    r"\b(?:so far|except|apart from|but (?:\d+|one|two|three|a few)|all but|save one|bar one|"
    r"still (?:running|pending|going)|not (?:all|every|on|yet)|first \d+|remaining|"
    r"didn't run|did not run|without (?:the )?(?:fix|runs?|ci)|"
    r"omit\w*|minus|leav\w* out|remov\w*|cut short|kept|(?:top|best|last) \d+|"
    r"runs? \d+\s*(?:-|to)\s*\d+|\d+(?:st|nd|rd|th) run)\b",
    re.IGNORECASE,
)
_TOTAL = re.compile(
    r"\b(?:out of|of|in)\s+(\d+)\s+(?:runs?|times|total)\b|\bout of\s+(\d+)\b|\b(\d+)\s+total\b",
    re.IGNORECASE,
)
_REFS = re.compile(r"\bAC-[0-9a-f]{6,8}\b", re.IGNORECASE)
# Something still to do, in what addresses a criterion: not a result yet.
_PLAN = re.compile(
    r"\b(?:will|i'll|going to|todo|later|tomorrow|pending|queued|scheduled|target|goal|"
    r"in progress|working on|waiting|not (?:yet|started|run)|to do|tbd|eventually|"
    r"after \d+ (?:attempts|tries)|retr(?:y|ied|ies)|re-?ran|re-?runs?|re-?run\w*|restart\w*|"
    r"re-?trigger\w*|attempts?|(?:second|third|\d+(?:nd|rd|th)) (?:attempt|try|go)|"
    r"probably|i assume|dry-?run|simulated)\b|\[ \]",
    re.IGNORECASE,
)
# Someone asking for the result rather than reporting it: "AC-1a2b3c: please post the log".
_REQUEST = re.compile(
    r"\b(?:please|post|show|share|attach|confirm|want|let me know|can you|could you|"
    r"requesting|request|need|needs|waiting|wait)\b",
    re.IGNORECASE,
)
# Kelpie's own announcement of a criterion: neither a criterion nor evidence.
ANNOUNCEMENT = "Acceptance criterion AC-"
# What a ratio or count of runs is not: "unit tests: 20/20", "all 20 checks", "10 CI runs".
_NOT_RUNS = re.compile(
    r"\b(?:tests|test suite|checks?|jobs?|items|suites?|workflows?|builds?|pipelines?|lint|"
    r"ci runs|nightly)\b",
    re.IGNORECASE,
)
# What may follow a ratio that reports runs: nothing, a run word, a pass word or a
# preposition ("20/20 runs passed", "20/20 green on the device"); not "20/20 checks".
_RUN_NOUN = re.compile(
    rf"(?:\s*[.;!)✓✅]|\s*$|[\s,:]*(?:runs?|times|iterations|loops?|in|on|with|for|across|over|"
    rf"{_PASS_WORDS[3:-1]})\b)",
    re.IGNORECASE,
)
# A pass word followed by what passed when that is no run: "20/20 green CI checks".
_OTHER_NOUN = re.compile(
    rf"[\s,:]*(?:\w+\s+)?(?:{_PASS_WORDS}\s+)?(?:ci\s+)?"
    r"(?:checks?|jobs?|builds?|workflows?|pipelines?|prs?|files?|steps?|suites?|tests?)\b",
    re.IGNORECASE,
)


def _until_break(text: str, begin: int) -> str:
    """``text`` from ``begin`` to the next blank line, criterion reference or quote line."""
    stop = _BREAK.search(text, begin)
    return text[begin : stop.start() if stop else len(text)]


def _unlinked(text: str) -> str:
    """``text`` without links and paths: "https://ci.example/runs/42/attempts/1"."""
    return _LINK.sub(" ", text)


def _run_counts(text: str) -> set[str]:
    """Numbers of runs a text names: "20 runs", "out of 21", "in 21 runs", "21 total"."""
    found = set(_COUNT.findall(text))
    for groups in _TOTAL.findall(text):
        found |= {g for g in groups if g}
    return found


def _partial(sentence: str) -> bool:
    """A ratio short of all: "17/20", "3 of 20 crashed"."""
    return any(a != b for a, b in _RATIO.findall(_NONE_FAILED.sub(" ", sentence)))


def _plain(sentence: str) -> str:
    """The sentence without asides ("(it was red before)") and without reports of no
    failure ("0 failures", "no flakes"), which are no failure wording."""
    return _NONE_FAILED.sub(" ", _PAREN.sub(" ", sentence))


def _reported_ratios(sentence: str) -> set[tuple[str, str]]:
    """Ratios next to pass wording, either side, within a word or two."""
    found = set()
    for m in _RATIO.finditer(sentence):
        after = sentence[m.end() : m.end() + 30]
        if not _RUN_NOUN.match(after) or _OTHER_NOUN.match(after):
            continue
        before = sentence[max(0, m.start() - 20) : m.start()]
        if re.match(rf"[\s,:]*(?:\w+\s+)?{_PASS_WORDS}", after, re.IGNORECASE) or re.search(
            rf"{_PASS_WORDS}\s*[:,]?\s*(?:\w+\s+)?$", before, re.IGNORECASE
        ):
            found.add((m[1], m[2]))
    # As many passed as ran, said right there: "all 20 passed", "20 runs green", "ran it 20
    # times in a row, all green". A sentence that adds "except", "so far" or a partial
    # ratio is no result (see _NOT_A_RESULT and Criterion._answers).
    runs = r"(?:runs?|times|iterations|loops?)"
    row = r"(?:\s+in\s+a\s+row|\s+consecutively)?"
    for n in re.findall(rf"\ball\s+(\d+)\s+(?:{runs}\s+)?{_PASS_WORDS}", sentence, re.I):
        found.add((n, n))
    for n in re.findall(
        rf"\b(\d+)\s+{runs}{row}\s*[,:]?\s+(?:all\s+)?{_PASS_WORDS}", sentence, re.I
    ):
        found.add((n, n))
    return found


def _counts(sentence: str) -> set[str]:
    """Counts of runs a sentence reports: "ran it 20 times", "20 runs green", "looped 20x"."""
    found = set(_COUNT.findall(sentence))
    found |= set(re.findall(r"\b(\d+)x\b", sentence))
    return found


def _words(text: str) -> set[str]:
    return {
        w.casefold()[:5]
        for w in _WORD.findall(text)
        if not w.isdigit() and w.casefold() not in _STOP
    }


def criteria(
    task: Task, patterns: Sequence[re.Pattern[str]], people: frozenset[str] = frozenset()
) -> list[Criterion]:
    """The acceptance criteria people stated on ``task``, oldest first, each once.
    ``people`` are the ids of human owners: their comments are a person's too."""
    texts: list[tuple[str, str, datetime]] = []
    if task.body_author and task.body:
        texts.append((task.body, task.body_author, _created(task)))
    texts += [
        (c.text, c.author, c.at)
        for c in task.comments
        if (c.human or c.author in people) and not c.text.lstrip().startswith(ANNOUNCEMENT)
    ]
    found: dict[str, Criterion] = {}
    for text, author, at in sorted(texts, key=lambda t: t[2]):
        # What it quotes ("> ...") is someone else's; a verdict ("AC-1a2b3c: met, ...") is
        # evidence; any other reference names an existing criterion.
        own = _REFS.sub("", re.sub(r"(?m)^[ \t]*>.*$", " ", text[:MAX_TEXT]))
        own = re.sub(r"[ \t]{2,}", " ", own)
        for line in _SPLIT.split(own):
            line = line.strip(" \t:-—,")
            if not line or not any(p.search(line) for p in patterns):
                continue
            verdict = _VERDICT.match(line) or _UNMET.match(line)
            if verdict:
                # A verdict on an earlier criterion is evidence; what follows it is a new
                # criterion only when it asks for more ("met, but 50 runs on iOS too").
                line = line[verdict.end() :].strip(" \t:-—,")
                if not (_ASKS_MORE.search(line) and any(p.search(line) for p in patterns)):
                    continue
            if "?" in line and not _ASK.match(line):
                continue  # a question, not a request ("Can you do 20 runs?" is one)
            if any(c.at < at and c.about(line) for c in found.values()):
                continue  # a person confirming, or repeating, an earlier criterion
            c = Criterion(line[:MAX_CRITERION], author, at)
            if c.answerable:
                found.setdefault(c.key, c)
    out = sorted(found.values(), key=lambda c: (c.at, c.text))
    shared = {r for r in (c.ref for c in out) if [d.ref for d in out].count(r) > 1}
    return [replace(c, ref_digits=8) if c.ref in shared else c for c in out]  # unique refs


@dataclass(frozen=True, slots=True)
class Evidence:
    at: datetime
    text: str
    by_person: bool = False
    build: bool | None = None  # a CI build of the task's branch: whether it passed
    author: str = ""


def evidence(task: Task, snapshot: Snapshot, after: datetime) -> list[Evidence]:
    """Text written after ``after`` that may report a result about ``task``, oldest first:
    comments, session summaries, artifact paths (separators as spaces: "AC-1a2b3c-20-of-20-
    passed.txt") and the CI builds of the task's branch (their workflow name, and whether
    they passed)."""
    people = frozenset(o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN)
    found = [
        Evidence(c.at, c.text, c.human or c.author in people, author=c.author)
        for c in task.comments
        if c.at > after and not c.text.lstrip().startswith(ANNOUNCEMENT)
    ]
    for s in snapshot.sessions_for(task.id):
        if s.summary and s.ended_at is not None and s.ended_at > after:
            found.append(Evidence(s.ended_at, s.summary))
        found += [  # the file's name, its separators as spaces but for "AC-1a2b3c"
            Evidence(a.at, re.sub(r"(?<![Aa][Cc])[-_]+", " ", a.path.rsplit("/", 1)[-1]))
            for a in s.artifacts
            if a.at > after
        ]
    for b in snapshot.builds:
        done = b.finished_at
        if (
            task.branch
            and b.branch == task.branch
            and done is not None
            and done > after
            and b.status in (BuildStatus.SUCCESS, BuildStatus.FAILURE)
        ):
            found.append(Evidence(done, b.workflow or "", build=b.status is BuildStatus.SUCCESS))
    return sorted(found, key=lambda e: (e.at, e.by_person))  # a person's word last on a tie


def unanswered(
    task: Task, snapshot: Snapshot, patterns: Sequence[re.Pattern[str]]
) -> list[Criterion]:
    """The criteria stated on ``task`` that the evidence since leaves unanswered. Only text
    that addresses a criterion (names its ``ref`` or quotes it) answers it; the latest such
    text decides, and a person's later report of a failure takes an answer back."""
    people = frozenset(o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN)
    out = []
    for c in criteria(task, patterns, people):
        answered = False
        for e in evidence(task, snapshot, c.at):
            if e.build is not None:  # a job named for the criterion ("ac-1a2b3c.yml")
                verdict = e.build if re.search(rf"\b{c.ref}\b", e.text, re.I) else None
            else:
                verdict = c.answered_by(e.text)
                if verdict is None and e.by_person and c.addressed(_unlinked(e.text[:MAX_TEXT])):
                    verdict = False  # a person on it, without "met": "AC-1a2b3c: isn't met"
            if verdict is not None:
                answered = verdict
            elif e.by_person and any(_takes_back(s) for s in _SPLIT.split(e.text[:MAX_TEXT])):
                answered = False  # "it still fails on my machine"
        if not answered:
            out.append(c)
    return out


def _takes_back(sentence: str) -> bool:
    """A person's sentence that reports a failure or a partial result, of anything: on a
    task with a criterion, a person's "it fails on my machine" or "18 green, 2 red" is not
    told apart from an unrelated failure (the safe direction)."""
    plain = _NONE_FAILED.sub(" ", _unlinked(sentence))
    return bool(
        _FAILURE.search(plain)
        or _CHERRY.search(plain)
        or _partial(sentence)
        or _NOT_MET.search(plain)
        or _REQUEST.search(plain)
    )


def _created(task: Task) -> datetime:
    """When the description was written: the task's creation, else the earliest time known."""
    if task.created_at is not None:
        return task.created_at
    times = [c.at for c in task.comments]
    return min([*times, task.status_since])
