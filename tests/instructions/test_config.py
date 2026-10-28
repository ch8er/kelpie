"""``[instructions]`` parsing and the platform defaults."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from kelpie.instructions.config import load_instructions, platform_sources


def test_absent_section_is_off_and_present_section_uses_defaults() -> None:
    assert load_instructions(None).enabled is False
    s = load_instructions({})
    assert s.enabled and s.defaults and s.window == timedelta(days=7) and s.files == ()


def test_files_entries() -> None:
    s = load_instructions(
        {
            "defaults": False,
            "every": "30m",
            "window": "14d",
            "similarity": 0.9,
            "min_value_files": 2,
            "files": [
                {"role": "developer", "paths": ["~/dev/CLAUDE.md"], "owner": "human-owner"},
                {"paths": "~/farm/{role}/skills/*/SKILL.md", "loaded": "on_demand"},
            ],
        }
    )
    assert not s.defaults and s.every == timedelta(minutes=30) and s.similarity == 0.9
    dev, skills = s.files
    assert (dev.role, dev.loaded, dev.owner) == ("developer", "always", "human-owner")
    assert skills.paths == ("~/farm/{role}/skills/*/SKILL.md",) and skills.role is None


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("x", "must be a table"),
        ({"bogus": 1}, "unknown"),
        ({"defaults": "yes"}, "defaults must be true or false"),
        ({"every": "10s"}, "every must be at least"),
        ({"window": "soon"}, "window:"),
        ({"max_bytes": 0}, "max_bytes must be a positive integer"),
        ({"min_value_files": 1}, "at least 2"),
        ({"similarity": 1.5}, r"similarity must be a number in \(0, 1\]"),
        ({"files": {}}, "array of tables"),
        ({"files": [{"paths": []}]}, "non-empty list"),
        ({"files": [{"paths": ["a"], "extra": 1}]}, "unknown"),
        ({"files": [{"paths": ["{role}/{role}"]}]}, "once each"),
        ({"files": [{"paths": ["{role}/x"], "role": "dev"}]}, "pick one"),
        ({"files": [{"paths": ["a/**/{role}/x"]}]}, r"\*\* before"),
        ({"every": "2d", "window": "1d"}, "every must not be longer than window"),
        ({"files": [{"paths": ["a"], "loaded": "sometimes"}]}, "loaded must be one of"),
        ({"files": [{"paths": ["a"], "role": ""}]}, "role must be"),
        ({"files": [{"paths": ["a"], "owner": 3}]}, "owner must be"),
    ],
)
def test_bad_sections_are_refused(section: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_instructions(section)


def test_platform_defaults() -> None:
    hermes = platform_sources("hermes", role_of=str.upper)
    assert any("{role}" in p and s.role_of is not None for s in hermes for p in s.paths)
    assert {s.loaded for s in hermes} == {"always", "on_demand"}
    claude = platform_sources("claude-code")
    assert ("~/.claude/CLAUDE.md",) in [s.paths for s in claude]
    openclaw = platform_sources("openclaw", "/srv/oc/")
    assert "/srv/oc/workspace/AGENTS.md" in openclaw[0].paths
    assert all(s.platform == "openclaw" for s in openclaw)
    assert platform_sources("linear") == ()
