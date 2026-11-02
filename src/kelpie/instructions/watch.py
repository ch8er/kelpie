"""The instruction files of every role, measured for the snapshot (rule ``instruction_bloat``).

At most every ``[instructions] every`` of snapshot time the configured globs are expanded,
each file is read read-only (never through a symlink, at most ``max_bytes``), measured by
:mod:`kelpie.instructions.scan` and dropped. A size row is stored when a file is first seen
and whenever its hash changes, with whether the change only appended; the growth window is
read back from those rows. Between scans the last result is reused.

Tokens a day spent reading a file: its estimated tokens times how often it was read in the
last 24 hours of the snapshot.

- ``token_basis = "loads"``: a skill (``<name>/SKILL.md``, loaded on demand) of a platform
  that counted its skill loads this tick (``Snapshot.skill_loads_counted``: Hermes
  ``skill_view`` calls from ``state.db``, Claude Code ``Skill`` calls from its hook) is read
  as often as a skill of that name was opened (with its categories, when the call named
  them: ``dev/release-notes`` is ``.../dev/release-notes/SKILL.md``) by: the profile whose
  path it is under (what ``{role}`` matched), else its role, else any role when it is a
  skill every role of the platform shares; the first by path wins where several fit. A
  plugin's skill (``plugin:name``) is none of these files. A skill nobody opened costs
  nothing.
- ``token_basis = "sessions"``, otherwise: the sessions on this host of its role (of its
  platform, for the platform defaults; any, for a file every role loads) that started in
  the window. For an always-loaded file that is the cost; for a skill of a platform that
  counts no loads it is an upper bound.

Only files on this host are measured: a ``kelpie agent`` host's files are not (yet).
"""

from __future__ import annotations

import glob
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from kelpie.core.model import InstructionDuplicate, InstructionFile, Snapshot, TokenBasis
from kelpie.core.storage import SizeRow, SqliteInstructionStore
from kelpie.instructions.config import InstructionSettings, Source
from kelpie.instructions.scan import Measure, Scanned, Unreadable, copies, measure, read_file

log = logging.getLogger(__name__)

RETRY_AFTER = timedelta(minutes=5)
DAY = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Found:
    """One measured file and where it came from."""

    path: Path
    shown: str
    role: str | None
    source: Source
    measure: Measure
    scope: str | None = None  # what ``{role}`` matched in its path (a Hermes profile)


