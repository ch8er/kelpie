"""The report's instruction files section (#282): top files by tokens a day, with growth
and copies; never a file's text."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from kelpie.core.model import InstructionDuplicate, InstructionFile
from kelpie.report.daily import ReportInputs, build_report, render_markdown

DAY = date(2026, 3, 8)
AT = datetime(2026, 3, 9, 0, 5, tzinfo=UTC)
WEEK_AGO = datetime(2026, 3, 2, tzinfo=UTC)


def report(**kw: Any) -> str:
    inputs = ReportInputs(day=DAY, tz=UTC, instructions_at=AT, **kw)
    return render_markdown(build_report(inputs))


def file(path: str, tokens_per_day: int | None, **kw: Any) -> InstructionFile:
    base: dict[str, Any] = {
        "role": "developer",
        "chars": 40_000,
        "tokens": 10_000,
        "sections": 12,
        "since": WEEK_AGO,
        "chars_since": 40_000,
        "token_basis": "loads",
        "loaded": "on_demand",
    }
    return InstructionFile(path, tokens_per_day=tokens_per_day, **{**base, **kw})


def section(markdown: str) -> str:
    return markdown.split("## Instruction files\n", 1)[1].split("\n## ", 1)[0]


def test_the_costliest_files_with_their_basis_growth_and_copies() -> None:
    files = (
        file(
            "~/.hermes/profiles/dev/skills/build/SKILL.md",
            400_000,
            chars=120_000,
            chars_since=90_000,
            appends=6,
            sections=46,
        ),
        file("~/farm/ops/AGENTS.md", 960_000, role="ops", loaded="always", token_basis="sessions"),
        file(
            "~/farm/review/skills/review/SKILL.md",
            1_500_000,
            role="reviewer",
            token_basis="sessions",
            chars_since=41_000,
            edits=1,
            appends=1,
        ),
        file("~/.claude/skills/pdf/SKILL.md", 0, role=None),
        file("~/never-read.md", None),  # not measured for cost: left out
    )
    ops = "~/farm/ops/AGENTS.md"
    copies = (
        InstructionDuplicate("ops", "value", (f"{ops}:4", "~/farm/ops/TOOLS.md:9")),
        InstructionDuplicate("ops", "section", (f"{ops}:10", f"{ops}:80")),
    )
    text = section(report(instruction_files=files, instruction_duplicates=copies))
    assert "costs about 2,860,000 tokens a day as measured at 2026-03-09 00:05" in text
    rows = [line for line in text.splitlines() if line.startswith("| `")]
    assert [r.split(" | ")[0] for r in rows] == [
        "| `~/farm/review/skills/review/SKILL.md`",
        "| `~/farm/ops/AGENTS.md`",
        "| `~/.hermes/profiles/dev/skills/build/SKILL.md`",
        "| `~/.claude/skills/pdf/SKILL.md`",
    ]
    review, ops, build, pdf = rows
    assert (
        "1,500,000 (*estimate*, sessions)" in review and "-1,000 chars since 2026-03-02" in review
    )
    assert "960,000 (sessions)" in ops and ops.endswith("| 2 |")  # one value, one section
    assert "400,000 (counted loads)" in build and "only appends (6)" in build
    assert "120,000 chars (10,000 tokens, 46 sections)" in build
    assert "every role" in pdf and "none since 2026-03-02" in pdf and pdf.endswith("| - |")
    assert "an upper bound" in text


def test_top_is_configurable_and_no_files_is_no_section() -> None:
    files = tuple(file(f"~/s{i}/SKILL.md", 1000 * i) for i in range(5))
    sections = {"instructions": {"top": 2}}
    text = section(report(instruction_files=files, section_config=sections))
    assert text.count("| `") == 2 and "3 more file(s) cost less." in text
    assert "an upper bound" not in text
    assert "## Instruction files" not in report()
    assert "## Instruction files" not in report(instruction_files=files, section_config={
        "instructions": {"top": 0}})  # fmt: skip


def test_paths_and_roles_are_escaped() -> None:
    files = (file("~/a|b`c/SKILL.md", 10, role="dev|*x*"),)
    text = section(report(instruction_files=files))
    assert "dev\\|\\*x\\*" in text and "| `~/a\\|b`" not in text
