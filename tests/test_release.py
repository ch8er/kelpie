"""The release pipeline: version checks and the shape of the workflow."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

import kelpie

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "release_version", ROOT / "scripts/release_version.py"
)
assert _spec is not None and _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
sys.modules["release_version"] = release
_spec.loader.exec_module(release)


def tree(tmp_path: Path, project: str, package: str) -> Path:
    (tmp_path / "src" / "kelpie").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{project}"\n')
    (tmp_path / "src" / "kelpie" / "__init__.py").write_text(f'__version__ = "{package}"\n')
    return tmp_path


def test_the_repository_agrees_with_itself() -> None:
    assert release.project_version() == release.package_version() == kelpie.__version__


@pytest.mark.parametrize(
    "version", ["0.1.0", "0.1.0a1", "1.2.3rc10", "0.1.0.post1", "0.1.0b2.post1"]
)
def test_a_matching_tag_releases_its_version(tmp_path: Path, version: str) -> None:
    assert release.check(f"v{version}", tree(tmp_path, version, version)) == version


@pytest.mark.parametrize(
    ("tag", "project", "package", "error"),
    [
        ("0.1.0", "0.1.0", "0.1.0", "must be v<version>"),
        ("v0.1", "0.1", "0.1", "not a canonical"),
        ("v0.1.0-alpha.1", "0.1.0-alpha.1", "0.1.0-alpha.1", "not a canonical"),
        ("v0.1.0.dev1", "0.1.0.dev1", "0.1.0.dev1", "not a canonical"),
        ("v01.1.0", "01.1.0", "01.1.0", "not a canonical"),
        ("v0.1.0a1", "0.1.0", "0.1.0a1", "pyproject.toml says 0.1.0"),
        ("v0.1.0a1", "0.1.0a1", "0.0.1", "kelpie.__version__ is 0.0.1"),
    ],
)
def test_a_tag_that_disagrees_stops_the_release(
    tmp_path: Path, tag: str, project: str, package: str, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        release.check(tag, tree(tmp_path, project, package))


@pytest.mark.parametrize(
    ("version", "pre"),
    [("0.1.0", False), ("0.1.0.post1", False), ("0.1.0a1", True), ("0.1.0rc2", True),
     ("0.1.0b1.post1", True)],
)  # fmt: skip
def test_prerelease(version: str, pre: bool) -> None:
    assert release.is_prerelease(version) is pre
    assert release.main(["--is-prerelease", version]) == (0 if pre else 1)


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert release.main([f"v{kelpie.__version__}"]) == 0
    assert capsys.readouterr().out.strip() == kelpie.__version__
    assert release.main(["v9.9.9"]) == 1 and "pyproject.toml says" in capsys.readouterr().err
    assert release.main([]) == 2


# --- the workflow -----------------------------------------------------------------------


def workflow() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads `on:` as True
    return data


def test_only_tags_trigger_a_release() -> None:
    assert workflow()["on"] == {"push": {"tags": ["v*"]}}


def test_trusted_publishing_with_testpypi_first_and_no_stored_token() -> None:
    jobs = workflow()["jobs"]
    assert workflow()["permissions"] == {"contents": "read"}
    for name, env in (("testpypi", "testpypi"), ("pypi", "pypi")):
        job = jobs[name]
        assert job["environment"]["name"] == env
        assert job["permissions"] == {"id-token": "write"}
        [publish] = [s for s in job["steps"] if "pypi-publish" in s.get("uses", "")]
        assert "password" not in publish.get("with", {})  # OIDC, no API token
    assert jobs["testpypi"]["steps"][-1]["with"]["repository-url"].startswith("https://test.pypi")
    assert jobs["testpypi-install"]["needs"] == ["verify", "testpypi"]
    assert jobs["pypi"]["needs"] == "testpypi-install"
    assert "pypi" in jobs["image"]["needs"] and "pypi" in jobs["github-release"]["needs"]


def test_only_the_jobs_that_need_it_can_write() -> None:
    jobs = workflow()["jobs"]
    writers = {
        name: {k for k, v in job.get("permissions", {}).items() if v == "write"}
        for name, job in jobs.items()
    }
    assert writers == {
        "verify": set(),
        "build": set(),
        "testpypi": {"id-token"},
        "testpypi-install": set(),
        "pypi": {"id-token"},
        "image": {"packages", "id-token", "attestations"},
        "github-release": {"contents"},
    }


def test_the_image_is_latest_only_for_a_final_release() -> None:
    meta = next(s for s in workflow()["jobs"]["image"]["steps"] if s.get("id") == "meta")
    assert "enable=${{ needs.verify.outputs.prerelease == 'false' }}" in meta["with"]["tags"]


@pytest.mark.parametrize("file", ["release.yml", "ci.yml"])
def test_every_action_is_pinned_to_a_commit(file: str) -> None:
    data = yaml.safe_load((ROOT / ".github/workflows" / file).read_text())
    for name, job in data["jobs"].items():
        for step in job.get("steps", ()):
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", step["uses"]), (name, step)


def test_only_our_wheel_comes_from_testpypi() -> None:
    [check] = [s for s in workflow()["jobs"]["testpypi-install"]["steps"] if "run" in s]
    script = check["run"]
    assert "unsafe-best-match" not in script and "--index-url" not in script
    assert 'sha256sum "/tmp/testpypi/$name"' in script and '[ "$got" = "$want" ]' in script


_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})"
)


def test_the_package_ships_nothing_shaped_like_a_real_token() -> None:
    """The simulator's fake GitHub tokens were ``ghp_``-shaped and shipped in the wheel;
    secret scanners on package indexes may flag such strings (#267 item 4, #275)."""
    found = [
        f"{path.relative_to(ROOT)}: {m.group()[:8]}..."
        for path in sorted((ROOT / "src" / "kelpie").rglob("*.py"))
        for m in _TOKEN_SHAPES.finditer(path.read_text("utf-8"))
    ]
    assert found == []
