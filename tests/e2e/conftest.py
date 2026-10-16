"""Scenario fixtures: a fresh simulated farm per test, torn down with a log check.

Every scenario fails if ``kelpie serve`` logged an ERROR (or worse) or a traceback it did not
declare in ``farm.allowed_errors``, or did not exit cleanly on SIGTERM.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.sim import channels, github
from tests.sim.farm import Farm


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "tests/e2e/" in item.nodeid:
            item.add_marker(pytest.mark.e2e)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: runs kelpie serve against a simulated farm")


@pytest.fixture
def farm(tmp_path: Path) -> Iterator[Farm]:
    with channels.serve() as chans, github.serve() as gh:
        sim = Farm(tmp_path, chans, gh)
        try:
            yield sim
        finally:
            sim.close()
