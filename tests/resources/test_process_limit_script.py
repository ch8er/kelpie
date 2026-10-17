"""scripts/kelpie-process-limit.sh writes a user's processes and limit for the textfile
collector (macOS: kern.maxprocperuid; elsewhere the process ulimit)."""

from __future__ import annotations

import getpass
import os
import re
import resource
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "kelpie-process-limit.sh"


def run(*args: str, path: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{path}:{os.environ['PATH']}"} if path else None
    return subprocess.run(
        ["sh", str(SCRIPT), *args], capture_output=True, text=True, check=False, env=env
    )


def sysctl(tmp_path: Path, body: str) -> Path:
    """A stub sysctl first on the PATH."""
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (stub / "sysctl").write_text(f"#!/bin/sh\n{body}\n")
    (stub / "sysctl").chmod(0o755)
    return stub


def test_writes_the_users_processes_and_limit(tmp_path: Path) -> None:
    result = run(str(tmp_path))
    assert result.returncode == 0, result.stderr
    text = (tmp_path / "kelpie_processes.prom").read_text()
    user = re.escape(getpass.getuser())
    assert re.search(rf'^kelpie_user_processes\{{user="{user}"\}} [1-9]\d*$', text, re.M)
    limit = re.search(rf'^kelpie_user_processes_max\{{user="{user}"\}} (\d+)$', text, re.M)
    # A process ulimit of "unlimited" writes no limit, and says so.
    assert limit is not None or "no numeric process limit" in result.stderr
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".kelpie_processes.")]


def test_usage_errors(tmp_path: Path) -> None:
    assert run().returncode == 2
    assert run(str(tmp_path / "nope")).returncode == 2
    assert run(str(tmp_path), 'a"b').returncode == 2  # never written into a label


def test_the_limit_is_the_lower_of_maxprocperuid_and_the_ulimit(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    low = run(str(out), path=sysctl(tmp_path, "echo 7"))
    assert low.returncode == 0, low.stderr
    assert re.search(
        r"^kelpie_user_processes_max\{.*\} 7$", (out / "kelpie_processes.prom").read_text(), re.M
    )
    high = run(str(out), path=sysctl(tmp_path, "echo 999999999"))
    assert high.returncode == 0, high.stderr
    limit = re.search(r"^kelpie_user_processes_max\{.*\} (\d+)$",
                      (out / "kelpie_processes.prom").read_text(), re.M)  # fmt: skip
    assert limit is not None
    soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
    expected = 999999999 if soft == resource.RLIM_INFINITY else min(999999999, soft)
    assert int(limit[1]) == expected  # the ulimit, when it is lower


def test_an_unknown_user_is_refused(tmp_path: Path) -> None:
    result = run(str(tmp_path), "no-such-kelpie-user")
    assert result.returncode == 2 and "no user" in result.stderr
    assert run(str(tmp_path), "-x").returncode == 2
    assert not (tmp_path / "kelpie_processes.prom").exists()
