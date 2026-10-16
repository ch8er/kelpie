"""``kelpie demo`` through the installed entry point, as a visitor runs it."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def test_kelpie_demo_runs_end_to_end(tmp_path: Path) -> None:
    exe = shutil.which("kelpie")
    assert exe is not None, "the kelpie entry point is not installed"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KELPIE_", "HERMES_"))}
    env["TMPDIR"] = str(tmp_path)  # the farm lives (and is removed) here
    started = time.monotonic()
    result = subprocess.run(
        [exe, "demo"], capture_output=True, text=True, env=env, timeout=120, check=False
    )
    took = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    out = result.stdout
    for scene in ("── 1.", "── 2.", "── 3.", "── 4.", "── 5.", "## Cost of chaos"):
        assert scene in out
    assert "Stopped run hermes-run-" in out
    assert took < 120  # piped output is never paused
    assert list(tmp_path.iterdir()) == []  # nothing left behind
