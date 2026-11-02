"""The instruction watch: scans, size history, growth, sessions a day, failures."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core.model import Owner, Session, SkillLoad, Snapshot
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


def test_remote_sessions_do_not_count_and_files_of_every_role_count_all(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("# Project\n" + "x" * 396)  # 101 tokens
    settings = InstructionSettings(enabled=True)
    w = InstructionWatch(
        SqliteInstructionStore(":memory:"), settings, (Source((f"{tmp_path}/CLAUDE.md",)),)
    )
    owners = (Owner("dev", "developer"), Owner("laptop:dev", "developer", host="laptop"))
    local = sessions(T0, 3, "dev")
    remote = tuple(
        Session(f"r{i}", "laptop:T-1", "laptop:dev", T0, source="hermes") for i in range(4)
    )
    (f,) = w.read(Snapshot(now=T0, owners=owners, sessions=local + remote))[0]
    assert f.tokens_per_day == f.tokens * 3 and f.token_basis == "sessions"


def test_role_from_a_path_with_doubled_separators(tmp_path: Path) -> None:
    (tmp_path / "w-ops").mkdir()
    (tmp_path / "w-ops" / "AGENTS.md").write_text("# Ops\n")
    src = Source((f"{tmp_path}//w-{{role}}/AGENTS.md",))
    w = InstructionWatch(SqliteInstructionStore(":memory:"), InstructionSettings(), (src,))
    (f,) = w.read(Snapshot(now=T0))[0]
    assert f.role == "ops"


def test_skills_of_a_platform_counting_loads_cost_their_loads(farm: Path, tmp_path: Path) -> None:
    store = SqliteInstructionStore(tmp_path / "k.db")
    for role in ("reviewer", "dev-1"):
        (farm / "profiles" / role / "skills" / "review" / "SKILL.md").write_text(lesson(1))
    (farm / "profiles" / "dev-1" / "skills" / "unused").mkdir()
    (farm / "profiles" / "dev-1" / "skills" / "unused" / "SKILL.md").write_text(lesson(2))
    (farm / "AGENTS.md").write_text("# Core\n")
    w = watch(farm, store)
    owners = (Owner("reviewer", "reviewer"), Owner("dev-1", "dev"))

    def load(role: str, hours: float, skill: str = "review", source: str = "hermes") -> SkillLoad:
        return SkillLoad(skill, T0 - timedelta(hours=hours), "s", role, source)

    loads = (
        load("reviewer", 1),
        load("reviewer", 2),
        load("reviewer", 30),  # yesterday's
        load("dev", 3),
        load("dev", 3, source="claude-code"),  # another platform's skill of that name
        load("ops", 3),  # a role with no such skill: no file of its own nor a shared one
    )
    snap = Snapshot(
        now=T0,
        owners=owners,
        sessions=sessions(T0, 7),
        skill_loads=loads,
        skill_loads_counted=("hermes",),
    )
    files = {f.path: f for f in w.read(snap)[0]}
    rev = files["~/profiles/reviewer/skills/review/SKILL.md"]
    assert (rev.token_basis, rev.tokens_per_day) == ("loads", rev.tokens * 2)
    dev = files["~/profiles/dev-1/skills/review/SKILL.md"]
    assert (dev.role, dev.token_basis, dev.tokens_per_day) == ("dev", "loads", dev.tokens)
    unused = files["~/profiles/dev-1/skills/unused/SKILL.md"]
    assert (unused.token_basis, unused.tokens_per_day) == ("loads", 0)
    agents = files["~/AGENTS.md"]  # always loaded: read into every session
    assert (agents.token_basis, agents.tokens_per_day) == ("sessions", agents.tokens * 7)
    # The platform did not count its loads this tick: sessions, an upper bound.
    files = {f.path: f for f in w.read(replace(snap, skill_loads_counted=()))[0]}
    rev = files["~/profiles/reviewer/skills/review/SKILL.md"]
    assert (rev.token_basis, rev.tokens_per_day) == ("sessions", rev.tokens * 7)
    store.close()


def test_a_shared_skill_counts_the_loads_of_roles_without_their_own(tmp_path: Path) -> None:

    for d in ("shared/a/review", "shared/b/review", "profiles/dev/skills/review"):
        (tmp_path / d).mkdir(parents=True)
        (tmp_path / d / "SKILL.md").write_text(lesson(1))
    sources = (
        Source((f"{tmp_path}/shared/**/SKILL.md",), loaded="on_demand", platform="hermes"),
        Source(
            (f"{tmp_path}/profiles/{{role}}/skills/*/SKILL.md",),
            loaded="on_demand",
            platform="hermes",
        ),
    )
    store = SqliteInstructionStore(":memory:")
    w = InstructionWatch(store, InstructionSettings(enabled=True), sources, home=str(tmp_path))
    loads = tuple(
        SkillLoad("review", T0 - timedelta(hours=1), "s", role, "hermes")
        for role in ("dev", "ops", "qa")
    )
    snap = Snapshot(now=T0, skill_loads=loads, skill_loads_counted=("hermes",))
    got = {f.path: f.tokens_per_day // f.tokens for f in w.read(snap)[0]}
    assert got == {
        "~/shared/a/review/SKILL.md": 2,  # the first of the shared skills of that name
        "~/shared/b/review/SKILL.md": 0,
        "~/profiles/dev/skills/review/SKILL.md": 1,
    }
    store.close()
