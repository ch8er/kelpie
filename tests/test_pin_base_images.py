"""scripts/pin_base_images.py: the Dockerfile's base images pinned by index digest
(audit L14, #336). Offline: the registry is faked."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "pin_base_images", ROOT / "scripts/pin_base_images.py"
)
assert _spec is not None and _spec.loader is not None
pin = importlib.util.module_from_spec(_spec)
sys.modules["pin_base_images"] = pin
_spec.loader.exec_module(pin)

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
DOCKERFILE = """FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
COPY --from=build --chown=x /app /app
FROM python:3.12-slim-bookworm
"""


def index(digest: str, platforms: tuple[str, ...] = pin.PLATFORMS) -> dict[str, object]:
    manifests = [
        {"digest": B, "platform": {"os": p.split("/")[0], "architecture": p.split("/")[1]}}
        for p in platforms
    ]
    manifests.append({"digest": B, "platform": {"os": "unknown", "architecture": "unknown"}})
    return {"digest": digest, "manifests": manifests}


def test_every_pulled_image_is_found_but_not_a_stage() -> None:
    refs = [(n, r.tagged) for n, r in pin.image_lines(DOCKERFILE)]
    assert refs == [
        (0, "python:3.12-slim-bookworm"),
        (1, "ghcr.io/astral-sh/uv:0.11.32"),
        (3, "python:3.12-slim-bookworm"),
    ]


def test_rewrite_pins_and_keeps_tags_stage_names_and_flags() -> None:
    digests = {"python:3.12-slim-bookworm": A, "ghcr.io/astral-sh/uv:0.11.32": B}
    out = pin.rewrite(DOCKERFILE, digests)
    assert out.splitlines() == [
        f"FROM python:3.12-slim-bookworm@{A} AS build",
        f"COPY --from=ghcr.io/astral-sh/uv:0.11.32@{B} /uv /usr/local/bin/uv",
        "COPY --from=build --chown=x /app /app",
        f"FROM python:3.12-slim-bookworm@{A}",
    ]
    assert pin.unpinned(out) == []
    assert pin.rewrite(out, digests) == out  # idempotent
    newer = {"python:3.12-slim-bookworm": B, "ghcr.io/astral-sh/uv:0.11.32": B}
    assert f"python:3.12-slim-bookworm@{B} AS build" in pin.rewrite(out, newer)


def test_check_names_what_is_not_pinned() -> None:
    assert pin.unpinned(DOCKERFILE) == [
        "python:3.12-slim-bookworm",
        "ghcr.io/astral-sh/uv:0.11.32",
        "python:3.12-slim-bookworm",
    ]


def test_the_repository_dockerfile_lists_its_base_images() -> None:
    tagged = {r.tagged for _, r in pin.image_lines((ROOT / "Dockerfile").read_text())}
    assert any(t.startswith("python:3.12-slim") for t in tagged)
    assert any(t.startswith("ghcr.io/astral-sh/uv:") for t in tagged)


def test_index_digest_takes_the_index_and_checks_platforms() -> None:
    assert pin.index_digest("x:1", lambda _: index(A)) == A
    with pytest.raises(ValueError, match="linux/arm64"):
        pin.index_digest("x:1", lambda _: index(A, ("linux/amd64",)))
    with pytest.raises(ValueError, match="digest"):
        pin.index_digest("x:1", lambda _: {"digest": "latest", "manifests": []})


def test_a_bad_digest_is_never_written() -> None:
    with pytest.raises(ValueError, match="sha256"):
        pin.rewrite(
            DOCKERFILE, {"python:3.12-slim-bookworm": "x", "ghcr.io/astral-sh/uv:0.11.32": B}
        )


def test_main_check_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(DOCKERFILE)
    assert pin.main(["--dockerfile", str(dockerfile), "--check"]) == 1

    def fake(tagged: str) -> dict[str, object]:
        return index(A if "python" in tagged else B)

    assert pin.main(["--dockerfile", str(dockerfile), "--write"], fake) == 0
    assert f"ghcr.io/astral-sh/uv:0.11.32@{B}" in capsys.readouterr().out
    assert pin.main(["--dockerfile", str(dockerfile), "--check"]) == 0


def test_the_pins_cover_every_platform_the_release_builds() -> None:
    """The pin is the index digest, checked for the release's platforms: the multi-arch
    build pulls the matching platform from it."""
    import yaml

    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    (build,) = [s for s in release["jobs"]["image"]["steps"] if s.get("id") == "push"]
    platforms = build["with"]["platforms"].split(",")
    assert sorted(platforms) == sorted(pin.PLATFORMS)
    assert build["with"]["context"] == "."  # builds the repository's Dockerfile


def test_dependabot_keeps_the_docker_digests_current() -> None:
    import yaml

    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    (docker,) = [u for u in config["updates"] if u["package-ecosystem"] == "docker"]
    assert docker["directory"] == "/"
    workflow = yaml.safe_load((ROOT / ".github/workflows/pin-base-images.yml").read_text())
    run = "\n".join(s.get("run", "") for s in workflow["jobs"]["pin"]["steps"])
    assert "scripts/pin_base_images.py --write" in run and "--check" in run


@pytest.mark.parametrize(
    ("line", "found"),
    [
        ("FROM localhost:5000/img:1", "localhost:5000/img:1"),
        ("from python:3.12", "python:3.12"),
        ("  FROM python:3.12 AS base", "python:3.12"),
        ("FROM --platform=$BUILDPLATFORM python:3.12", "python:3.12"),
        ("COPY --chown=x --from=ghcr.io/a/b:1 /x /y", "ghcr.io/a/b:1"),
    ],
)
def test_image_line_forms(line: str, found: str) -> None:
    ((_, ref),) = pin.image_lines(line + "\n")
    assert ref.tagged == found
    assert pin.unpinned(line + "\n") == [found]


@pytest.mark.parametrize("line", ["FROM alpine", "FROM ${BASE}", "FROM ghcr.io/x/untagged"])
def test_what_cannot_be_pinned_is_reported_not_skipped(line: str, tmp_path: Path) -> None:
    assert pin.unpinned(line + "\n") == [line.split()[1]]
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(line + "\n")
    assert pin.main(["--dockerfile", str(dockerfile)], lambda _: index(A)) == 1


def test_stage_names_are_not_images() -> None:
    text = "FROM python:3.12 AS Build\nFROM build\nCOPY --from=BUILD /a /b\n"
    assert pin.unpinned(text) == ["python:3.12"]
