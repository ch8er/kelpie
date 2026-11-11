#!/usr/bin/env python3
"""Pin the Dockerfile's base images by digest (audit L14, #336).

Every image the Dockerfile pulls (``FROM <image>``, and ``COPY --from=<image>``, although
the Dockerfile names uv in a stage of its own so Dependabot, which reads ``FROM`` lines
only, sees it) is written
as ``<image>:<tag>@sha256:<digest>``: the tag says which release it is, the digest is what
is pulled. The digest is the image *index* (the multi-platform manifest list), so the
release workflow's multi-arch build (linux/amd64 and linux/arm64) pulls the matching
platform from it; an image whose index lacks one of those platforms is refused.

Digests are resolved where the registries are reachable (the ``pin-base-images``
workflow): ``docker buildx imagetools inspect <image>:<tag>`` per image. No digest is ever
copied by hand. Dependabot (``docker`` ecosystem) keeps them current afterwards.

    scripts/pin_base_images.py            # print each image and its digest
    scripts/pin_base_images.py --write    # also rewrite the Dockerfile
    scripts/pin_base_images.py --check    # exit 1 unless every image is pinned (offline)

Standard library only; ``docker`` with buildx must be on PATH for resolving.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PLATFORMS = ("linux/amd64", "linux/arm64")  # what the release workflow builds
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# ``FROM [--platform=…] <ref> [AS name]`` and ``COPY … --from=<ref> …`` (instructions are
# case-insensitive and may be indented). ``<ref>`` is anything up to a blank: what it is
# (an image, a stage name, a build argument) is decided by :func:`classify`.
_FROM = re.compile(
    r"^(?P<head>\s*FROM\s+(?:--platform=\S+\s+)?)(?P<ref>\S+)(?P<tail>(?:\s.*)?)$", re.I
)
_COPY = re.compile(r"^(?P<head>\s*COPY\s+(?:\S+\s+)*?--from=)(?P<ref>\S+)(?P<tail>\s.*)$", re.I)
_STAGE = re.compile(r"\sAS\s+(?P<name>\S+)\s*$", re.I)
# registry[:port]/path:tag[@sha256:…], or a Docker Hub name:tag[@sha256:…]
_IMAGE = re.compile(
    r"(?P<image>(?:[a-z0-9.-]+(?::\d+)?/)?[a-z0-9]+(?:[._/-][a-z0-9]+)*)"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9._-]{0,127})(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
)


@dataclass(frozen=True)
class Ref:
    image: str  # registry/path, no tag
    tag: str
    digest: str | None

    @classmethod
    def parse(cls, text: str) -> Ref | None:
        """An image reference with a tag (and maybe a digest), or None."""
        m = _IMAGE.fullmatch(text)
        return None if m is None else cls(m["image"], m["tag"], m["digest"])

    @property
    def tagged(self) -> str:
        return f"{self.image}:{self.tag}"

    def pinned(self, digest: str) -> str:
        return f"{self.tagged}@{digest}"


def classify(text: str) -> tuple[list[tuple[int, Ref]], list[str]]:
    """The images the Dockerfile pulls (0-based line numbers and references), and the
    references it cannot pin: an image without a tag, a build argument, anything that is
    neither an image with a tag nor an earlier stage's name."""
    images: list[tuple[int, Ref]] = []
    unknown: list[str] = []
    stages: set[str] = set()
    for n, line in enumerate(text.splitlines()):
        m = _FROM.match(line) or _COPY.match(line)
        if m is None:
            continue
        raw = m["ref"]
        if (ref := Ref.parse(raw)) is not None:
            images.append((n, ref))
        elif raw.casefold() not in stages:
            unknown.append(raw)
        if m.re is _FROM and (stage := _STAGE.search(m["tail"])) is not None:
            stages.add(stage["name"].casefold())
    return images, unknown


def image_lines(text: str) -> list[tuple[int, Ref]]:
    """Line numbers (0-based) and references of every image the Dockerfile pulls."""
    return classify(text)[0]


def unpinned(text: str) -> list[str]:
    """The images not pinned by digest, then the references that cannot be pinned."""
    images, unknown = classify(text)
    return [ref.tagged for _, ref in images if ref.digest is None] + unknown


def rewrite(text: str, digests: dict[str, str]) -> str:
    """``text`` with every image line pinned to ``digests[image:tag]``."""
    lines = text.splitlines(keepends=True)
    for n, ref in image_lines(text):
        digest = digests[ref.tagged]
        if DIGEST.fullmatch(digest) is None:
            raise ValueError(f"not a sha256 digest for {ref.tagged}: {digest!r}")
        line = lines[n]
        m = _FROM.match(line.rstrip("\n")) or _COPY.match(line.rstrip("\n"))
        assert m is not None
        end = "\n" if line.endswith("\n") else ""
        lines[n] = f"{m['head']}{ref.pinned(digest)}{m['tail']}{end}"
    return "".join(lines)


def inspect(tagged: str) -> dict[str, object]:
    """The image index of ``tagged`` as ``docker buildx imagetools inspect`` reports it."""
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", tagged, "--format", "{{json .Manifest}}"],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout
    manifest = json.loads(out)
    if not isinstance(manifest, dict):
        raise ValueError(f"{tagged}: unexpected inspect output")
    return manifest


def index_digest(tagged: str, read: Callable[[str], dict[str, object]] = inspect) -> str:
    """The digest of ``tagged``'s multi-platform index; ValueError when it is not an index
    or lacks a platform the release builds."""
    manifest = read(tagged)
    digest = manifest.get("digest")
    if not isinstance(digest, str) or DIGEST.fullmatch(digest) is None:
        raise ValueError(f"{tagged}: no sha256 digest in the inspect output")
    platforms = set()
    for entry in manifest.get("manifests") or []:
        platform = entry.get("platform") if isinstance(entry, dict) else None
        if isinstance(platform, dict):
            platforms.add(f"{platform.get('os')}/{platform.get('architecture')}")
    if missing := [p for p in PLATFORMS if p not in platforms]:
        raise ValueError(f"{tagged}: not a multi-platform index with {', '.join(missing)}")
    return digest


def main(
    argv: list[str] | None = None, read: Callable[[str], dict[str, object]] | None = None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dockerfile", type=Path, default=Path("Dockerfile"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="rewrite the Dockerfile")
    mode.add_argument("--check", action="store_true", help="fail unless every image is pinned")
    args = parser.parse_args(argv)
    text = args.dockerfile.read_text()
    if args.check:
        if bad := unpinned(text):
            print(f"not pinned by digest: {', '.join(bad)}", file=sys.stderr)
            return 1
        print("every base image is pinned by digest")
        return 0
    images, unknown = classify(text)
    if unknown:
        print(f"cannot pin (no tag, or not an image): {', '.join(unknown)}", file=sys.stderr)
        return 1
    digests: dict[str, str] = {}
    for _, ref in images:
        if ref.tagged not in digests:
            digests[ref.tagged] = index_digest(ref.tagged, read or inspect)
            print(f"{ref.tagged}@{digests[ref.tagged]}")
    if args.write:
        new = rewrite(text, digests)
        if new != text:
            args.dockerfile.write_text(new)
            print(f"rewrote {args.dockerfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