@dataclass(slots=True)
class InstructionWatch:
    store: SqliteInstructionStore
    settings: InstructionSettings
    sources: tuple[Source, ...]  # configured entries first: the first source of a path wins
    home: str = field(default_factory=lambda: os.path.expanduser("~"))
    retry_after: timedelta = RETRY_AFTER
    skipped: dict[str, str] = field(default_factory=dict)  # shown path -> why (kelpie status)
    failed: bool = False  # the last scan failed: the result is the one before
    _found: tuple[Found, ...] = ()
    _history: dict[str, list[SizeRow]] = field(default_factory=dict)
    _copies: tuple[InstructionDuplicate, ...] = ()
    _next: datetime | None = None

    def read(
        self, snapshot: Snapshot
    ) -> tuple[tuple[InstructionFile, ...], tuple[InstructionDuplicate, ...]]:
        now = snapshot.now
        every = self.settings.every
        if self._next is None or not (self._next - every <= now < self._next):
            try:
                self._scan(now)
            except Exception as exc:
                # Keep the last measurements: an empty list would resolve every open alert.
                self._next = now + self.retry_after
                log.error("instruction files could not be measured: %s", type(exc).__name__)
                self.failed = True
            else:
                self._next = now + every
                self.failed = False
        sessions = _sessions_per_day(snapshot)
        loads = _loads_per_file(self._found, snapshot)
        return tuple(self._file(f, now, sessions, loads) for f in self._found), self._copies

    # --- scanning -------------------------------------------------------------------

    def _scan(self, now: datetime) -> None:
        window_start = now - self.settings.window
        history = self.store.history(window_start)
        found: list[Found] = []
        seen: set[Path] = set()
        skipped: dict[str, str] = {}
        unread: set[str] = set()
        rows: list[SizeRow] = []
        for source, path, role, scope in self._paths():
            if path in seen:
                continue
            seen.add(path)
            shown = self._shown(path)
            if len(found) >= self.settings.max_files:
                skipped[shown] = f"over [instructions] max_files ({self.settings.max_files})"
                unread.add(str(path))
                continue
            last = history.get(str(path), [])
            previous = (last[-1].bytes, last[-1].sha256) if last else None
            try:
                m = measure(read_file(path, self.settings.max_bytes), previous)
            except Unreadable as exc:
                skipped[shown] = str(exc)
                unread.add(str(path))
                continue
            found.append(Found(path, shown, role, source, m, scope))
            if previous is None or previous[1] != m.sha256:
                row = SizeRow(str(path), now, m.bytes, m.chars, m.sha256, len(m.sections),
                              m.appended)  # fmt: skip
                rows.append(row)
                history.setdefault(str(path), []).append(row)
        if rows:
            self.store.record(rows)
        # A file no longer found (deleted, unconfigured) loses its history; one that could
        # not be read this time keeps it.
        self.store.prune(window_start, keep={str(f.path) for f in found} | unread)
        for shown, why in skipped.items():
            if self.skipped.get(shown) != why:
                log.warning("instruction file %s not measured: %s", shown, why)
        self.skipped = skipped
        self._found, self._history = tuple(found), history
        self._copies = self._find_copies(found)

    def _paths(self) -> Iterable[tuple[Source, Path, str | None, str | None]]:
        for source in self.sources:
            for pattern in source.paths:
                expanded = os.path.expanduser(pattern)
                for match in sorted(glob.glob(expanded.replace("{role}", "*"), recursive=True)):
                    path = Path(match)
                    role, name = source.role, None
                    if "{role}" in expanded:
                        name = _role_part(expanded, match)
                        if name is None:
                            continue
                        role = source.role_of(name) if source.role_of else name
                    yield source, path, role, name

    def _shown(self, path: Path) -> str:
        text = str(path)
        home = self.home.rstrip(os.sep)
        if home and (text == home or text.startswith(home + os.sep)):
            return "~" + text[len(home) :]
        return text

    def _find_copies(self, found: Sequence[Found]) -> tuple[InstructionDuplicate, ...]:
        """Per role, its own files plus the files every role of its platforms loads; a copy
        among the shared files alone is reported once, under no role."""
        shared = [f for f in found if f.role is None]
        roles = sorted({f.role for f in found if f.role is not None})
        s = self.settings
        out: list[InstructionDuplicate] = []
        shared_paths = {f.shown for f in shared}
        groups: list[tuple[str | None, list[Found]]] = []
        # The shared files per platform (no session loads two platforms' files; files of no
        # platform go with each), then each role with the shared files of its platforms.
        platforms = sorted({f.source.platform for f in shared if f.source.platform}) or [None]
        for platform in platforms:
            groups.append((None, [f for f in shared if f.source.platform in (platform, None)]))
        for role in roles:
            own = [f for f in found if f.role == role]
            theirs = {f.source.platform for f in own} | {None}
            groups.append((role, own + [f for f in shared if f.source.platform in theirs]))
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for role, group in groups:
            for d in copies(
                role,
                (Scanned(f.shown, f.measure) for f in group),
                similarity=s.similarity,
                same_heading_similarity=s.same_heading_similarity,
                min_value_files=s.min_value_files,
            ):
                if role is not None and all(_place_path(p) in shared_paths for p in d.places):
                    continue
                if (d.kind, d.places) not in seen:  # files of no platform are in each pass
                    seen.add((d.kind, d.places))
                    out.append(d)
        return tuple(out)

    # --- the snapshot's view --------------------------------------------------------

    def _file(
        self,
        f: Found,
        now: datetime,
        sessions: dict[tuple[str | None, str | None], int],
        loads: dict[Path, int],
    ) -> InstructionFile:
        start = now - self.settings.window
        rows = self._history.get(str(f.path), [])
        # The scan cut its history at its own clock: cut again at this read's.
        before = [r for r in rows if r.measured_at <= start]
        rows = before[-1:] + [r for r in rows if r.measured_at > start]
        base = rows[0] if rows else None
        since = max(base.measured_at, start) if base else None
        later = rows[1:]
        basis: TokenBasis = "sessions"
        n = sessions.get((f.source.platform, f.role), 0)
        if f.path in loads:
            basis, n = "loads", loads[f.path]
        tokens_per_day = f.measure.tokens * n
        return InstructionFile(
            path=f.shown,
            role=f.role,
            chars=f.measure.chars,
            tokens=f.measure.tokens,
            sections=len(f.measure.sections),
            loaded=f.source.loaded,
            since=since,
            chars_since=base.chars if base else None,
            appends=sum(1 for r in later if r.appended is True),
            edits=sum(1 for r in later if r.appended is not True),
            tokens_per_day=tokens_per_day,
            token_basis=basis,
            owner=f.source.owner,
        )


