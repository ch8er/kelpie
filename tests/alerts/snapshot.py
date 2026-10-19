"""Golden files for rendered alerts: ``tests/alerts/snapshots/<name>``.

``assert_snapshot`` compares a rendering with its file; run with ``KELPIE_UPDATE_SNAPSHOTS=1``
to write the files instead (then review the diff before committing).
"""

from __future__ import annotations

import os
from pathlib import Path

SNAPSHOTS = Path(__file__).parent / "snapshots"
UPDATE = "KELPIE_UPDATE_SNAPSHOTS"


def assert_snapshot(name: str, actual: str) -> None:
    path = SNAPSHOTS / name
    content = actual + "\n"  # files end in a newline
    if os.environ.get(UPDATE) == "1":
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"no snapshot {name}: run the tests with {UPDATE}=1 to write it"
    expected = path.read_text(encoding="utf-8")
    assert content == expected, f"{name} differs from its snapshot (update: {UPDATE}=1)"
