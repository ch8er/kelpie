"""The instruction watch: scans, size history, growth, sessions a day, failures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core.model import Owner, Session, Snapshot
from kelpie.core.storage import SqliteInstructionStore
from kelpie.instructions.config import InstructionSettings, Source
from kelpie.instructions.watch import InstructionWatch

T0 = datetime(2026, 3, 2, 12, tzinfo=UTC)
LESSON = "When the review finds a missing test, ask for it with the file and the case, and wait for the next push before approving"  # noqa: E501


def lesson(n: int) -> str:
    return f"## Case {n}\n{LESSON} (case {n}).\n\n"


@pytest.fixture
def farm(tmp_path: Path) -> Path:
    for role in ("reviewer", "dev-1"):
        (tmp_path / "profiles" / role / "skills" / "review").mkdir(parents=True)
    return tmp_path


def watch(farm: Path, store: SqliteInstructionStore, **kw: object) -> InstructionWatch:
    settings = InstructionSettings(enabled=True, every=timedelta(hours=1), **kw)  # type: ignore[arg-type]
    sources = (
        Source((f"{farm}/AGENTS.md",), loaded="always", platform="hermes", owner="human-owner"),
        Source(
            (f"{farm}/profiles/{{role}}/skills/**/SKILL.md",),
            loaded="on_demand",
            platform="hermes",
            role_of=lambda name: name.rsplit("-", 1)[0] if name[-1].isdigit() else name,
        ),
    )
    return InstructionWatch(store, settings, sources, home=str(farm))


def sessions(now: datetime, n: int, owner: str = "reviewer") -> tuple[Session, ...]:
    return tuple(
        Session(f"s{i}", f"T-{i}", owner, now - timedelta(hours=i), source="hermes")
        for i in range(n)
    )


def test_append_only_skill_grows_past_the_window_and_is_counted(farm: Path, tmp_path: Path) -> None:
    store = SqliteInstructionStore(tmp_path / "k.db")
    skill = farm / "profiles" / "reviewer" / "skills" / "review" / "SKILL.md"
    skill.write_text(lesson(0))
    (farm / "AGENTS.md").write_text("# Core\nkey: value\n")
    w = watch(farm, store)
    owners = (Owner("reviewer", "reviewer"),)
    files, _ = w.read(Snapshot(now=T0, owners=owners))
    first = {f.path: f for f in files}
    assert set(first) == {"~/AGENTS.md", "~/profiles/reviewer/skills/review/SKILL.md"}
    assert first["~/profiles/reviewer/skills/review/SKILL.md"].role == "reviewer"
    for day in range(1, 9):
        skill.write_text(skill.read_text() + lesson(day))
        now = T0 + timedelta(days=day)
        files, _ = w.read(Snapshot(now=now, owners=owners, sessions=sessions(now, 5)))
    (s,) = [f for f in files if f.role == "reviewer"]
    assert s.loaded == "on_demand" and s.only_grows
    assert s.since == T0 + timedelta(days=1)  # the window start: the size it had then
    assert s.appends == 7 and s.edits == 0
    assert s.chars > s.chars_since
    assert s.tokens_per_day == s.tokens * 5 and s.token_basis == "sessions"
    agents = next(f for f in files if f.role is None)
    assert agents.owner == "human-owner" and agents.tokens_per_day == agents.tokens * 5
    # an edit is not an append
    skill.write_text("# Short core\nSee topics.\n")
    now = T0 + timedelta(days=9)
    files, _ = w.read(Snapshot(now=now))
    (s,) = [f for f in files if f.role == "reviewer"]
    assert s.edits == 1 and not s.only_grows and s.tokens_per_day == 0
    store.close()


def test_cache_between_scans_and_failures_keep_the_last_result(
    farm: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteInstructionStore(":memory:")
    (farm / "AGENTS.md").write_text("# A\n")
    w = watch(farm, store)
    first, _ = w.read(Snapshot(now=T0))
    (farm / "AGENTS.md").write_text("# A\nmore\n")
    again, _ = w.read(Snapshot(now=T0 + timedelta(minutes=30)))
    assert again == first  # not rescanned within the hour

    def broken(since: datetime) -> dict[str, list[object]]:
        raise OSError("disk")

    monkeypatch.setattr(store, "history", broken)
    kept, _ = w.read(Snapshot(now=T0 + timedelta(hours=2)))
    assert kept == first and w.failed


def test_skipped_files_and_the_file_cap(farm: Path, tmp_path: Path) -> None:
    store = SqliteInstructionStore(":memory:")
    (farm / "AGENTS.md").write_text("x" * 50)
    for role in ("reviewer", "dev-1"):
        (farm / "profiles" / role / "skills" / "review" / "SKILL.md").write_text("# S\n")
    w = watch(farm, store, max_bytes=10)
    files, _ = w.read(Snapshot(now=T0))
    assert "~/AGENTS.md" in w.skipped and len(files) == 2
    assert {f.role for f in files} == {"reviewer", "dev"}  # role_of maps dev-1
    capped = watch(farm, SqliteInstructionStore(":memory:"), max_files=1)
    files, _ = capped.read(Snapshot(now=T0))
    assert len(files) == 1 and len(capped.skipped) == 2


def test_copies_across_a_role_and_the_shared_files(farm: Path) -> None:
    store = SqliteInstructionStore(":memory:")
    (farm / "AGENTS.md").write_text("# Shared\n- base branch: main\n")
    a = farm / "profiles" / "reviewer" / "skills" / "review" / "SKILL.md"
    b = farm / "profiles" / "reviewer" / "skills" / "more"
    b.mkdir()
    a.write_text(lesson(1) + "- base branch: main\n")
    (b / "SKILL.md").write_text(lesson(1) + "- base branch: main\n")
    _, copies = watch(farm, store).read(Snapshot(now=T0))
    kinds = {(d.role, d.kind, len(d.places)) for d in copies}
    assert ("reviewer", "section", 2) in kinds
    assert ("reviewer", "value", 3) in kinds  # the shared AGENTS.md counts for the role
