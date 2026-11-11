#!/usr/bin/env python3
"""Pin the Dockerfile's base images by digest (audit L14, #336).

Every image the Dockerfile pulls (``FROM <image>`` and ``COPY --from=<image>``) is written
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
# ``FROM <ref> [AS name]`` and ``COPY --from=<ref> ...``; a stage name (no ``:`` and no
# ``/`` and no ``@``) is not an image.
_REF = r"(?P<ref>[a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?)"
_FROM = re.compile(rf"^(?P<head>FROM\s+(?:--platform=\S+\s+)?){_REF}(?P<tail>(?:\s.*)?)$")
_COPY = re.compile(rf"^(?P<head>COPY\s+(?:\S+\s+)*?--from=){_REF}(?P<tail>\s.*)$")


@dataclass(frozen=True)
class Ref:
    image: str  # registry/path, no tag
    tag: str
    digest: str | None

    @classmethod
    def parse(cls, text: str) -> Ref | None:
        """An image reference with a tag, or None (a stage name, or no tag to pin to)."""
        name, _, digest = text.partition("@")
        last = name.rsplit("/", 1)[-1]
        if ":" not in last:
            return None
        image, _, tag = name.rpartition(":")
        return cls(image, tag, digest or None)

    @property
    def tagged(self) -> str:
        return f"{self.image}:{self.tag}"

    def pinned(self, digest: str) -> str:
        return f"{self.tagged}@{digest}"


def image_lines(text: str) -> list[tuple[int, Ref]]:
    """Line numbers (0-based) and references of every image the Dockerfile pulls."""
    found: list[tuple[int, Ref]] = []
    for n, line in enumerate(text.splitlines()):
        m = _FROM.match(line) or _COPY.match(line)
        if m is not None and (ref := Ref.parse(m["ref"])) is not None:
            found.append((n, ref))
    return found


def unpinned(text: str) -> list[str]:
    """The images not pinned by digest, and image lines without a tag (unpinnable)."""
    bad = [ref.tagged for _, ref in image_lines(text) if ref.digest is None]
    for line in text.splitlines():
        m = _FROM.match(line) or _COPY.match(line)
        ref = m["ref"] if m else ""
        if m and Ref.parse(ref) is None and ("/" in ref or "." in ref or "@" in ref):
            bad.append(ref)
    return bad


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


def main(argv: list[str] | None = None) -> int:
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
    digests: dict[str, str] = {}
    for _, ref in image_lines(text):
        if ref.tagged not in digests:
            digests[ref.tagged] = index_digest(ref.tagged)
            print(f"{ref.tagged}@{digests[ref.tagged]}")
    if args.write:
        new = rewrite(text, digests)
        if new != text:
            args.dockerfile.write_text(new)
            print(f"rewrote {args.dockerfile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
