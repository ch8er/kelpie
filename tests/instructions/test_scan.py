"""Measuring instruction files: sizes, sections, facts, append detection, copies."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kelpie.instructions.scan import Scanned, Unreadable, copies, measure, read_file

CASE = "When the build fails on the toolchain step, rerun with the pinned version and attach the log to the task before you ask for review"  # noqa: E501
OTHER = "Release notes are written from merged pull requests grouped by area, newest first, with one line per change and a link to it"  # noqa: E501


def doc(*sections: tuple[str, str], front: bool = False) -> bytes:
    head = "---\nname: example\nlicense: MIT\n---\n" if front else ""
    return (head + "".join(f"## {h}\n{b}\n\n" for h, b in sections)).encode()


def test_measure_counts_chars_tokens_sections_and_skips_front_matter_and_code() -> None:
    text = doc(("Build", CASE), ("Notes", "```\n# not a heading\nkey: value\n```\n"), front=True)
    m = measure(text)
    assert m.bytes == len(text) and m.chars == len(text.decode())
    assert m.tokens == -(-m.chars // 4)
    assert [s.heading for s in m.sections] == ["build", "notes"]
    assert m.sections[0].line == 5
    assert m.facts == ()  # front matter and code hold no facts
    assert m.appended is None


def test_append_only_is_told_from_an_edit_by_the_old_prefix_hash() -> None:
    old = doc(("Build", CASE))
    first = measure(old)
    appended = measure(old + doc(("Release", OTHER)), (first.bytes, first.sha256))
    assert appended.appended is True
    edited = measure(doc(("Build", OTHER), ("Release", CASE)), (first.bytes, first.sha256))
    assert edited.appended is False
    assert measure(old, (first.bytes, first.sha256)).appended is None  # unchanged
    shrunk = measure(old[:20], (first.bytes, first.sha256))
    assert shrunk.appended is False


def test_near_duplicate_sections_within_and_across_files() -> None:
    a = measure(
        doc(("Toolchain failure", CASE), ("Release", OTHER), ("Toolchain again", CASE + " now"))
    )
    b = measure(doc(("Toolchain failure", CASE.replace("attach", "add")), ("Other", "short")))
    found = copies("dev", [Scanned("~/a.md", a), Scanned("~/b.md", b)])
    (dupe,) = [d for d in found if d.kind == "section"]
    assert dupe.role == "dev"
    assert dupe.places == ("~/a.md:1", "~/a.md:7", "~/b.md:1")
    assert 0.5 <= dupe.similarity < 1


def test_same_heading_lowers_the_bar() -> None:
    half = " ".join(CASE.split()[:18]) + " and then " + " ".join(OTHER.split()[:5])  # ~0.53
    a = measure(doc(("Build failure", CASE)))
    b = measure(doc(("Build failure", half)))
    c = measure(doc(("Something else", half)))
    assert [d.kind for d in copies(None, [Scanned("a", a), Scanned("b", b)])] == ["section"]
    assert copies(None, [Scanned("a", a), Scanned("c", c)]) == ()


def test_one_value_in_several_files_but_not_two_and_not_sentences() -> None:
    files = [
        Scanned(
            f"~/f{i}.md",
            measure(f"# X\n- **Default timeout**: 30 minutes\nNote: {OTHER}\n".encode()),
        )
        for i in range(3)
    ]
    (value,) = [d for d in copies("support", files) if d.kind == "value"]
    assert value.kind == "value" and value.places == ("~/f0.md:2", "~/f1.md:2", "~/f2.md:2")
    assert [d for d in copies("support", files[:2]) if d.kind == "value"] == []
    assert [d for d in copies("support", files, min_value_files=4) if d.kind == "value"] == []


def test_read_file_refuses_symlinks_non_files_and_large_files(tmp_path: Path) -> None:
    real = tmp_path / "SKILL.md"
    real.write_bytes(b"# A\n")
    assert read_file(real, 100) == b"# A\n"
    link = tmp_path / "link.md"
    link.symlink_to(real)
    with pytest.raises(Unreadable, match="cannot open"):
        read_file(link, 100)
    with pytest.raises(Unreadable, match="not a regular file"):
        read_file(tmp_path, 100)
    with pytest.raises(Unreadable, match="larger than 2 bytes"):
        read_file(real, 2)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(Unreadable, match="not a regular file"):
        read_file(fifo, 100)
