"""Instruction bloat (incident 21): skills and role instructions that only grow.

Agents append a write-up to their own skills after every case and nobody prunes them; on
a live farm three roles' skills reached the platform's size cap, read on every step before
any work. ``Snapshot.instruction_files`` holds the files each role loads, measured
read-only by :mod:`kelpie.instructions` (sizes, counts and hashes, never the text), and
``Snapshot.instruction_duplicates`` the copies found in them.

A file is reported when one of these holds:

- it crossed ``max_chars`` within the growth window (it was below at ``since``);
- it grows faster than ``max_growth_per_week`` characters a week (measured over at least
  ``min_span``);
- it is above ``max_chars`` and costs at least ``max_tokens_per_day`` tokens a day: loaded
  into every session of its role (``loaded = "always"``), or counted loads of it. An
  on-demand file (a skill) without counted loads has no cost to judge, only an upper bound,
  so a large skill that does not change stays silent: a built-in one is loaded rarely.

The alert names the file, its size in characters and estimated tokens, its growth, whether
it only grows (appends and nothing removed), its tokens a day and where that figure comes
from, the copies of its sections and values, and recommends a short core plus topic
references. Copies that involve no reported file get their own (informational) alert per
role: one fact, one place. Each is a lasting state: alerted when it starts, kept open while
the file stays over ``max_chars`` (after the window has moved past its growth), and
resolved when the file is split. Kelpie never edits instruction files, so
the rule only alerts (observe by default).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import InstructionDuplicate, InstructionFile, Snapshot
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._alerts import alert_params, count

NAME = "instruction_bloat"
MAX_PLACES = 8  # places of a copy named in a reason

RECOMMENDATION = (
    "Split it into a short core plus topic references: keep in the file only what every "
    "case needs, move case write-ups and reference material into topic files the core "
    "names (loaded when the case comes up), merge the duplicated sections and keep each "
    "value in one place."
)


@dataclass(frozen=True, slots=True)
class InstructionBloat:
    name: str = NAME
    max_chars: int = 40_000
    max_growth_per_week: int = 10_000
    max_tokens_per_day: int = 500_000
    min_span: timedelta = timedelta(days=1)

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        actions: list[Action] = []
        told: set[int] = set()  # copies named in a file's alert
        for f in snapshot.instruction_files:
            why = self._why(f, snapshot)
            if why:
                copies = [
                    (i, d)
                    for i, d in enumerate(snapshot.instruction_duplicates)
                    if _touches(d, f.path)
                ]
                told.update(i for i, _ in copies)
                actions.append(self._alert(f, why, [d for _, d in copies], snapshot))
        by_role: dict[str | None, list[InstructionDuplicate]] = {}
        for i, d in enumerate(snapshot.instruction_duplicates):
            if i not in told:
                by_role.setdefault(d.role, []).append(d)
        for role, copies in sorted(by_role.items(), key=lambda kv: kv[0] or ""):
            actions.append(_copies_alert(role, copies))
        return actions

    def _why(self, f: InstructionFile, snapshot: Snapshot) -> list[str]:
        why: list[str] = []
        below = f.chars_since is not None and f.chars_since < self.max_chars
        if f.chars >= self.max_chars and below:
            why.append(f"it crossed {self.max_chars:,} characters")
        growth = f.growth_per_week(snapshot.now, self.min_span)
        if growth is not None and growth >= self.max_growth_per_week:
            why.append(
                f"it grows {growth:,.0f} characters a week (over {self.max_growth_per_week:,})"
            )
        if not why and f.chars >= self.max_chars and _state(f) in snapshot.open_states:
            # Alerted before and not split yet: the window moved on, the file did not.
            why.append(f"it is still over {self.max_chars:,} characters")
        costed = f.loaded == "always" or f.token_basis == "loads"
        if (
            f.chars >= self.max_chars
            and costed
            and f.tokens_per_day is not None
            and f.tokens_per_day >= self.max_tokens_per_day
            and not why
        ):
            why.append(
                f"it is over {self.max_chars:,} characters and read for over "
                f"{self.max_tokens_per_day:,} tokens a day"
            )
        return why

    def _alert(
        self,
        f: InstructionFile,
        why: list[str],
        copies: Sequence[InstructionDuplicate],
        snapshot: Snapshot,
    ) -> Action:
        role = f.role or "every role"
        who = f"{f.owner}: " if f.owner else ""
        parts = [
            f"{who}`{f.path}` ({role}) is {f.chars:,} characters, about {f.tokens:,} tokens, "
            f"in {f.sections} sections: {'; '.join(why)}."
        ]
        growth = f.growth_per_week(snapshot.now, self.min_span)
        if f.only_grows and f.since is not None and f.chars_since is not None:
            parts.append(
                f"It only grows: {f.appends} appends and nothing removed since "
                f"{f.since:%Y-%m-%d} ({f.chars - f.chars_since:+,} characters)."
            )
        if f.tokens_per_day is not None:
            basis = (
                "from counted loads"
                if f.token_basis == "loads"
                else "estimated as its size times the role's sessions"
                + (", an upper bound for a file loaded on demand" if f.loaded != "always" else "")
            )
            parts.append(f"Reading it costs about {f.tokens_per_day:,} tokens a day ({basis}).")
        if copies:
            parts.append(_copies_text(copies))
        parts.append(RECOMMENDATION)
        state = _state(f)
        fast = growth is not None and growth >= self.max_growth_per_week
        return Action(
            ActionKind.ALERT,
            NAME,
            " ".join(parts),
            key=state,
            severity=Severity.WARNING,
            state=state,
            params={
                "path": f.path,
                "role": f.role,
                "owner": f.owner,
                "chars": f.chars,
                "tokens": f.tokens,
                "sections": f.sections,
                "growth_per_week": None if growth is None else round(growth),
                "only_grows": f.only_grows,
                "tokens_per_day": f.tokens_per_day,
                "token_basis": f.token_basis,
                "duplicate_sections": sum(1 for d in copies if d.kind == "section"),
                "repeated_values": sum(1 for d in copies if d.kind == "value"),
                **alert_params(
                    f"`{_name(f.path)}` ({role}): {f.chars:,} characters, "
                    + (f"{growth:+,.0f} a week" if fast else f"~{f.tokens:,} tokens")
                ),
            },
        )


def _copies_alert(role: str | None, copies: Sequence[InstructionDuplicate]) -> Action:
    who = role or "every role"
    sections = sum(1 for d in copies if d.kind == "section")
    values = len(copies) - sections
    state = f"{NAME}:copies:{who}"
    return Action(
        ActionKind.ALERT,
        NAME,
        f"The {who} instructions repeat themselves. {_copies_text(copies)} A fact changed in "
        "one copy stays stale in the others and goes into the output: keep one place and "
        "refer to it.",
        key=state,
        severity=Severity.INFO,
        state=state,
        params={
            "role": role,
            "duplicate_sections": sections,
            "repeated_values": values,
            **alert_params(
                f"{who} instructions: {count(sections, 'duplicated section')}, "
                f"{count(values, 'repeated value')}"
            ),
        },
    )


def _copies_text(copies: Sequence[InstructionDuplicate]) -> str:
    out: list[str] = []
    for d in sorted(copies, key=lambda d: (d.kind, -len(d.places), d.places)):
        places = ", ".join(d.places[:MAX_PLACES])
        more = f" and {len(d.places) - MAX_PLACES} more" if len(d.places) > MAX_PLACES else ""
        if d.kind == "section":
            out.append(f"near-duplicate sections ({d.similarity:.0%} alike): {places}{more}")
        else:
            out.append(f"one value in {len(d.places)} places: {places}{more}")
    return "Copies: " + "; ".join(out) + "."


def _state(f: InstructionFile) -> str:
    return f"{NAME}:{f.path}"


def _touches(d: InstructionDuplicate, path: str) -> bool:
    return any(p.rpartition(":")[0] == path for p in d.places)


def _name(path: str) -> str:
    """The last two parts of a path, for a headline (skills are all called SKILL.md)."""
    parts = path.replace("\\", "/").rstrip("/").split("/")
    return "/".join(parts[-2:])


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@register(NAME)
def build(config: dict[str, Any]) -> InstructionBloat:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("max_chars", "max_growth_per_week", "max_tokens_per_day"):
        if key in config:
            kwargs[key] = _positive_int(config.pop(key), key)
    if "min_span" in config:
        try:
            span = parse_duration(config.pop("min_span"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"min_span: {exc}") from None
        if span <= timedelta(0):
            raise ValueError("min_span must be positive")
        kwargs["min_span"] = span
    if config:
        raise ValueError(
            f"unknown parameters: {sorted(config)} (the files to measure are in [instructions])"
        )
    return InstructionBloat(**kwargs)
