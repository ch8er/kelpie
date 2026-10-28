"""Measuring instruction files without keeping their text.

A file is read once per scan, measured and dropped: what leaves this module is its size
in bytes and characters, an estimated token count, its SHA-256, the number of sections
and, for comparisons within the same scan, per-section fingerprints (hashed word
shingles) and per-fact hashes. Nothing here is stored or logged as text.

Sections are Markdown ATX headings (``#`` to ``######``) outside fenced code blocks; YAML
front matter (``---`` ... ``---`` on the first lines, a skill's name and description) is
not a section and holds no facts. A fact is a short ``key: value`` or ``key = value``
line (a list item or bold key allowed): the same key and value in several files of a role
is one fact kept in several places.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from kelpie.core.model import InstructionDuplicate

CHARS_PER_TOKEN = 4  # as kelpie.traces.outputs estimates tool outputs
SHINGLE = 3  # words per shingle
MIN_SHINGLES = 8  # a section shorter than this is not compared (a heading and a line)
COMMON_SHINGLE = 64  # a shingle in more sections than this only joins, never seeds, a pair

_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t#]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_WORD = re.compile(r"\w+")
# "- **Default timeout**: 30m", "retries = 3", "* base branch: main"
_FACT = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*|__)?([A-Za-z][\w .\-/]{0,48}?)(?:\*\*|__)?\s*[:=]\s+(\S.*)$"
)
MAX_FACT_KEY_WORDS = 5
MAX_FACT_VALUE = 120


@dataclass(frozen=True, slots=True)
class Section:
    line: int  # 1-based line of its heading
    heading: str  # normalised heading, compared within a scan only, never stored
    shingles: frozenset[int]


@dataclass(frozen=True, slots=True)
class Measure:
    """One file's measurements. ``sections`` and ``facts`` are fingerprints for this scan's
    comparisons; the store keeps only the sizes and the hash."""

    bytes: int
    chars: int
    sha256: str
    sections: tuple[Section, ...]
    facts: tuple[tuple[int, int], ...]  # (fact hash, line)
    appended: bool | None = None  # vs the previous measurement: only appended; None: unknown

    @property
    def tokens(self) -> int:
        return -(-self.chars // CHARS_PER_TOKEN)


class Unreadable(Exception):
    """A file that is not a regular file, is a symlink, or is too large."""


def read_file(path: Path, max_bytes: int) -> bytes:
    """The bytes of a regular file that is not itself a symlink, at most ``max_bytes``."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise Unreadable(f"cannot open: {exc.strerror or exc}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Unreadable("not a regular file")
        if st.st_size > max_bytes:
            raise Unreadable(f"larger than {max_bytes} bytes")
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise Unreadable(f"larger than {max_bytes} bytes")
    return data


def measure(data: bytes, previous: tuple[int, str] | None = None) -> Measure:
    """Measure a file's bytes. ``previous`` is the last stored (bytes, sha256): the change
    is an append when those bytes are this file's first ones."""
    text = data.decode("utf-8", errors="replace")
    sha = hashlib.sha256(data).hexdigest()
    appended: bool | None = None
    if previous is not None and previous[1] != sha:
        size, old = previous
        appended = len(data) > size and hashlib.sha256(data[:size]).hexdigest() == old
    sections, facts = _structure(text)
    return Measure(len(data), len(text), sha, sections, facts, appended)


def _structure(text: str) -> tuple[tuple[Section, ...], tuple[tuple[int, int], ...]]:
    lines = text.splitlines()
    start = _front_matter_end(lines)
    sections: list[Section] = []
    facts: list[tuple[int, int]] = []
    heading: tuple[int, str] | None = None
    body: list[str] = []
    fence: str | None = None  # the open fence's marker (its character, at least its length)

    def close() -> None:
        if heading is not None:
            sections.append(Section(heading[0], heading[1], _shingles(" ".join(body))))

    for i in range(start, len(lines)):
        line = lines[i]
        m = _FENCE.match(line)
        if m:
            marker, info = m.group(1), m.group(2).strip()
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence) and not info:
                fence = None
            body.append(line)
            continue
        if fence is None:
            h = _HEADING.match(line)
            if h:
                close()
                heading, body = (i + 1, _normal(h.group(2))), []
                continue
            fact = _fact(line)
            if fact is not None:
                facts.append((fact, i + 1))
        body.append(line)
    close()
    return tuple(sections), tuple(facts)


