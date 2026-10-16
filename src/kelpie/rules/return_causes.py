"""Return causes — why tasks come back (incident 7).

46 of 63 task returns on a live farm were merge conflicts, nearly all in a few shared files
(a route registry, a central types module): agents working in parallel kept editing them,
and every merge undid someone's work. This rule groups the returns of the last ``window``
(24h) by cause and turns a dominant cause into a recommendation.

**Causes.** The first of ``cause_patterns`` (category -> regular expressions,
case-insensitive; trusted config, run on the first 1000 characters of each reason) that
matches a return's reason (``Task.returns``) names its cause, else the
category its platform gave it (Hermes: ``review`` for changes requested or a reopened
review), else ``other``. By default only
``merge_conflict`` is recognised, in git's wording ("merge conflict in", "CONFLICT
(content)", "conflicts with main"): review prose such as "this conflicts with the design"
is not a conflict. The share of each cause is computed over every return in the window, of finished
tasks too.

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
with the shares: there is no file to act on.
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

NAME = "return_causes"
CONFLICT = "merge_conflict"
OTHER = "other"
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
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        since = snapshot.now - self.window
        returns = [
            (task, r)
            for task in snapshot.tasks
            for r in task.returns
            if since <= r.at <= snapshot.now
        ]
        if len(returns) < self.min_returns:
            return []
        causes = Counter(self.cause(r) for _, r in returns)
        conflicts = [(task, r) for task, r in returns if self.cause(r) == CONFLICT]
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
        if cause == OTHER or n / len(returns) < self.dominant_ratio:
            return []
        return [self._dominant(cause, n, causes, len(returns))]

    def cause(self, r: Return) -> str:
        """What the reason says first (a review that bounced a task for a merge conflict
        is a conflict), then the platform's category."""
        reason = r.reason[:MAX_REASON]
        for category, patterns in self.cause_patterns:
            if any(p.search(reason) for p in patterns):
                return category
        return r.category or OTHER

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
            },
        )

    def _dominant(self, cause: str, n: int, causes: Counter[str], total: int) -> Action:
        """One cause behind most returns, with no file to act on: report the shares."""
        shares = {c: round(m / total, 2) for c, m in sorted(causes.items())}
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
            f"were '{cause}'. {advice}",
            severity=Severity.INFO,
            key=f"{NAME}:dominant:{cause}",
            state=f"{NAME}:dominant:{cause}",
            params={
                "finding": "dominant_cause",
                "dominant_cause": cause,
                "returns": total,
                "shares": shares,
                "window_minutes": int(self.window.total_seconds() // 60),
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
