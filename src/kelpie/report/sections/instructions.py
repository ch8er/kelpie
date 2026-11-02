"""Instruction files in the daily report (incident 21): the files each role loads that cost
the most tokens a day, with their size, growth and copies (``[instructions]``).

What the board measured when the report was built (``ReportInputs.instruction_files``,
:mod:`kelpie.instructions`): tokens a day are over the 24 hours before that, and say where
they come from (``loads``: counted skill loads; ``sessions``: size times the role's
sessions, for a skill loaded on demand an upper bound). Growth is over ``[instructions]
window``. Copies are the duplicated sections and values a file takes part in. Only paths,
sizes and ``path:line`` places, never a file's text. No advice: rule ``instruction_bloat``
alerts on what crosses its thresholds, with the split recommendation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from kelpie.core.model import InstructionFile
from kelpie.report import md
from kelpie.report.sections import Section, SectionContext, register_section
from kelpie.report.sections._config import keys_of, settings_from

TITLE = "Instruction files"


@dataclass(frozen=True, slots=True)
class InstructionSettings:
    top: int = 10  # files listed, the costliest first


DEFAULTS = InstructionSettings()


@register_section("instructions", keys=keys_of(DEFAULTS))
def instructions(ctx: SectionContext) -> Section | None:
    s = settings_from(DEFAULTS, ctx.config)
    inputs = ctx.inputs
    files = [f for f in inputs.instruction_files if f.tokens_per_day is not None]
    if not files or s.top == 0:
        return None
    copies: Counter[str] = Counter()
    for d in inputs.instruction_duplicates:
        for path in {p.rpartition(":")[0] for p in d.places}:
            copies[path] += 1
    files.sort(key=lambda f: (-(f.tokens_per_day or 0), -f.chars, f.path))
    shown = files[: s.top]
    total = sum(f.tokens_per_day or 0 for f in files)
    at = inputs.instructions_at
    when = f" as measured at {at.astimezone(inputs.tz):%Y-%m-%d %H:%M}" if at else ""
    lines = [
        f"Reading the {len(files)} instruction file(s) each role loads costs about "
        f"{total:,} tokens a day{when} (the 24 hours before). The costliest:",
        "",
        "| File | Role | Size | Tokens a day | Growth | Copies |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += [_row(f, copies[f.path]) for f in shown]
    if len(files) > len(shown):
        lines += ["", f"{len(files) - len(shown)} more file(s) cost less."]
    if any(f.token_basis == "sessions" and f.loaded == "on_demand" for f in shown):
        lines += [
            "",
            "Tokens a day marked *estimate* are size times the role's sessions: for a skill "
            "loaded on demand an upper bound, since its platform did not count its loads.",
        ]
    return Section(TITLE, tuple(lines))


def _row(f: InstructionFile, copies: int) -> str:
    role = md.text(f.role) if f.role else "every role"
    size = f"{f.chars:,} chars ({f.tokens:,} tokens, {f.sections} sections)"
    tokens = f"{f.tokens_per_day or 0:,}"
    if f.token_basis == "loads":
        tokens += " (counted loads)"
    elif f.loaded == "on_demand":
        tokens += " (*estimate*, sessions)"
    else:
        tokens += " (sessions)"
    return (
        f"| {md.code(f.path, cell=True)} | {role} | {size} | {tokens} | {_growth(f)} "
        f"| {copies or '-'} |"
    )


def _growth(f: InstructionFile) -> str:
    if f.since is None or f.chars_since is None:
        return "-"
    delta = f.chars - f.chars_since
    if not delta and not (f.appends or f.edits):
        return f"none since {f.since:%Y-%m-%d}"
    out = f"{delta:+,} chars since {f.since:%Y-%m-%d}"
    if f.only_grows:
        out += f", only appends ({f.appends})"
    return out
