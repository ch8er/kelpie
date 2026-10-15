"""Kelpie's own metrics in Prometheus text exposition format (stub, stdlib only)."""

from __future__ import annotations

import time

from kelpie import __version__
from kelpie.rules import REGISTRY

_STARTED = time.time()


def render() -> str:
    lines = [
        "# HELP kelpie_build_info Kelpie build information.",
        "# TYPE kelpie_build_info gauge",
        f'kelpie_build_info{{version="{__version__}"}} 1',
        "# HELP kelpie_start_time_seconds Unix time the server started.",
        "# TYPE kelpie_start_time_seconds gauge",
        f"kelpie_start_time_seconds {_STARTED:.3f}",
        "# HELP kelpie_rules_registered Number of control rules loaded.",
        "# TYPE kelpie_rules_registered gauge",
        f"kelpie_rules_registered {len(REGISTRY)}",
    ]
    return "\n".join(lines) + "\n"
