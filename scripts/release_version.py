"""Release version checks for .github/workflows/release.yml (stdlib only).

    python3 scripts/release_version.py v0.1.0a1     # prints 0.1.0a1, or fails
    python3 scripts/release_version.py --is-prerelease 0.1.0a1   # exit 0: a pre-release

A release tag is ``v`` plus the version in pyproject.toml, written exactly as there (a
canonical PEP 440 release, pre-release or post-release: 0.1.0, 0.1.0a1, 0.1.0rc2,
0.1.0.post1), and ``kelpie.__version__`` must say the same. Anything else stops the
release before it builds.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = re.compile(r"(0|[1-9]\d*)(\.(0|[1-9]\d*)){2}((a|b|rc)(0|[1-9]\d*))?(\.post(0|[1-9]\d*))?")
PRE = re.compile(r".*(a|b|rc)\d+(\.post\d+)?")


def project_version(root: Path = ROOT) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def package_version(root: Path = ROOT) -> str:
    text = (root / "src" / "kelpie" / "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'^__version__ = "([^"]+)"$', text, re.MULTILINE)
    if found is None:
        raise ValueError("src/kelpie/__init__.py has no __version__")
    return found.group(1)


def check(tag: str, root: Path = ROOT) -> str:
    """The version a tag releases; ValueError naming what disagrees."""
    if not tag.startswith("v"):
        raise ValueError(f"tag {tag!r} must be v<version>, e.g. v0.1.0a1")
    version = tag[1:]
    if not VERSION.fullmatch(version):
        raise ValueError(
            f"{version!r} is not a canonical release version (0.1.0, 0.1.0a1, 0.1.0rc1, "
            "0.1.0.post1)"
        )
    if (project := project_version(root)) != version:
        raise ValueError(f"tag {tag} but pyproject.toml says {project}")
    if (package := package_version(root)) != version:
        raise ValueError(f"tag {tag} but kelpie.__version__ is {package}")
    return version


def is_prerelease(version: str) -> bool:
    return PRE.fullmatch(version) is not None


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--is-prerelease":
        return 0 if is_prerelease(argv[1]) else 1
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(check(argv[0]))
    except ValueError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
