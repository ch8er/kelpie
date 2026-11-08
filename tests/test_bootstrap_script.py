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


# The uv install branch: no uv anywhere, a fake python3 whose pip either installs uv into
# its user base (as pip on macOS does, outside ~/.local/bin) or fails, and a fake curl.
FAKE_PYTHON = """#!/bin/sh
if [ "$1 $2" = "-m pip" ]; then
  echo "pip $*" >> "$UV_LOG"
  [ "$PIP_WORKS" = 1 ] || exit 1
  mkdir -p "$USER_BASE/bin" && cp "$FAKE_UV_SRC" "$USER_BASE/bin/uv"
  exit 0
fi
if [ "$1 $2 $3" = "-m site --user-base" ]; then echo "$USER_BASE"; exit 0; fi
exec {python} "$@"
"""

FAKE_CURL = """#!/bin/sh
echo "curl $*" >> "$UV_LOG"
printf 'echo "installer UV_NO_MODIFY_PATH=$UV_NO_MODIFY_PATH" >> "$UV_LOG"\\n'
printf 'mkdir -p "$HOME/.local/bin" && cp "$FAKE_UV_SRC" "$HOME/.local/bin/uv"\\n'
"""

TOOLS = ("sh", "sed", "date", "cut", "dirname", "cat", "cp", "mkdir", "chmod", "printf")


def bare(tmp_path: Path, *, curl: bool) -> tuple[Path, dict[str, str], Path]:
    root, env, log = checkout(tmp_path, python_ready=True)
    fake_uv = tmp_path / "bin" / "uv"
    src = tmp_path / "uv-src"
    fake_uv.rename(src)
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        assert found, tool
        (tools / tool).symlink_to(found)
    py = tmp_path / "bin" / "python3"
    py.write_text(FAKE_PYTHON.replace("{python}", sys.executable))
    py.chmod(0o755)
    if curl:
        c = tmp_path / "bin" / "curl"
        c.write_text(FAKE_CURL)
        c.chmod(0o755)
    env.update(
        PATH=f"{tmp_path / 'bin'}:{tools}",
        FAKE_UV_SRC=str(src),
        USER_BASE=str(tmp_path / "userbase"),
        PIP_WORKS="0",
    )
    return root, env, log


def test_pip_install_outside_local_bin_is_found(tmp_path: Path) -> None:
    root, env, log = bare(tmp_path, curl=True)
    env["PIP_WORKS"] = "1"
    result = run(root, env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "pip -m pip install --user --quiet uv" in calls
    assert "curl" not in calls
    assert "sync --frozen --quiet" in calls


def test_falls_back_to_the_installer_without_touching_profiles(tmp_path: Path) -> None:
    root, env, log = bare(tmp_path, curl=True)
    result = run(root, env)
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "--break-system-packages" in calls
    assert "installer UV_NO_MODIFY_PATH=1" in calls
    assert "sync --frozen --quiet" in calls


def test_no_pip_and_no_curl_fails_clearly(tmp_path: Path) -> None:
    root, env, _ = bare(tmp_path, curl=False)
    result = run(root, env)
    assert result.returncode == 1
    assert "cannot install uv" in result.stderr
