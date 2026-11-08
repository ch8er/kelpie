"""Property-based tests of the parsers that read what agents and senders write.

Every example is generated, so these tests carry no farm data. ``KELPIE_HYPOTHESIS=thorough``
runs many more examples locally (``uv run pytest tests/property``).
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "default",
    max_examples=60,
    deadline=None,  # the first examples import and warm caches
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.register_profile(
    "thorough",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
# CI: the same examples on every run, no example database, and a failure prints the blob
# that replays it (``@reproduce_failure``), so a red build is never a coin toss.
settings.register_profile(
    "ci",
    max_examples=60,
    deadline=None,
    derandomize=True,
    database=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
_default = "ci" if os.environ.get("CI") else "default"
settings.load_profile(os.environ.get("KELPIE_HYPOTHESIS", _default))