def _front_matter_end(lines: Sequence[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for i in range(1, min(len(lines), 200)):
        if lines[i].strip() in ("---", "..."):
            return i + 1
    return 0


def _normal(text: str) -> str:
    return " ".join(w.casefold() for w in _WORD.findall(text))


def _shingles(text: str) -> frozenset[int]:
    words = [w.casefold() for w in _WORD.findall(text)]
    if len(words) < SHINGLE:
        return frozenset()
    return frozenset(
        zlib.crc32(" ".join(words[i : i + SHINGLE]).encode())
        for i in range(len(words) - SHINGLE + 1)
    )


def _fact(line: str) -> int | None:
    m = _FACT.match(line)
    if m is None:
        return None
    key, value = _normal(m.group(1)), _normal(m.group(2))
    if not key or not value or len(key.split()) > MAX_FACT_KEY_WORDS:
        return None
    if len(m.group(2)) > MAX_FACT_VALUE:
        return None  # a sentence after a colon, not a value
    return zlib.crc32(f"{key}\0{value}".encode())


# --- copies within and across a role's files ------------------------------------------


@dataclass(frozen=True, slots=True)
class Scanned:
    """A measured file, for :func:`copies`."""

    path: str  # as shown
    measure: Measure


def copies(
    role: str | None,
    files: Iterable[Scanned],
    *,
    similarity: float = 0.8,
    same_heading_similarity: float = 0.5,
    min_value_files: int = 3,
) -> tuple[InstructionDuplicate, ...]:
    """Near-duplicate sections and repeated values among the files ``role`` loads. Two
    sections are near duplicates when their bodies' shingle sets are at least
    ``similarity`` alike (Jaccard), or ``same_heading_similarity`` alike under the same
    normalised heading; near duplicates are grouped transitively. A value is repeated when
    the same fact is in at least ``min_value_files`` of the files."""
    group = sorted({f.path: f for f in files}.values(), key=lambda f: f.path)
    return (
        *_section_copies(role, group, similarity, same_heading_similarity),
        *_value_copies(role, group, min_value_files),
    )


def _section_copies(
    role: str | None, files: Sequence[Scanned], similarity: float, same_heading: float
) -> list[InstructionDuplicate]:
    items = [
        (f"{f.path}:{s.line}", s)
        for f in files
        for s in f.measure.sections
        if len(s.shingles) >= MIN_SHINGLES
    ]
    parent = list(range(len(items)))
    best: dict[int, float] = {}
    # Exact copies first, however many: only one of each body is compared further, so a
    # shingle shared by many copies of one section never hides them (COMMON_SHINGLE).
    first: dict[frozenset[int], int] = {}
    unique: list[int] = []
    for n, (_, s) in enumerate(items):
        rep = first.setdefault(s.shingles, n)
        if rep == n:
            unique.append(n)
        else:
            parent[n] = rep
            best[n] = best[rep] = 1.0
    index: dict[int, list[int]] = {}
    for n in unique:
        for sh in items[n][1].shingles:
            index.setdefault(sh, []).append(n)

    def find(n: int) -> int:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for n in unique:
        s = items[n][1]
        candidates: set[int] = set()
        for sh in s.shingles:
            posting = index[sh]
            if len(posting) <= COMMON_SHINGLE:
                candidates.update(m for m in posting if m > n)
        for m in candidates:
            other = items[m][1]
            shared = len(s.shingles & other.shingles)
            jaccard = shared / (len(s.shingles) + len(other.shingles) - shared)
            bar = same_heading if s.heading and s.heading == other.heading else similarity
            if jaccard >= bar:
                a, b = find(n), find(m)
                if a != b:
                    parent[b] = a
                for k in (n, m):
                    best[k] = min(best.get(k, 1.0), jaccard)
    groups: dict[int, list[int]] = {}
    for n in best:
        groups.setdefault(find(n), []).append(n)
    found = []
    for members in groups.values():
        members.sort()
        found.append(
            InstructionDuplicate(
                role,
                "section",
                tuple(items[n][0] for n in members),
                round(min(best[n] for n in members), 2),
            )
        )
    return sorted(found, key=lambda d: d.places)


def _value_copies(
    role: str | None, files: Sequence[Scanned], min_files: int
) -> list[InstructionDuplicate]:
    places: dict[int, dict[str, int]] = {}
    for f in files:
        for fact, line in f.measure.facts:
            places.setdefault(fact, {}).setdefault(f.path, line)  # first line per file
    found = [
        InstructionDuplicate(role, "value", tuple(f"{p}:{line}" for p, line in at.items()))
        for at in places.values()
        if len(at) >= min_files
    ]
    return sorted(found, key=lambda d: (-len(d.places), d.places))
