"""Review economy (#298): where full review almost never finds anything.

On a farm, reviewers can spend most of the tokens (62-84% on one farm's first token
review). This analysis measures review per repository and kind of change, and recommends a
lighter path for the kinds where a full review almost never finds anything. Kelpie only
recommends: it never changes a review policy.

**A review** is a session of a review role (``[report] review_roles``) on a task the board
shows that ended before the report's end. Its tokens are the counted model tokens of its
spans. It **found something** when the task was sent back (any
:class:`~kelpie.core.model.Return`, whoever sent it: a conservative count) after the review
began and before the next review of the task began, or when another review session
followed it (ping-pong; a review split over two sessions counts as one that found
something). The latest review of a task still open counts only once it ended ``settle``
ago: until then it has not had its chance to be followed by a return.

**The kind** of a task comes from the files it changed (``Task.files``) and its labels,
first match wins: a dependency bump (only manifests and lockfiles), generated files, docs
only, tests only, a small change (at most ``small_files`` files of any kind, code included;
the board gives no line counts), else code. A task whose files the board does not know is a
dependency bump when it has a dependency label, else of an unknown kind.

**A recommendation** goes to a light kind (never code or unknown) with at least
``min_sample`` reviews and findings in at most ``max_findings_share`` of them: the reviews,
the ones with findings, and the tokens a lighter path would save (``light_share``: what it
costs next to a full review), per day of the reviews seen. Below the sample, a kind under
the threshold is listed as "direction only". A kind above the threshold never gets one.
Pure: no I/O, no clock reads.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from fnmatch import fnmatchcase

from kelpie.core.model import Session, Span, Status, Task
from kelpie.core.spans import SpanKey, TokenUsage, counted_llm, last_at, span_key, usage

DEPENDENCY = "dependency bump"
GENERATED = "generated files"
DOCS = "docs only"
TESTS = "tests only"
SMALL = "small change"
CODE = "code"
UNKNOWN = "unknown"
LIGHT_KINDS = (DEPENDENCY, GENERATED, DOCS, TESTS, SMALL)
KIND_ORDER = (*LIGHT_KINDS, CODE, UNKNOWN)
LIGHTER_PATH = {
    DEPENDENCY: "green CI plus a changelog and advisory check, no full read",
    GENERATED: "review the generator's input, not its output",
    DOCS: "a single pass on a cheaper model for links, spelling and facts",
    TESTS: "green CI plus a pass on a cheaper model",
    SMALL: "a diff-only pass on a cheaper model",
}
NO_REPO = "(no repository)"
CLOSED = frozenset({Status.DONE, Status.ACCEPTED, Status.CANCELLED})


@dataclass(frozen=True, slots=True)
class ReviewEconomySettings:
    """``[report.sections.review_economy]``."""

    min_sample: int = 20  # reviews of a kind before it gets a recommendation
    settle: timedelta = timedelta(days=1)  # a task's last review counts once this old
    max_findings_share: float = 0.05  # findings in at most this share of its reviews
    light_share: float = 0.3  # what the lighter path costs next to a full review
    small_files: int = 2  # at most this many changed files: a small change
    docs_globs: tuple[str, ...] = (
        "*.md",
        "*.rst",
        "*.adoc",
        "README*",
        "LICENSE*",
        "CHANGELOG*",
        "CONTRIBUTING*",
        "docs/*.png",
        "docs/*.svg",
        "*/docs/*.png",
        "*/docs/*.svg",
    )
    tests_globs: tuple[str, ...] = (
        "tests/*",
        "*/tests/*",
        "test/*",
        "*/test/*",
        "*_test.*",
        "test_*",
        "*/test_*",
        "*.test.*",
        "*.spec.*",
    )
    generated_globs: tuple[str, ...] = (
        "*_pb2.py",
        "*_pb2_grpc.py",
        "*.pb.go",
        "*.generated.*",
        "*/generated/*",
        "generated/*",
        "*.min.js",
        "*.snap",
    )
    dependency_globs: tuple[str, ...] = (
        "uv.lock",
        "*/uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements*.txt",
        "package-lock.json",
        "*/package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "go.sum",
        "go.mod",
        "Cargo.lock",
        "Gemfile.lock",
        "Podfile.lock",
        "Package.resolved",
        "*/Package.resolved",
        "pyproject.toml",
        "package.json",
        "*/package.json",
        "Cargo.toml",
        "Gemfile",
        "Podfile",
    )
    dependency_labels: tuple[str, ...] = ("dependencies", "deps")


@dataclass(frozen=True, slots=True)
class Review:
    task_id: str
    repo: str
    kind: str
    session_id: str
    at: datetime
    tokens: int
    found: bool
    usage: tuple[TokenUsage, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewClass:
    """The reviews of one repository and kind."""

    repo: str
    kind: str
    reviews: tuple[Review, ...]

    @property
    def n(self) -> int:
        return len(self.reviews)

    @property
    def k(self) -> int:
        return sum(r.found for r in self.reviews)

    @property
    def tasks(self) -> tuple[str, ...]:
        return tuple(sorted({r.task_id for r in self.reviews}))

    @property
    def rounds(self) -> float:
        """Reviews per task."""
        return self.n / len(self.tasks) if self.reviews else 0.0

    @property
    def tokens(self) -> int:
        return sum(r.tokens for r in self.reviews)

    @property
    def share(self) -> float:
        return self.k / self.n if self.n else 0.0


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the analysis says about a class: ``recommend`` (enough evidence), ``direction``
    (under the threshold, too few reviews), or neither."""

    cls: ReviewClass
    recommend: bool
    direction: bool
    saved_tokens_per_day: float
    days: float

    @property
    def path(self) -> str:
        return LIGHTER_PATH.get(self.cls.kind, "")


