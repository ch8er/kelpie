"""``[instructions]``: which instruction files each role loads (incident 21).

Off unless the section is present: measuring reads files on the host, so a farm opts in.
With the section, the platform defaults of the configured adapters apply (``defaults =
false`` turns them off) plus every ``[[instructions.files]]`` entry::

    [instructions]
    every = "1h"           # how often the files are measured
    window = "7d"          # growth is measured over this
    [[instructions.files]]
    role = "developer"     # omit: every role loads it
    paths = ["~/farm/dev/CLAUDE.md", "~/farm/dev/.claude/skills/*/SKILL.md"]
    loaded = "always"      # or "on_demand" (skills)
    owner = "human-owner"  # who owns these instructions (named in the alert)

A path is a glob (``**`` for any depth). ``{role}`` in a path matches one directory or
file-name part, which becomes the role (through the platform's own role mapping for the
defaults): ``~/.hermes/profiles/{role}/skills/**/SKILL.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from kelpie.core.model import InstructionLoad
from kelpie.core.serde import parse_duration

SECTION = "[instructions]"
KEYS = frozenset(
    {
        "defaults", "every", "window", "max_bytes", "max_files", "similarity",
        "same_heading_similarity", "min_value_files", "files",
    }
)  # fmt: skip
FILE_KEYS = frozenset({"role", "paths", "loaded", "owner"})
LOADS: tuple[InstructionLoad, ...] = ("always", "on_demand")
MIN_EVERY = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Source:
    """Instruction files of one kind: glob ``paths``, the ``role`` that loads them (None:
    every role; ``{role}`` in a path names it per file), how they are loaded, who owns
    them, and the platform whose sessions read them (None: any)."""

    paths: tuple[str, ...]
    role: str | None = None
    loaded: InstructionLoad = "always"
    owner: str | None = None
    platform: str | None = None
    role_of: Callable[[str], str] | None = field(default=None, compare=False)


@dataclass(frozen=True, slots=True)
class InstructionSettings:
    enabled: bool = False
    defaults: bool = True
    every: timedelta = timedelta(hours=1)
    window: timedelta = timedelta(days=7)
    max_bytes: int = 2_000_000  # a larger file is skipped (and named in kelpie status)
    max_files: int = 2_000  # files measured per scan, at most
    similarity: float = 0.8
    same_heading_similarity: float = 0.5
    min_value_files: int = 3
    files: tuple[Source, ...] = ()


def platform_sources(
    platform: str, home: str = "~", role_of: Callable[[str], str] | None = None
) -> tuple[Source, ...]:
    """The instruction files a platform loads by default. ``home`` is the platform's own
    state directory where it has one (OpenClaw)."""
    if platform == "hermes":
        return (
            Source(("~/.hermes/SOUL.md",), loaded="always", platform=platform),
            Source(("~/.hermes/skills/**/SKILL.md",), loaded="on_demand", platform=platform),
            Source(
                ("~/.hermes/profiles/{role}/SOUL.md",),
                loaded="always",
                platform=platform,
                role_of=role_of,
            ),
            Source(
                ("~/.hermes/profiles/{role}/skills/**/SKILL.md",),
                loaded="on_demand",
                platform=platform,
                role_of=role_of,
            ),
        )
    if platform == "claude-code":
        return (
            Source(("~/.claude/CLAUDE.md",), loaded="always", platform=platform),
            Source(("~/.claude/skills/*/SKILL.md",), loaded="on_demand", platform=platform),
            Source(("~/.claude/agents/{role}.md",), loaded="always", platform=platform),
        )
    if platform == "openclaw":
        ws = f"{home.rstrip('/')}/workspace"
        return (
            Source(
                tuple(f"{ws}/{name}" for name in ("AGENTS.md", "SOUL.md", "TOOLS.md")),
                loaded="always",
                platform=platform,
            ),
            Source((f"{ws}/skills/*/SKILL.md",), loaded="on_demand", platform=platform),
            Source(
                tuple(f"{ws}-{{role}}/{name}" for name in ("AGENTS.md", "SOUL.md", "TOOLS.md")),
                loaded="always",
                platform=platform,
            ),
            Source((f"{ws}-{{role}}/skills/*/SKILL.md",), loaded="on_demand", platform=platform),
        )
    return ()


def load_instructions(section: Any) -> InstructionSettings:
    """Parse ``[instructions]``; absent: disabled."""
    if section is None:
        return InstructionSettings()
    if not isinstance(section, Mapping):
        raise ValueError(f"{SECTION} must be a table")
    if extra := set(section) - KEYS:
        raise ValueError(f"unknown {SECTION} keys: {sorted(extra)}")
    kwargs: dict[str, Any] = {"enabled": True}
    if "defaults" in section:
        if not isinstance(section["defaults"], bool):
            raise ValueError(f"{SECTION} defaults must be true or false")
        kwargs["defaults"] = section["defaults"]
    for key, least in (("every", MIN_EVERY), ("window", timedelta(days=1))):
        if key in section:
            kwargs[key] = _duration(section[key], key, least)
    for key in ("max_bytes", "max_files", "min_value_files"):
        if key in section:
            value = section[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{SECTION} {key} must be a positive integer")
            kwargs[key] = value
    if kwargs.get("min_value_files", 3) < 2:
        raise ValueError(f"{SECTION} min_value_files must be at least 2")
    for key in ("similarity", "same_heading_similarity"):
        if key in section:
            value = section[key]
            if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
                raise ValueError(f"{SECTION} {key} must be a number in (0, 1]")
            kwargs[key] = float(value)
    if kwargs.get("every", timedelta(hours=1)) > kwargs.get("window", timedelta(days=7)):
        raise ValueError(f"{SECTION} every must not be longer than window")
    kwargs["files"] = _files(section.get("files", []))
    return InstructionSettings(**kwargs)


def _duration(value: Any, key: str, least: timedelta) -> timedelta:
    try:
        d = parse_duration(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{SECTION} {key}: {exc}") from None
    if d < least:
        raise ValueError(f"{SECTION} {key} must be at least {least}")
    return d


def _files(raw: Any) -> tuple[Source, ...]:
    where = "[[instructions.files]]"
    if not isinstance(raw, list) or not all(isinstance(e, Mapping) for e in raw):
        raise ValueError(f"{where} must be an array of tables")
    out: list[Source] = []
    for i, entry in enumerate(cast(Sequence[Mapping[str, Any]], raw)):
        at = f"{where} #{i + 1}"
        if extra := set(entry) - FILE_KEYS:
            raise ValueError(f"unknown {at} keys: {sorted(extra)}")
        paths = entry.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if (
            not isinstance(paths, list)
            or not paths
            or not all(isinstance(p, str) and p.strip() for p in paths)
        ):
            raise ValueError(f"{at} paths must be a non-empty list of path globs")
        if any(p.count("{role}") > 1 for p in paths):
            raise ValueError(f"{at} paths may hold {{role}} once each")
        role = entry.get("role")
        if role is not None and (not isinstance(role, str) or not role.strip()):
            raise ValueError(f"{at} role must be a non-empty string")
        if any("**" in p.partition("{role}")[0] for p in paths if "{role}" in p):
            raise ValueError(f"{at} paths may not have ** before {{role}}")
        if role is not None and any("{role}" in p for p in paths):
            raise ValueError(f"{at} sets role and uses {{role}} in a path: pick one")
        loaded = entry.get("loaded", "always")
        if loaded not in LOADS:
            raise ValueError(f"{at} loaded must be one of {list(LOADS)}")
        owner = entry.get("owner")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise ValueError(f"{at} owner must be a non-empty string")
        out.append(
            Source(
                tuple(p.strip() for p in paths),
                role=role.strip() if role else None,
                loaded=cast(InstructionLoad, loaded),
                owner=owner.strip() if owner else None,
            )
        )
    return tuple(out)
