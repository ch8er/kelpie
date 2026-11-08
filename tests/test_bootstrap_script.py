"""scripts/bootstrap.sh prepares a fresh checkout for the local checks and is safe to
re-run. These tests run it against a fake `uv` that records its arguments, so they need
no network and touch no real environment."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "bootstrap.sh"

FAKE_UV = """#!/bin/sh
echo "$*" >> "$UV_LOG"
case "$1" in
  --version) echo "uv 0.0.0" ;;
  python)
    case "$2" in
      install) : > "$PY_READY" ;;
      find) [ -f "$PY_READY" ] || exit 1; echo "{python}" ;;
    esac
    ;;
esac
exit 0
"""


def checkout(tmp_path: Path, *, python_ready: bool) -> tuple[Path, dict[str, str], Path]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, root / "scripts" / "bootstrap.sh")
    (root / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (root / "uv.lock").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV.replace("{python}", sys.executable))
    uv.chmod(0o755)
    log = tmp_path / "uv.log"
    ready = tmp_path / "py-ready"
    if python_ready:
        ready.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "UV_LOG": str(log),
        "PY_READY": str(ready),
    }
    return root, env, log


def run(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(root / "scripts" / "bootstrap.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd="/",
    )


def test_prepares_then_rerun_installs_nothing(tmp_path: Path) -> None:
    root, env, log = checkout(tmp_path, python_ready=False)
    first = run(root, env)
    assert first.returncode == 0, first.stderr
    calls = log.read_text().splitlines()
    assert "python install 3.12" in calls
    assert "sync --frozen --quiet" in calls
    assert "ready in" in first.stdout

    log.unlink()
    again = run(root, env)
    assert again.returncode == 0, again.stderr
    calls = log.read_text().splitlines()
    assert "python install 3.12" not in calls
    assert "sync --frozen --quiet" in calls
    assert not any(c.startswith("run ") for c in calls)
    assert "uv not found" not in again.stdout


def test_check_runs_the_claude_md_checks(tmp_path: Path) -> None:
    root, env, log = checkout(tmp_path, python_ready=True)
    result = run(root, env, "--check")
    assert result.returncode == 0, result.stderr
    runs = [c for c in log.read_text().splitlines() if c.startswith("run ")]
    assert runs == [
        "run ruff check . --quiet",
        "run ruff format --check . --quiet",
        "run lint-imports",
        "run pytest --cov=kelpie -q",
        "run coverage report --include=src/kelpie/rules/* --fail-under=90",
    ]


def test_rejects_unknown_arguments_and_a_foreign_directory(tmp_path: Path) -> None:
    root, env, _ = checkout(tmp_path, python_ready=True)
    bad = run(root, env, "--fast")
    assert bad.returncode == 2
    assert "unknown argument" in bad.stderr
    (root / "uv.lock").unlink()
    foreign = run(root, env)
    assert foreign.returncode == 2
    assert "Kelpie checkout" in foreign.stderr


def test_help_prints_the_header(tmp_path: Path) -> None:
    root, env, _ = checkout(tmp_path, python_ready=True)
    result = run(root, env, "--help")
    assert result.returncode == 0
    assert result.stdout.startswith("Gets a fresh checkout")
    assert "Idempotent" in result.stdout
