"""Return causes — why tasks come back (incident 7).

46 of 63 task returns on a live farm were merge conflicts, nearly all in a few shared files
(a route registry, a central types module): agents working in parallel kept editing them,
and every merge undid someone's work. This rule groups the returns of the last ``window``
(24h) by cause and turns a dominant cause into a recommendation.

**Causes.** What sent a task back, not the stage it went through: the first of
``cause_patterns`` (category -> regular expressions, case-insensitive; trusted config, run
on the first 1000 characters of each reason) that matches a return's reason
(``Task.returns``) names its cause, ``merge_conflict`` first and then the configured
categories in their order; else ``failing_tests`` when it names a test as failing
(``test_names``, below) or, with ``ci_wording`` (on), says so in CI's words ("tests
failed", "CI is red", "the build failed", "3 failed", pytest's ``FAILED x::y``) in a
clause that does not say none failed ("no unit tests failed", "0 tests failed") or that it
was fixed ("Fixed failing tests", "now green"); else, for a return its platform files
under a stage (``stage_categories``: ``review``, which is every Hermes return, changes
requested or a reopened review), what was found there (``review_findings``); else the
category its platform gave it, else ``other``. By default ``cause_patterns`` recognise
only ``merge_conflict``, in git's wording ("merge conflict in", "CONFLICT (content)",
"conflicts with main"; review prose such as "this conflicts with the design" is not a
conflict). The share of each cause is computed over every return in the window, of
finished tasks too. On a live farm every return went through review, and the rule kept
reporting that 95 to 100% of them were "review".

**Hot files.** The files a conflict names: paths in its reason, matched to the task's files
(``Task.files``; a bare file name is resolved to the one file of that name any task
reports), minus ``expected_shared`` (glob patterns of files many tasks are meant to edit;
without its own list, this rule uses rule 12's, ``conflict_warning`` ``expected_shared``).
Version numbers, abbreviations, branch names and web addresses are not files. A file named
by at least ``hot_file_min_conflicts`` (3) conflicts is hot.

**What Kelpie does.** With at least ``min_returns`` (5) returns in the window, merge conflicts
making up at least ``conflict_ratio_threshold`` (0.5) of them, and hot files: one lasting
alert naming the hot files, with the shares of each cause and a recommendation (give each
hot file one owner, split it, or serialize the work that touches it; rule 12 warns live
tasks that share files), and a ``comment`` to each task in progress that touches a hot file,
once per task and set of files.

**Otherwise**, a cause behind at least ``dominant_ratio`` (0.5) of the returns (``other``
excepted), such as conflicts spread over many files, gets one informational lasting alert
with the shares by cause: there is no file to act on. A stage's findings are not reported
as dominant when every return of the window was filed under a stage: that is how the farm
works, not a cause to act on.

**Flaky tests** (incident 18). A test named as failing (``test_names``: patterns with a named
group ``test``; by default pytest's ``test_...`` and XCTest's ``SomeTests.testSomething``;
next to failure wording, not a file path, not in a merge conflict) in the returns of at
least ``flaky_min_tasks`` (2) different tasks in the window fails other tasks' work, not
just its own: its returns count as cause ``flaky_test``, and it gets one lasting
alert naming the test and the tasks it sent back (fix or quarantine it). This needs no
``min_returns``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Return, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._alerts import alert_params, count

NAME = "return_causes"
CONFLICT = "merge_conflict"
OTHER = "other"
FLAKY = "flaky_test"
FAILING = "failing_tests"
DEFAULT_STAGES = ("review",)  # platform categories that name where a return happened
_FAILED = re.compile(
    r"\b(?:(?i:fail\w*|flak\w*|timed? ?out|red|errored|broke\w*)|[A-Z]\w*Error)\b"
)  # an exception name ("AssertionError") is case-sensitive: "no error" is none
_NO_FAILURE = re.compile(r"\b(?:no|without|0)\s+(?:\w+\s+)?(?:errors?|failures?)\b", re.I)
# "Fixed TypeError in test_parse": resolved before the name, not "not fixed" after it.
_RESOLVED = re.compile(r"\b(?:fixed|resolved)\b", re.IGNORECASE)
_NEGATED = re.compile(
    r"(?:\b(?:not|never|nothing)|n't)\s+(?:(?:yet|been|got|it)\s+)*$", re.IGNORECASE
)
_FIXED = re.compile(r"\b(?:fix(?:ed|es)?|added|new|renamed)\b", re.IGNORECASE)
# Where a clause ends: "add test_x; lint failed", "Added test_x. Lint failed." or "test_x is
# fixed, but test_y is flaky" names no failing test_x. A dot inside a name
# ("LoginTests.testTimeout") is followed by no space, and the dots of "test_x ... FAIL"
# (unittest's verbose output) end nothing.
_CLAUSE_END = re.compile(
    r"[;\n]|[!?](?=\s)|\.{3}(?!\s*(?:FAIL|ERROR|ok|PASS|SKIP))|(?<!\.)\.(?=\s)"
    r"|,\s*(?=(?:but|while|whereas|and|then)\b)"
)
_BULLET = " \t:-*>"
_ITEM = re.compile(r"\s*(?:[-*•>]|\d+[.)])\s+\S")  # a bulleted or numbered line
DEFAULT_TEST_NAMES = (
    r"\b(?P<test>test_\w+)",  # pytest (also "FAILED tests/test_api.py::test_create")
    r"\b(?P<test>[A-Z]\w*Tests?[./]test\w+)",  # XCTest: LoginTests.testTimeout
)
# Git's own wording, so review prose ("this conflicts with the design doc", "no merge
# conflicts") is not a conflict.
DEFAULT_CAUSES: Mapping[str, tuple[str, ...]] = {
    CONFLICT: (
        r"\bmerge conflicts? in\b",
        r"\bCONFLICT \(\w[\w/ -]*\)",
        r"\bconflicts? with (?:origin/)?(?:main|master|the base branch)\b",
        r"\b(?:rebase|merge) (?:failed|stopped)\b.*\bconflict",
    ),
}
# CI's wording for failing tests, when no test is named ("tests failed", "CI is red", "the
# build failed", "3 failed", pytest's "FAILED x::y"); a clause at a time, see _ci_failed.
_CI_FAILED = re.compile(
    # Each repeated word matches one way and at most three times: no exponential backtracking.
    r"\b(?:(?:unit|ui|integration|e2e|snapshot)\s+)?(?:tests?|specs?)"
    r"\s+(?:(?:is|are|was|were|still|keeps?|now|all)\s+){0,3}"
    r"(?:fail(?:ed|ing|s)?|broken)\b(?!\s+to\b)"
    r"|\b(?:CI|pipeline|(?:CI|status|required)\s+checks?)"
    r"\s+(?:(?:is|are|was|were|still|keeps?|now|all)\s+){0,3}"
    r"(?:fail(?:ed|ing|s)?|red|broken)\b(?!\s+to\b)"
    r"|\bbuild\s+(?:(?:is|was|still|now)\s+){0,3}(?:failed|failing|red|broken|broke)\b(?!\s+to\b)"
    r"|\bfailing\s+(?:(?:unit|ui|integration|e2e)\s+)?(?:tests?|specs?|CI|builds?|checks?)\b"
    r"|\bFAILED\s+\S+::"
    r"|(?<![\w.#])(?<!step )(?<!stage )(?<!job )[1-9]\d*\s+(?:tests?\s+)?failed\b(?!\s+to\b)",
    re.IGNORECASE,
)
# Right before it, in its own phrase (no ":" or "," between): the words that say none
# failed ("no unit tests failed", "none of the tests", "0 tests failed", "not failing
# tests") or that ask for one ("add a failing test").
_NONE_BEFORE = re.compile(
    r"(?:^|[^\w])(?:no|not|never|none\s+of\s+the|zero|0|without)\s+(?:\w+\s+){0,2}$"
    r"|\b(?:add|adding|write|writing)\s+(?:an?\s+|the\s+|some\s+)?$",
    re.IGNORECASE,
)
# Where the failure's own phrase starts: "No changelog and CI is red" negates the changelog.
# "or" joins one phrase ("No flaky or failing tests").
_PHRASE_BREAK = re.compile(
    r"[:,;]|\b(?:but|and|because|why|so|while|though|although|then)\b", re.IGNORECASE
)
# An "and" right before "failing tests" or "integration tests" joins words, not clauses.
_JOINED = re.compile(r"(?:failing|unit|ui|integration|e2e|snapshot)\b", re.IGNORECASE)
_TRAILING_AND = re.compile(r"\s+and\s+$", re.IGNORECASE)
# A resolution of the failure itself: "Fixed failing tests" (before it, not negated), "the
# failing test is now fixed", "CI was red, now green" (after it). Not "please fix", "needs
# a fix" or "even though I fixed the timeout".
_FIXED_BEFORE = re.compile(r"\b(?:fixed|resolved)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)
_FIXED_AFTER = re.compile(
    r"\b(?:(?:is|are|was|were)\s+(?:now\s+)?(?:fixed|resolved)"
    r"|now\s+(?:fixed|resolved|green|passing|passes|pass))\b",
    re.IGNORECASE,
)
MAX_REASON = 1000  # characters of a return reason searched
_SHOWN = 5
# A file path: segments of word characters, dots and dashes, and a last segment with an
# extension that starts with a letter ("src/app/routes.py", "routes.py"; not "v1.2", "e.g",
# "origin/main" or "example.com/x").
_PATH = re.compile(r"(?<![\w./:-])(?:[\w-]+(?:\.[\w-]+)*/)*[\w-]+\.[A-Za-z]\w*(?![\w/-])")
_NOT_FILES = re.compile(r"^(?:e\.g|i\.e|etc|vs)$|^[^/]+\.(?:com|org|net|io|dev)(?:/|$)", re.I)


@dataclass(frozen=True, slots=True)
class ReturnCauses:
    window: timedelta = timedelta(hours=24)
    min_returns: int = 5
    conflict_ratio_threshold: float = 0.5
    hot_file_min_conflicts: int = 3
    dominant_ratio: float = 0.5  # a cause this common is reported even without hot files
    expected_shared: tuple[str, ...] = ()  # glob patterns; ``*`` also matches ``/``
    cause_patterns: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = field(
        default_factory=lambda: _compile(DEFAULT_CAUSES)
    )
    test_names: tuple[re.Pattern[str], ...] = field(
        default_factory=lambda: tuple(re.compile(p) for p in DEFAULT_TEST_NAMES)
    )
    flaky_min_tasks: int = 2
    stage_categories: tuple[str, ...] = DEFAULT_STAGES
    ci_wording: bool = True  # CI's wording ("tests failed") names failing tests too
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        since = snapshot.now - self.window
        returns = [
            (task, r)
            for task in snapshot.tasks
            for r in task.returns
            if since <= r.at <= snapshot.now
        ]
        matched = [self._matched(r) for _, r in returns]  # once per return: they are long
        named = [self._tests(r, m) for (_, r), m in zip(returns, matched, strict=True)]
        flaky = self._flaky(returns, named)
        found = [self._flaky_alert(test, tasks) for test, tasks in sorted(flaky.items())]
        if len(returns) < self.min_returns:
            return found
        of = [
            self._cause(r, tests, m)
            for (_, r), tests, m in zip(returns, named, matched, strict=True)
        ]
        causes = Counter(
            FLAKY if tests & flaky.keys() else cause for tests, cause in zip(named, of, strict=True)
        )
        # Every return went through a stage (review): its findings are how the farm works.
        staged = all(r.category in self.stage_categories for _, r in returns)
        conflicts = [tr for tr, cause in zip(returns, of, strict=True) if cause == CONFLICT]
        return found + list(self._causes(snapshot, returns, conflicts, causes, staged))

    def _tests(self, r: Return, matched: str | bool | None = False) -> set[str]:
        """Tests the return says failed: a name next to failure wording ("failed", "flaky",
        "timed out", "red"), not a file path (``tests/test_models.py`` in a conflict), and
        never in a merge conflict's reason."""
        found = self._matched(r) if matched is False else matched
        if found == CONFLICT or r.category == CONFLICT:
            return set()
        reason = r.reason[:MAX_REASON].replace("\r\n", "\n")
        found = set()
        for p in self.test_names:
            for m in p.finditer(reason):
                name = m["test"]
                start, end = m.span("test")
                if (
                    not name
                    or reason[start - 1 : start] in ("/", "\\")
                    or re.match(r"\.\w", reason[end : end + 2])
                ):
                    continue  # a path, or a file name with its extension
                clause = _clause(reason, start, end, self.test_names)
                failed = _FAILED.search(_NO_FAILURE.sub(" ", clause))
                if failed and not _resolved_before(clause, name, self.test_names):
                    found.add(name)
        return found

    def _flaky(
        self, returns: Sequence[tuple[Task, Return]], named: Sequence[set[str]]
    ) -> dict[str, list[str]]:
        """Tests named in the returns of at least ``flaky_min_tasks`` tasks, with them."""
        by_test: dict[str, set[str]] = {}
        for (task, _), tests in zip(returns, named, strict=True):
            for test in tests:
                by_test.setdefault(test, set()).add(task.id)
        return {t: sorted(ids) for t, ids in by_test.items() if len(ids) >= self.flaky_min_tasks}

    def _flaky_alert(self, test: str, tasks: list[str]) -> Action:
        shown = ", ".join(tasks[:_SHOWN]) + (
            f" and {len(tasks) - _SHOWN} more" if len(tasks) > _SHOWN else ""
        )
        return Action(
            ActionKind.ALERT,
            NAME,
            f"Test {test} sent {len(tasks)} different tasks back in the last "
            f"{_span(self.window)} ({shown}): it fails other tasks' work, not only its own. "
            "Fix or quarantine it before more PRs are returned for it.",
            severity=Severity.WARNING,
            key=f"{NAME}:flaky:{test}",
            state=f"{NAME}:flaky:{test}",
            params={
                "finding": "flaky_test",
                "test": test,
                "tasks": tasks,
                **alert_params(f"Flaky test {test} sent {count(len(tasks), 'task')} back"),
            },
        )

    def _causes(
        self,
        snapshot: Snapshot,
        returns: Sequence[tuple[Task, Return]],
        conflicts: Sequence[tuple[Task, Return]],
        causes: Counter[str],
        staged: bool,
    ) -> Sequence[Action]:
        if len(conflicts) / len(returns) >= self.conflict_ratio_threshold:
            names = _names_index(snapshot.tasks)
            named = Counter(f for task, r in conflicts for f in self._files(task, r, names))
            hot = sorted(f for f, n in named.items() if n >= self.hot_file_min_conflicts)
            if hot:
                return [self._alert(snapshot, hot, named, causes, len(returns), len(conflicts))] + [
                    self._comment(t, touched, named)
                    for t in snapshot.tasks
                    if t.status is Status.IN_PROGRESS and (touched := self._touching(t, hot, names))
                ]
        cause, n = max(causes.items(), key=lambda c: (c[1], c[0]))
        if cause in (OTHER, FLAKY) or n / len(returns) < self.dominant_ratio:
            return []  # flaky tests have alerts of their own
        if staged and cause in {_findings(s) for s in self.stage_categories}:
            return []  # every return went through a stage: how the farm works, no cause
        return [self._dominant(cause, n, causes, len(returns))]

    def cause(self, r: Return) -> str:
        """What sent the task back: what the reason says first (a review that bounced a
        task for a merge conflict is a conflict, one for a failing test a failing test),
        then what was found at the stage it names (``review_findings``), then the
        platform's category."""
        found = self._matched(r)
        return self._cause(r, self._tests(r, found), found)

    def _cause(self, r: Return, tests: set[str], found: str | None) -> str:
        if found is not None:
            return found  # merge_conflict and the configured categories, in that order
        if tests or (self.ci_wording and _ci_failed(r.reason[:MAX_REASON])):
            return FAILING
        if r.category in self.stage_categories:
            return _findings(r.category)
        return r.category or OTHER

    def _matched(self, r: Return) -> str | None:
        """The first ``cause_patterns`` category whose pattern matches the reason."""
        reason = r.reason[:MAX_REASON]
        for category, patterns in self.cause_patterns:
            if any(p.search(reason) for p in patterns):
                return category
        return None

    def _shared(self, path: str) -> bool:
        return any(fnmatchcase(path, p) for p in self.expected_shared)

    def _files(self, task: Task, r: Return, names: dict[str, set[str]]) -> set[str]:
        """The files a conflict names: paths in its reason, a bare file name resolved to the
        one file of that name the tasks report (this task's first)."""
        mine = {_clean(f) for f in task.files}
        found: set[str] = set()
        for token in _PATH.findall(r.reason[:MAX_REASON]):
            token = _clean(token)
            if _NOT_FILES.search(token):
                continue
            if "/" in token:
                found.add(token)
                continue
            own = {f for f in mine if PurePosixPath(f).name == token}
            known = own or names.get(token, set())
            found |= known if len(known) == 1 else {token}
        return {f for f in found if not self._shared(f)}

    def _touching(self, task: Task, hot: Sequence[str], names: dict[str, set[str]]) -> list[str]:
        """Hot files the task changes; a hot bare file name matches the one file of that
        name."""
        mine = {_clean(f) for f in task.files}
        return [
            f
            for f in hot
            if f in mine or ("/" not in f and len(names.get(f, ())) == 1 and names[f] & mine)
        ]

    def _alert(
        self,
        snapshot: Snapshot,
        hot: list[str],
        named: Counter[str],
        causes: Counter[str],
        total: int,
        conflicts: int,
    ) -> Action:
        shares = {c: round(n / total, 2) for c, n in sorted(causes.items())}
        span = _span(self.window)
        listed = ", ".join(f"{f} ({named[f]})" for f in hot[:_SHOWN])
        more = f" +{len(hot) - _SHOWN} more" if len(hot) > _SHOWN else ""
        return Action(
            ActionKind.ALERT,
            NAME,
            f"{conflicts} of {total} task returns in the last {span} "
            f"({round(100 * conflicts / total)}%) were merge conflicts, concentrated on "
            f"{listed}{more}. Give each of these files one owner, split it, or serialize "
            "the tasks that touch it; rule 12 (conflict_warning) warns live tasks that share "
            "files. List files that many tasks are meant to edit in expected_shared.",
            severity=Severity.WARNING,
            key=f"{NAME}:hot-files:{','.join(hot)}",  # a new hot file is news
            state=f"{NAME}:hot-files",
            params={
                "finding": "hot_files",
                "hot_files": hot,
                "conflicts_by_file": {f: named[f] for f in hot},
                "returns": total,
                "conflicts": conflicts,
                "shares": shares,
                "window_minutes": int(self.window.total_seconds() // 60),
                **alert_params(
                    f"Merge conflicts concentrate on {hot[0]}"
                    + (f" and {count(len(hot) - 1, 'more file')}" if len(hot) > 1 else "")
                ),
            },
        )

    def _dominant(self, cause: str, n: int, causes: Counter[str], total: int) -> Action:
        """One cause behind most returns, with no file to act on: report the shares."""
        shares = {c: round(m / total, 2) for c, m in sorted(causes.items())}
        by_cause = ", ".join(
            f"{_label(c)} {round(100 * m / total)}%"
            for c, m in sorted(causes.items(), key=lambda c: (-c[1], c[0]))
        )
        advice = (
            "They are spread over many files, so no file needs an owner: keep tasks small "
            "and rebase them early."
            if cause == CONFLICT
            else f"Look at what the '{cause}' returns have in common."
        )
        return Action(
            ActionKind.ALERT,
            NAME,
            f"{n} of {total} task returns in the last {_span(self.window)} "
            f"({round(100 * n / total)}%) "
            f"were {_label(cause)} (by cause: {by_cause}). {advice}",
            severity=Severity.INFO,
            key=f"{NAME}:dominant:{cause}",
            state=f"{NAME}:dominant:{cause}",
            params={
                "finding": "dominant_cause",
                "dominant_cause": cause,
                "returns": total,
                "shares": shares,
                "window_minutes": int(self.window.total_seconds() // 60),
                **alert_params(
                    f"Top cause of task returns: {_label(cause)} "
                    f"({round(100 * n / total)}% of {total})"
                ),
            },
        )

    def _comment(self, task: Task, files: list[str], named: Counter[str]) -> Action:
        listed = ", ".join(f"{f} ({named[f]} conflicts)" for f in files)
        return Action(
            ActionKind.COMMENT,
            NAME,
            f"Heads-up: this task changes {listed}, the files most task returns on this "
            "farm conflicted on lately. Keep the edits there small, rebase early, and merge "
            "as soon as you can.",
            task_id=task.id,
            key=f"{NAME}:{task.id}:{','.join(files)}",
            once=True,
            params={"hot_files": files},
        )


def _label(cause: str) -> str:
    """A cause as the alerts write it: "merge conflict", "review findings"."""
    return cause.replace("_", " ")


def _ci_failed(reason: str) -> bool:
    """CI's wording for failing tests in a clause that does not say none failed ("no unit
    tests failed", "0 tests failed") or that it was fixed ("Fixed failing tests", "CI was
    red, now green")."""
    for clause in _CLAUSE_END.split(reason.replace("\r\n", "\n")):
        for m in _CI_FAILED.finditer(clause):
            before = clause[: m.start()]
            if _JOINED.match(m.group()):  # "No unit and integration tests failed"
                before = _TRAILING_AND.sub(" ", before)
            phrase = _PHRASE_BREAK.split(before)[-1][-40:]
            fixed = _FIXED_BEFORE.search(phrase)
            if fixed and _NEGATED.search(phrase[: fixed.start()]):
                return True  # "Never fixed failing tests": the negation is of the fix
            if fixed or _NONE_BEFORE.search(phrase) or _FIXED_AFTER.search(clause, m.end()):
                continue  # "Fixed failing tests", "no unit tests failed", "is now fixed"
            return True
    return False


def _findings(stage: str) -> str:
    """The cause of a return at ``stage`` that names nothing else: what was found there."""
    return f"{stage}_findings"


def _names_index(tasks: Sequence[Task]) -> dict[str, set[str]]:
    """File name -> the paths of that name the tasks report."""
    index: dict[str, set[str]] = {}
    for t in tasks:
        for f in t.files:
            path = _clean(f)
            index.setdefault(PurePosixPath(path).name, set()).add(path)
    return index


def _span(d: timedelta) -> str:
    minutes = int(d.total_seconds() // 60)
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


def _clean(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def _compile(
    raw: Mapping[str, Sequence[str]],
) -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    return tuple(
        (category, tuple(re.compile(p, re.IGNORECASE) for p in patterns))
        for category, patterns in raw.items()
    )


def _duration(value: Any, name: str) -> timedelta:
    try:
        d = parse_duration(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name}: {exc}") from None
    if d <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return d


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@register(NAME)
def build(config: dict[str, Any]) -> ReturnCauses:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "window" in config:
        kwargs["window"] = _duration(config.pop("window"), "window")
    for key in ("min_returns", "hot_file_min_conflicts"):
        if key in config:
            kwargs[key] = _positive_int(config.pop(key), key)
    for key in ("conflict_ratio_threshold", "dominant_ratio"):
        if key in config:
            ratio = config.pop(key)
            if isinstance(ratio, bool) or not isinstance(ratio, int | float) or not 0 < ratio <= 1:
                raise ValueError(f"{key} must be a number in (0, 1]")
            kwargs[key] = float(ratio)
    if "expected_shared" in config:
        patterns = config.pop("expected_shared")
        if not isinstance(patterns, list | tuple) or not all(
            isinstance(p, str) and p for p in patterns
        ):
            raise ValueError("expected_shared must be a list of non-empty glob patterns")
        kwargs["expected_shared"] = tuple(patterns)
    if "flaky_min_tasks" in config:
        n = _positive_int(config.pop("flaky_min_tasks"), "flaky_min_tasks")
        if n < 2:
            raise ValueError(
                "flaky_min_tasks must be at least 2 (one task's own test is not flaky)"
            )
        kwargs["flaky_min_tasks"] = n
    if "test_names" in config:
        raw_tests = config.pop("test_names")
        if not isinstance(raw_tests, list) or not all(isinstance(p, str) and p for p in raw_tests):
            raise ValueError("test_names must be a list of regular expressions")
        try:
            compiled = tuple(re.compile(p) for p in raw_tests)
        except re.error as exc:
            raise ValueError(f"test_names: invalid pattern: {exc}") from None
        if any("test" not in p.groupindex for p in compiled):
            raise ValueError("test_names: every pattern needs a named group 'test'")
        kwargs["test_names"] = compiled
    if "ci_wording" in config:
        flag = config.pop("ci_wording")
        if not isinstance(flag, bool):
            raise ValueError("ci_wording must be true or false")
        kwargs["ci_wording"] = flag
    if "stage_categories" in config:
        stages = config.pop("stage_categories")
        if not isinstance(stages, list | tuple) or not all(
            isinstance(s, str) and s.strip() for s in stages
        ):
            raise ValueError("stage_categories must be a list of platform categories")
        kwargs["stage_categories"] = tuple(s.strip() for s in stages)
    if "cause_patterns" in config:
        raw = config.pop("cause_patterns")
        if not isinstance(raw, Mapping) or not all(
            isinstance(k, str)
            and k
            and isinstance(v, list | tuple)
            and all(isinstance(p, str) and p for p in v)
            for k, v in raw.items()
        ):
            raise ValueError("cause_patterns must map categories to lists of regular expressions")
        try:
            kwargs["cause_patterns"] = _compile({**DEFAULT_CAUSES, **raw})
        except re.error as exc:
            raise ValueError(f"cause_patterns: invalid pattern: {exc}") from None
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return ReturnCauses(**kwargs)


def _clause(reason: str, start: int, end: int, names: Sequence[re.Pattern[str]]) -> str:
    """The clause of ``reason`` around ``reason[start:end]``. A name alone on its line
    belongs to the next line when that line is the failure ("test_login_timeout" over
    "failed 3 times"), and to the header of its list ("Tests failed:" over "- test_a")."""
    heads = [m.end() for m in _CLAUSE_END.finditer(reason, 0, start)]
    head = heads[-1] if heads else 0
    tail = _CLAUSE_END.search(reason, end)
    stop = tail.start() if tail else len(reason)
    clause = reason[head:stop]
    lines = reason.split("\n")
    at = reason.count("\n", 0, start)
    alone = not clause.replace(reason[start:end], "").strip(_BULLET)
    if alone:
        below = lines[at + 1] if at + 1 < len(lines) else ""
        if (
            _FAILED.match(below.lstrip(_BULLET))
            and not below.rstrip().endswith(":")  # the next list's header
            and not any(p.search(below) for p in names)
        ):
            return clause + "\n" + below
    elif _FAILED.search(clause) or not _ITEM.match(lines[at]):
        return clause  # the name has words around it, its own verdict or none
    # A list item: up through the list to its header ("Tests failed:", "Flaky:").
    blank = False
    for line in reversed(lines[:at]):
        if not line.strip():
            if blank:
                break
            blank = True  # one blank line between a header and its list
            continue
        if line.rstrip().endswith(":") and not _ITEM.match(line):
            fixed = _FIXED.search(line) and not _FAILED.search(line)  # "Fixed tests:"
            return clause if fixed else line + "\n" + clause  # the header, then the item
        if not _ITEM.match(line) and not any(p.fullmatch(line.strip(_BULLET)) for p in names):
            break  # a line of prose: no list
    return clause


def _resolved_before(clause: str, name: str, names: Sequence[re.Pattern[str]]) -> bool:
    """The clause says the failure was fixed before it names the test, and the test with
    it ("Fixed TypeError in test_parse"; not "wasn't fixed", nor "after I fixed the
    timeout, test_login failed")."""
    named = re.search(rf"\b{re.escape(name)}\b", clause)
    at = named.start() if named else len(clause)
    for m in _RESOLVED.finditer(clause, 0, at):
        between = clause[m.end() : at]
        if _NEGATED.search(clause[: m.start()]):
            continue  # not fixed
        rest = _NO_FAILURE.sub(" ", clause[at + len(name) :])
        if ("," in between or " and " in between) and _FAILED.search(rest):
            continue  # "fixed the timeout and test_login failed": the fix was of another thing
        if any(p.search(between) for p in names):
            continue  # it fixed another test
        return True
    return False
