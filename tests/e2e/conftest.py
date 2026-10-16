"""Scenario fixtures: a fresh simulated farm per test, torn down with a log check.

Every scenario fails if ``kelpie serve`` logged an ERROR (or worse), a traceback or a WARNING
that reports a failure (see :data:`tests.sim.kelpie.FAILURE_WARNINGS`) it did not declare in
``farm.allowed_errors``, or did not exit cleanly on SIGTERM.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kelpie.sim import github
from tests.sim import channels
from tests.sim.farm import Farm


@pytest.fixture
def farm(tmp_path: Path) -> Iterator[Farm]:
    with channels.serve() as chans, github.serve() as gh:
        sim = Farm(tmp_path, chans, gh)
        try:
            yield sim
        finally:
            sim.close()
