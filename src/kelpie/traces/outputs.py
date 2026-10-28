"""Huge tool outputs: find the commands that flood the context, rank them, recommend a
filter that already exists, and verify its effect before/after.

Pure: no I/O and no clock. Input is tool spans as ingest and hooks record them
(:mod:`kelpie.traces.semconv`): never the output itself, only its size.

**Size.** ``kelpie.tool.output_tokens`` when the platform reports it (Claude Code), else
``kelpie.tool.output_chars`` (the result's length, from OTLP or the Hermes hook) divided
by :data:`CHARS_PER_TOKEN`; such numbers are marked ``estimated``. Tool spans with
neither are not counted. The size is what the call put into the context once; every later
model call of the session re-reads it until the context is compacted, so savings computed
from it are a lower bound.

**Grouping.** Calls are grouped per role and *key*: the command prefix
(``kelpie.tool.command_prefix`` from OTLP, or the prefix of the hook's redacted
``command``; see :func:`kelpie.traces.command.command_prefix`), else ``tool:<name>`` for
tools that are not shell commands (file reads, searches). ``rtk git status`` groups with
``git status``; calls that already run through a filter (``kelpie.tool.filter``) are
counted apart, so a group's recommendation is about its unfiltered calls only, and a group
whose latest call is filtered has adopted a filter and gets none.

**Recommendation.** A group whose unfiltered calls average at least ``threshold_tokens``
over at least ``min_calls`` calls gets the first filter of the table (:class:`Filter`,
``[[traces.outputs.filters]]``) that matches its key and supports its platform: RTK
(github.com/rtk-ai/rtk) for the command families it covers, on the agent platforms it hooks
into; otherwise e.g. ``xcbeautify`` for ``xcodebuild``, then errors only and a tail for any
other command. Tools that are not shell commands get advice for their kind (issue #197: on a
live farm built-in tools were the real offenders, an image analysis at 25-56k tokens, a card
view at 6-14k re-read many times a session): images downscaled or cropped, a record read in
a compact view and not again, a file in a range, and any other tool asked for less and not
called again for the same thing. Kelpie does not filter anything itself. Estimated savings
are the group's unfiltered tokens times the filter's expected ``reduction`` (an assumption
until verified).
A group above the threshold on ``repeat_days`` different days is a repeated offender: the
owner of the role's instructions (``owners``) should add the filter there.

**Before/after.** :func:`before_after` compares tokens per call of one key before and
after a moment (a fix noted by an operator). :func:`filter_introductions` finds those
moments by itself: the first filtered call of a key that was called unfiltered before.
Each side covers ``window`` (a day by default); a side with fewer than ``min_sample``
calls is a small sample and labelled as such.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from fnmatch import fnmatchcase

from kelpie.core.model import OutputOffender, Snapshot, Span, SpanKind
from kelpie.core.spans import Attribution
from kelpie.traces.command import command_prefix, detect_filter
from kelpie.traces.semconv import (
    COMMAND_ATTRIBUTE,
    FILTER_ATTRIBUTE,
    OUTPUT_CHARS_ATTRIBUTE,
    OUTPUT_TOKENS_ATTRIBUTE,
)

CHARS_PER_TOKEN = 4
HOOK_COMMAND = "command"  # redacted command on hook spans (Hermes spool)
TOOL_KEY = "tool:"
DEFAULT_THRESHOLD_TOKENS = 4000
DEFAULT_MIN_CALLS = 3
DEFAULT_REPEAT_DAYS = 2
DEFAULT_WINDOW = timedelta(days=1)
DEFAULT_MIN_SAMPLE = 5

# Agent platforms RTK hooks into (its README); names as Kelpie sees them: an adapter name or
# an OTLP service.name, lower case with "_" and " " read as "-".
RTK_PLATFORMS = (
    "claude-code",
    "hermes",
    "codex",
    "openclaw",
    "cursor",
    "gemini-cli",
    "copilot",
    "opencode",
    "windsurf",
    "cline",
    "roo-code",
    "kilo-code",
)
# Tools that return a file's content (case varies by platform: Read, read_file, view).
# Globs are on word boundaries ("_", "." or a capital), so "revision" is no vision tool and
# "thread" no read.
FILE_TOOLS = (
    *("tool:read*", "tool:*_read*", "tool:*-read*", "tool:*.read*", "tool:Read*", "tool:*Read"),
    *("tool:*Read[A-Z]*", "tool:view*", "tool:View*", "tool:cat"),
)
# Tools that look at an image (a screenshot, a rendered page). What they put into the context
# is the image, or an analysis of it (Hermes' vision_analyze): a smaller image costs less
# either way, by how much is a guess until verified.
IMAGE_TOOLS = (
    *("tool:vision*", "tool:*_vision*", "tool:*-vision*", "tool:*Vision*", "tool:*screenshot*"),
    *("tool:*Screenshot*", "tool:image", "tool:image_*", "tool:*_image", "tool:*-image"),
    *("tool:*Image", "tool:analyze_image*"),
)
# Tools that show a whole record (a card, an issue, a run): e.g. Hermes' kanban_show.
RECORD_TOOLS = ("tool:*_show", "tool:show_*", "tool:*Show")
# The agent platforms' shell tools (Claude Code Bash, Hermes terminal, Cursor
# run_terminal_cmd, Gemini CLI run_shell_command, Cline execute_command, Codex local_shell,
# ...): a call of one without a command prefix (commands not recorded, or one Kelpie cannot
# parse) groups every command the role ran, which no advice fits; the catch-all ``tool:*``
# row leaves them alone.
SHELL_TOOLS = (
    *("tool:Bash", "tool:bash", "tool:exec", "tool:exec_*", "tool:*_exec"),
    *("tool:*shell*", "tool:*Shell*", "tool:*terminal*", "tool:*Terminal*"),
    *("tool:*_command", "tool:*Command", "tool:*_cmd", "tool:*Cmd"),
)
# Platforms a span's service.name may name (other names are custom: platform unknown).
KNOWN_PLATFORMS = frozenset({*RTK_PLATFORMS, "paperclip"})
# Command families RTK filters (its README lists 100+ commands in these families).
RTK_COMMANDS = (
    "git",
    "gh",
    "cargo",
    "npm",
    "pnpm",
    "yarn",
    "npx",
    "vitest",
    "jest",
    "tsc",
    "playwright",
    "prisma",
    "ruff",
    "pytest",
    "python* -m pytest",
    "mypy",
    "pip",
    "go",
    "golangci-lint",
    "rspec",
    "rubocop",
    "dotnet",
    "gradle",
    "gradlew",
    "mvn",
    "docker",
    "kubectl",
    "aws",
    "terraform",
    "ls",
    "find",
    "grep",
    "rg",
    "cat",
    "tree",
)


@dataclass(frozen=True, slots=True)
class Filter:
    """One row of the filter table. ``commands`` are globs over a group's key and over its
    executable (``git``, ``python* -m pytest``); ``*`` is any shell command and ``tool:*``
    any other tool. ``platforms`` limits the filter to agent platforms it works with (None:
    any). ``reduction`` is the expected share of output tokens removed."""

    name: str
    how: str
    commands: tuple[str, ...]
    reduction: float
    platforms: tuple[str, ...] | None = None
    # What to do, for a title ("Downscale images"): advice that is not a filter to put a
    # command through; None for a filter.
    advice: str | None = None

    def matches(self, key: str) -> bool:
        if key.startswith(TOOL_KEY):
            return any(
                c.startswith(TOOL_KEY)
                and fnmatchcase(key, c)
                and not (c == "tool:*" and any(fnmatchcase(key, g) for g in SHELL_TOOLS))
                for c in self.commands
            )
        exe = key.split(" ", 1)[0]
        return any(
            not c.startswith(TOOL_KEY) and (fnmatchcase(key, c) or fnmatchcase(exe, c))
            for c in self.commands
        )

    def supports(self, platform: str | None) -> bool | None:
        """True or False when the platform is known, None when it is not."""
        if self.platforms is None:
            return True
        if platform is None:
            return None
        return platform in self.platforms


DEFAULT_FILTERS: tuple[Filter, ...] = (
    Filter(
        name="rtk",
        how=(
            "install RTK (github.com/rtk-ai/rtk) and its hook for this platform (`rtk init`): "
            "the agent's command runs as `rtk <command>` and returns failures and a summary"
        ),
        commands=RTK_COMMANDS,
        reduction=0.6,  # RTK claims 60-90% on common dev commands: the low end
        platforms=RTK_PLATFORMS,
    ),
    Filter(
        name="xcbeautify",
        how="pipe the build through xcbeautify: `xcodebuild ... 2>&1 | xcbeautify --quiet`",
        commands=("xcodebuild",),
        reduction=0.7,
    ),
    Filter(
        name="errors-only",
        how=(
            "keep errors and the summary only, e.g. `<command> 2>&1 | grep -E "
            "'error|warning|FAIL' | tail -n 100`"
        ),
        commands=("*",),
        reduction=0.5,
    ),
    Filter(
        name="downscale-images",
        how=(
            "downscale or crop an image before analysing it (at most 1024 px on its long "
            "side, only the region that matters), and analyse each image once"
        ),
        commands=IMAGE_TOOLS,
        reduction=0.5,
        advice="Downscale images",
    ),
    Filter(
        name="range",
        how="read a line range or search the file instead of returning it whole",
        commands=FILE_TOOLS,
        reduction=0.5,
        advice="Read a range",
    ),
    Filter(
        name="compact-view",
        how=(
            "read only what the step needs (a compact or summary view where the tool has "
            "one), and keep what was read instead of reading the same record again in the "
            "same session"
        ),
        commands=RECORD_TOOLS,
        reduction=0.5,
        advice="Read a compact view, once",
    ),
    Filter(
        name="ask-less",
        how=(
            "ask the tool for less (a limit, a range, a summary), and keep its result instead "
            "of calling it again for the same thing in the same session"
        ),
        commands=("tool:*",),
        reduction=0.3,
        advice="Ask for less",
    ),
)


@dataclass(frozen=True, slots=True)
class OutputSettings:
    """``[traces.outputs]``."""

    threshold_tokens: int = DEFAULT_THRESHOLD_TOKENS  # mean output tokens per call
    min_calls: int = DEFAULT_MIN_CALLS
    repeat_days: int = DEFAULT_REPEAT_DAYS
    window: timedelta = DEFAULT_WINDOW  # each side of a before/after comparison
    min_sample: int = DEFAULT_MIN_SAMPLE  # fewer calls on a side: a small sample
    filters: tuple[Filter, ...] = DEFAULT_FILTERS
    owners: Mapping[str, str] = field(default_factory=dict)  # role -> instructions owner


@dataclass(frozen=True, slots=True)
class Call:
    """One measured tool call."""

    at: datetime
    role: str | None
    key: str
    tokens: int
    estimated: bool
    filter: str | None
    platform: str | None
    task_id: str | None
    session_id: str | None


@dataclass(frozen=True, slots=True)
class OutputGroup:
    """Tool calls of one role and key. Totals cover unfiltered calls; ``filtered_*`` the
    calls that already ran through a filter."""

    role: str | None
    key: str
    calls: int
    tokens: int
    max_tokens: int
    estimated: bool
    filtered_calls: int = 0
    filtered_tokens: int = 0
    filters: tuple[str, ...] = ()
    platform: str | None = None
    tasks: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    hot_days: tuple[date, ...] = ()  # days whose unfiltered calls averaged >= threshold
    adopted: bool = False  # the latest call already ran through a filter

    @property
    def mean(self) -> float:
        return self.tokens / self.calls if self.calls else 0.0


@dataclass(frozen=True, slots=True)
class Recommendation:
    group: OutputGroup
    filter: Filter
    saved_tokens: int  # estimate: unfiltered tokens x expected reduction
    repeated: bool  # above the threshold on repeat_days days
    owner: str | None  # who owns the role's instructions, if configured
    platform_known: bool  # False: the filter was chosen without knowing the platform


@dataclass(frozen=True, slots=True)
class Side:
    calls: int
    tokens: int
    estimated: bool = False

    @property
    def mean(self) -> float:
        return self.tokens / self.calls if self.calls else 0.0


@dataclass(frozen=True, slots=True)
class BeforeAfter:
    key: str
    role: str | None
    at: datetime
    before: Side
    after: Side
    filter: str | None = None  # the filter introduced, when detected from traces
    small_sample: bool = False

    @property
    def change(self) -> float | None:
        """Relative change of tokens per call (-0.8: 80% fewer); None without both sides."""
        if not self.before.calls or not self.after.calls or not self.before.mean:
            return None
        return self.after.mean / self.before.mean - 1


def calls(
    spans: Iterable[Span],
    roles: Mapping[str, str] | None = None,
    platforms: Mapping[str, str] | None = None,
) -> list[Call]:
    """Measured tool calls among ``spans``, in time order. ``roles`` and ``platforms`` map
    session ids to the role of the session's owner and the platform it runs on; a span's
    ``service.name`` is the platform otherwise, when it names one in
    :data:`KNOWN_PLATFORMS`."""
    roles = roles or {}
    platforms = platforms or {}
    out: list[Call] = []
    for span in spans:
        if span.kind is not SpanKind.TOOL:
            continue
        size = output_tokens(span)
        if size is None:
            continue
        session = span.session_id
        platform = platforms.get(session) if session else None
        if platform is None and isinstance(service := span.attributes.get("service.name"), str):
            # a custom service name says nothing about the platform: only known ones count
            known = normalize_platform(service)
            platform = known if known in KNOWN_PLATFORMS else None
        out.append(
            Call(
                at=span.end or span.start,
                role=roles.get(session) if session else None,
                key=call_key(span),
                tokens=size[0],
                estimated=size[1],
                filter=filter_of(span),
                platform=normalize_platform(platform),
                task_id=span.task_id,
                session_id=session,
            )
        )
    out.sort(key=lambda c: (c.at, c.key))
    return out


def output_tokens(span: Span) -> tuple[int, bool] | None:
    """``(tokens, estimated)`` a tool call put into the context, None when unknown."""
    tokens = _int(span.attributes.get(OUTPUT_TOKENS_ATTRIBUTE))
    if tokens is not None:
        return tokens, False
    chars = _int(span.attributes.get(OUTPUT_CHARS_ATTRIBUTE))
    if chars is not None:
        return math.ceil(chars / CHARS_PER_TOKEN), True
    return None


def call_key(span: Span) -> str:
    prefix = span.attributes.get(COMMAND_ATTRIBUTE)
    if not isinstance(prefix, str) or not prefix:
        hook = span.attributes.get(HOOK_COMMAND)
        prefix = command_prefix(hook) if isinstance(hook, str) else None
    return prefix or f"{TOOL_KEY}{span.tool or span.name}"


def filter_of(span: Span) -> str | None:
    used = span.attributes.get(FILTER_ATTRIBUTE)
    if isinstance(used, str) and used:
        return used
    hook = span.attributes.get(HOOK_COMMAND)
    return detect_filter(hook) if isinstance(hook, str) else None


def normalize_platform(name: str | None) -> str | None:
    if not name:
        return None
    return name.strip().lower().replace("_", "-").replace(" ", "-") or None


def rank(
    found: Sequence[Call], settings: OutputSettings | None = None, tz: tzinfo = UTC
) -> list[OutputGroup]:
    """Groups per role and key, the most unfiltered output tokens first; ``hot_days`` are
    dates in ``tz``."""
    settings = settings or OutputSettings()
    by_group: dict[tuple[str | None, str], list[Call]] = {}
    for call in found:
        by_group.setdefault((call.role, call.key), []).append(call)
    groups = [_group(role, key, mine, settings, tz) for (role, key), mine in by_group.items()]
    groups.sort(key=lambda g: (-g.tokens, -g.filtered_tokens, g.key, g.role or ""))
    return groups


def _group(
    role: str | None, key: str, mine: list[Call], settings: OutputSettings, tz: tzinfo
) -> OutputGroup:
    plain = [c for c in mine if c.filter is None]
    filtered = [c for c in mine if c.filter is not None]
    per_day: dict[date, list[int]] = {}
    for c in plain:
        per_day.setdefault(c.at.astimezone(tz).date(), []).append(c.tokens)
    hot = tuple(
        sorted(
            d
            for d, sizes in per_day.items()
            if sum(sizes) / len(sizes) >= settings.threshold_tokens
        )
    )
    platforms = sorted({c.platform for c in mine if c.platform})
    return OutputGroup(
        role=role,
        key=key,
        calls=len(plain),
        tokens=sum(c.tokens for c in plain),
        max_tokens=max((c.tokens for c in plain), default=0),
        estimated=any(c.estimated for c in plain),
        filtered_calls=len(filtered),
        filtered_tokens=sum(c.tokens for c in filtered),
        filters=tuple(sorted({c.filter for c in filtered if c.filter})),
        platform=platforms[0] if len(platforms) == 1 else None,
        tasks=tuple(sorted({c.task_id for c in mine if c.task_id})),
        sessions=tuple(sorted({c.session_id for c in mine if c.session_id})),
        hot_days=hot,
        adopted=max(mine, key=lambda c: c.at).filter is not None,
    )


def recommend(
    groups: Iterable[OutputGroup], settings: OutputSettings | None = None
) -> list[Recommendation]:
    """A filter for every group above the threshold, most savings first."""
    settings = settings or OutputSettings()
    out: list[Recommendation] = []
    for group in groups:
        if group.adopted:
            continue  # verified by filter_introductions instead
        if group.calls < settings.min_calls or group.mean < settings.threshold_tokens:
            continue
        chosen = _choose(group, settings.filters)
        if chosen is None:
            continue
        filt, known = chosen
        out.append(
            Recommendation(
                group=group,
                filter=filt,
                saved_tokens=round(group.tokens * filt.reduction),
                repeated=len(group.hot_days) >= settings.repeat_days,
                owner=settings.owners.get(group.role) if group.role else None,
                platform_known=known,
            )
        )
    out.sort(key=lambda r: (-r.saved_tokens, r.group.key, r.group.role or ""))
    return out


def repeated_offenders(
    found: Sequence[Call], settings: OutputSettings | None = None, tz: tzinfo = UTC
) -> tuple[OutputOffender, ...]:
    """The repeated offenders among ``found`` (:func:`rank` and :func:`recommend`, as the
    daily report lists them), for the snapshot rules read."""
    settings = settings or OutputSettings()
    recent: dict[tuple[str | None, str], list[str]] = {}
    for c in reversed(found):  # the latest first
        if c.filter is None and c.task_id:
            seen = recent.setdefault((c.role, c.key), [])
            if c.task_id not in seen:
                seen.append(c.task_id)
    return tuple(
        OutputOffender(
            role=r.group.role,
            command=r.group.key,
            filter=r.filter.name,
            how=r.filter.how,
            days=len(r.group.hot_days),
            calls=r.group.calls,
            mean_tokens=round(r.group.mean, 1),
            saved_tokens=r.saved_tokens,
            owner=r.owner,
            tasks=r.group.tasks,
            recent_tasks=tuple(recent.get((r.group.role, r.group.key), ())[:10]),
        )
        for r in recommend(rank(found, settings, tz), settings)
        if r.repeated
    )


def _choose(group: OutputGroup, filters: Sequence[Filter]) -> tuple[Filter, bool] | None:
    for filt in filters:
        if not filt.matches(group.key):
            continue
        if filt.supports(group.platform) is False:
            continue
        return filt, filt.platforms is None or group.platform is not None
    return None


def before_after(
    found: Sequence[Call],
    key: str,
    at: datetime,
    settings: OutputSettings | None = None,
    role: str | None = None,
    match: Callable[[Call], bool] | None = None,
    introduced: str | None = None,
) -> BeforeAfter:
    """Tokens per call of ``key`` (one role, or all with ``role`` None) in ``window``
    before ``at`` against ``window`` from ``at``. ``match`` narrows the calls further."""
    settings = settings or OutputSettings()
    mine = [
        c
        for c in found
        if c.key == key and (role is None or c.role == role) and (match is None or match(c))
    ]
    before = [c for c in mine if at - settings.window <= c.at < at]
    after = [c for c in mine if at <= c.at < at + settings.window]
    sides = (_side(before), _side(after))
    small = min(sides[0].calls, sides[1].calls) < settings.min_sample
    return BeforeAfter(key, role, at, sides[0], sides[1], introduced, small)


def _side(found: Sequence[Call]) -> Side:
    return Side(len(found), sum(c.tokens for c in found), any(c.estimated for c in found))


def filter_introductions(
    found: Sequence[Call], settings: OutputSettings | None = None
) -> list[BeforeAfter]:
    """Before/after for every key and role whose calls switched to a filter: unfiltered
    calls before the first filtered one, filtered calls from it on."""
    settings = settings or OutputSettings()
    first: dict[tuple[str | None, str], Call] = {}
    seen_plain: set[tuple[str | None, str]] = set()
    for call in found:  # time order
        group = (call.role, call.key)
        if call.filter is None:
            seen_plain.add(group)
        elif group in seen_plain and group not in first:
            first[group] = call
    out = []
    for (role, key), call in first.items():
        used = call.filter

        def same(
            c: Call, at: datetime = call.at, used: str | None = used, role: str | None = role
        ) -> bool:
            if c.role != role:  # a group without a role is one group, not "any role"
                return False
            return c.filter is None if c.at < at else c.filter == used

        out.append(before_after(found, key, call.at, settings, role, same, used))
    out.sort(key=lambda b: (b.at, b.key, b.role or ""))
    return out


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return max(0, int(value))


def session_context(
    snapshot: Snapshot, remembered: Attribution | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """``(roles, platforms)`` per session id for :func:`calls`: the role of the session's
    owner and the adapter its task came from (``Task.source``), from the snapshot and from
    what the span store remembers of finished sessions."""
    owners = dict(remembered.owners) if remembered else {}
    tasks_of = dict(remembered.session_tasks) if remembered else {}
    for session in snapshot.sessions:
        owners[session.id] = session.owner_id
        tasks_of[session.id] = session.task_id
    role_of = {o.id: o.role for o in snapshot.owners}
    source_of = {t.id: t.source for t in snapshot.tasks if t.source}
    roles = {s: role_of[o] for s, o in owners.items() if o in role_of}
    platforms = {s: source_of[t] for s, t in tasks_of.items() if t in source_of}
    return roles, platforms
