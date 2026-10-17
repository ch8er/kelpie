"""Suite-wide fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True, scope="session")
def _private_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """No test writes to the developer's home: Kelpie's defaults live under ``~`` (the wait
    key that signs parks and stops is created on first use)."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path_factory.mktemp("home"))
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