def _matches(path: str, globs: Sequence[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(path, g) or fnmatchcase(name, g) for g in globs)


def kind_of(task: Task, cfg: ReviewEconomySettings) -> str:
    """The kind of change ``task`` is (see the module docs)."""
    files = [f for f in task.files if f]
    if not files:  # a dependency label stands for files only when none are known
        labels = {label.casefold() for label in task.labels}
        return DEPENDENCY if labels & {d.casefold() for d in cfg.dependency_labels} else UNKNOWN
    for kind, globs in (
        (DEPENDENCY, cfg.dependency_globs),
        (GENERATED, cfg.generated_globs),
        (DOCS, cfg.docs_globs),
        (TESTS, cfg.tests_globs),
    ):
        if all(_matches(f, globs) for f in files):
            return kind
    return SMALL if len(files) <= cfg.small_files else CODE


def reviews(
    tasks: Iterable[Task],
    sessions: Iterable[Session],
    spans: Sequence[Span],
    roles: Mapping[str, str],
    review_roles: Sequence[str],
    end: datetime,
    cfg: ReviewEconomySettings,
    counted: Mapping[SpanKey, str | None] | None = None,
) -> list[Review]:
    """Every review the board shows that began before ``end``, with its tokens and
    whether it found something."""
    by_id = {t.id: t for t in tasks}
    mine: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        if roles.get(s.id) in review_roles and s.task_id in by_id and s.started_at < end:
            mine[s.task_id].append(s)
    spans = [s for s in spans if last_at(s) < end]  # the day as it was, also re-run later
    if counted is None:
        counted = counted_llm({span_key(s): s for s in spans}, spans)
    rows: dict[str, list[TokenUsage]] = defaultdict(list)
    wanted = {s.id for found in mine.values() for s in found}
    for row in usage([s for s in spans if s.session_id in wanted], counted):
        if row.session_id is not None:
            rows[row.session_id].append(row)
    out = []
    for task_id, found in sorted(mine.items()):
        task = by_id[task_id]
        kind, repo = kind_of(task, cfg), task.repo or NO_REPO
        found.sort(key=lambda s: (s.started_at, s.id))
        closed = task.status in CLOSED and task.status_since < end
        for i, s in enumerate(found):
            nxt = found[i + 1].started_at if i + 1 < len(found) else None
            sent_back = any(
                s.started_at <= r.at < end and (nxt is None or r.at < nxt) for r in task.returns
            )
            if s.ended_at is None or s.ended_at > end:
                continue  # still running: it has not had its chance to find something
            if nxt is None and not sent_back and not closed and end - s.ended_at < cfg.settle:
                continue  # the latest review, too recent to tell it was clean
            mine_rows = tuple(rows.get(s.id, ()))
            out.append(
                Review(
                    task_id=task_id,
                    repo=repo,
                    kind=kind,
                    session_id=s.id,
                    at=s.started_at,
                    tokens=sum(r.input_tokens + r.output_tokens for r in mine_rows),
                    found=sent_back or nxt is not None,
                    usage=mine_rows,
                )
            )
    return out


def classes(found: Iterable[Review]) -> list[ReviewClass]:
    groups: dict[tuple[str, str], list[Review]] = defaultdict(list)
    for r in found:
        groups[(r.repo, r.kind)].append(r)
    return [
        ReviewClass(repo, kind, tuple(rs))
        for (repo, kind), rs in sorted(
            groups.items(), key=lambda kv: (kv[0][0], KIND_ORDER.index(kv[0][1]))
        )
    ]


def judge(cls: ReviewClass, end: datetime, cfg: ReviewEconomySettings) -> Verdict:
    """Whether ``cls`` gets a recommendation, a direction, or neither; the tokens a day a
    lighter path would save, over the days its reviews span (at least one)."""
    days = 1.0
    if cls.reviews:
        first = min(r.at for r in cls.reviews)
        days = max((end - first) / timedelta(days=1), 1.0)
    saved = cls.tokens * (1 - cfg.light_share) / days
    light = cls.kind in LIGHT_KINDS and cls.n > 0 and cls.share <= cfg.max_findings_share
    enough = cls.n >= max(cfg.min_sample, 1)
    return Verdict(cls, light and enough, light and not enough, saved, days)
