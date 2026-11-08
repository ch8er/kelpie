#!/bin/sh
# Gets a fresh checkout (a new cloud session, a new laptop) ready to run the local checks
# from CLAUDE.md: uv, Python 3.12 and every locked dependency.
#
# Usage: scripts/bootstrap.sh [--check]
#
# Without arguments it only prepares the environment; with --check it then runs the same
# checks as CLAUDE.md ("Commands"): ruff, the format check, import contracts, pytest with
# coverage and the 90% gate on kelpie.rules. It prints how long each step took.
#
# Idempotent and safe to re-run: each step is skipped or is a no-op when its result is
# already there (uv on PATH, a Python 3.12 that uv can find, a .venv in sync with
# uv.lock). It never edits tracked files, never touches git and writes nothing outside
# the checkout except uv's own install and cache directories. uv is installed, only when
# missing, with `python3 -m pip install --user uv` and else uv's official installer
# (told to leave shell profiles alone).
# Dependencies come from uv.lock as they are (`uv sync --frozen`), as in CI.
set -eu

check=0
for arg in "$@"; do
  case "$arg" in
    --check) check=1 ;;
    -h | --help)
      sed -n '2,/^[^#]/s/^# \{0,1\}//p' "$0"
      exit 0
      ;;
    *)
      echo "$0: unknown argument: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
[ -f pyproject.toml ] && [ -f uv.lock ] || {
  echo "$0: run from a Kelpie checkout (no pyproject.toml/uv.lock in $root)" >&2
  exit 2
}

start=$(date +%s)
step_start=$start
step() {
  now=$(date +%s)
  printf '[bootstrap] %-28s %4ss\n' "$1" "$((now - step_start))"
  step_start=$now
}

# 1. uv. The usual install locations are added to PATH first, so a re-run finds the uv a
# previous run installed even when the shell profile has not picked it up.
PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PATH
if ! command -v uv >/dev/null 2>&1; then
  echo "[bootstrap] uv not found, installing it"
  if command -v python3 >/dev/null 2>&1; then
    if ! python3 -m pip install --user --quiet uv; then
      # PEP 668: a distribution Python refuses user installs without this flag.
      echo "[bootstrap] retrying with --break-system-packages (user site only)"
      python3 -m pip install --user --quiet --break-system-packages uv || :
    fi
    # pip's user scripts directory (~/Library/Python/3.X/bin on macOS, or $PYTHONUSERBASE).
    user_base=$(python3 -m site --user-base 2>/dev/null || :)
    [ -n "$user_base" ] && PATH="$user_base/bin:$PATH"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    if command -v curl >/dev/null 2>&1; then
      echo "[bootstrap] installing uv with its official installer"
      # UV_NO_MODIFY_PATH: leave the shell profiles alone; this script sets PATH itself.
      curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
    else
      echo "$0: cannot install uv: pip failed and there is no curl; see https://docs.astral.sh/uv/" >&2
      exit 1
    fi
  fi
  command -v uv >/dev/null 2>&1 || {
    echo "$0: uv was installed but is not on PATH" >&2
    exit 1
  }
fi
step "uv $(uv --version | cut -d' ' -f2)"

# 2. Python 3.12 (.python-version). uv uses a system one when it has it.
if ! uv python find 3.12 >/dev/null 2>&1; then
  uv python install 3.12
fi
py=$(uv python find 3.12) || {
  echo "$0: uv cannot find or install Python 3.12" >&2
  exit 1
}
step "python $("$py" -c 'import platform; print(platform.python_version())')"

# 3. Locked dependencies, runtime and dev, into .venv.
uv sync --frozen --quiet
step "uv sync --frozen"
# uv run would otherwise re-lock (and rewrite uv.lock) when pyproject.toml has moved on.
UV_FROZEN=1
export UV_FROZEN

if [ "$check" -eq 1 ]; then
  uv run ruff check . --quiet
  step "ruff check"
  uv run ruff format --check . --quiet
  step "ruff format --check"
  uv run lint-imports >/dev/null
  step "lint-imports"
  uv run pytest --cov=kelpie -q
  step "pytest --cov=kelpie"
  uv run coverage report --include='src/kelpie/rules/*' --fail-under=90 >/dev/null
  step "coverage kelpie.rules >= 90%"
fi

printf '[bootstrap] ready in %ss\n' "$(($(date +%s) - start))"