def _role_part(pattern: str, match: str) -> str | None:
    """The part of ``match`` that ``{role}`` in ``pattern`` matched, both normalised (glob
    keeps some doubled separators and drops others); the config refuses ``**`` before it."""
    parts = os.path.normpath(pattern).split(os.sep)
    k = next(i for i, p in enumerate(parts) if "{role}" in p)
    actual = os.path.normpath(match).split(os.sep)
    if len(actual) <= k:
        return None
    before, _, after = parts[k].partition("{role}")
    name = actual[k]
    if not (name.startswith(before) and name.endswith(after)) or len(name) <= len(before + after):
        return None
    return name[len(before) : len(name) - len(after)] or None


def _place_path(place: str) -> str:
    return place.rpartition(":")[0]


SKILL_FILE = "SKILL.md"


def skill_name(path: Path) -> str | None:
    """The name a platform opens a skill by: the directory of its ``SKILL.md``."""
    return (path.parent.name or None) if path.name == SKILL_FILE else None


def _loads_per_file(found: Sequence[Found], snapshot: Snapshot) -> dict[Path, int]:
    """Loads in the last 24 hours of each skill whose platform counted them this tick
    (every such skill is a key, opened or not)."""
    counted = set(snapshot.skill_loads_counted)
    skills: dict[tuple[str, str], list[Found]] = {}  # (platform, name) -> its files
    for f in found:
        name = skill_name(f.path)
        platform = f.source.platform
        if f.source.loaded == "on_demand" and platform in counted and name is not None:
            skills.setdefault((platform, name), []).append(f)
    out = {f.path: 0 for files in skills.values() for f in files}
    for load in snapshot.skill_loads:
        if load.source is None or not snapshot.now - DAY <= load.at <= snapshot.now:
            continue
        name = load.skill.rpartition("/")[2]
        files = sorted(skills.get((load.source, name), []), key=lambda f: f.shown)
        if "/" in load.skill:  # categories named: only the skill under them
            tail = f"/{load.skill}/{SKILL_FILE}"
            files = [f for f in files if f.path.as_posix().endswith(tail)]
        profile = load.profile.casefold() if load.profile else None
        here = [f for f in files if profile and f.scope and f.scope.casefold() == profile]
        own = [f for f in files if f.scope is None and f.role is not None and f.role == load.role]
        if not profile:  # by role alone: any profile of the role
            own += [f for f in files if f.scope is not None and f.role == load.role]
        shared = [f for f in files if f.role is None]
        if target := (here or own or shared)[:1]:
            out[target[0].path] += 1
    return out


def _sessions_per_day(snapshot: Snapshot) -> dict[tuple[str | None, str | None], int]:
    """Sessions on this host that started in the last 24 hours, per (platform, role); also
    per (platform, None), (None, role) and (None, None) for files every role or any
    platform loads. A ``kelpie agent`` host's sessions read that host's files, which are not
    measured here, so they are left out."""
    owners = {o.id: o for o in snapshot.owners}
    counts: dict[tuple[str | None, str | None], int] = {}
    for s in snapshot.sessions:
        if not snapshot.now - DAY <= s.started_at <= snapshot.now:
            continue
        owner = owners.get(s.owner_id)
        if owner is not None and owner.host is not None:
            continue
        role = owner.role if owner is not None else None
        for key in {(s.source, role), (s.source, None), (None, role), (None, None)}:
            counts[key] = counts.get(key, 0) + 1
    return counts
