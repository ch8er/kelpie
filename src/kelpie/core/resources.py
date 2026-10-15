"""Resource readings from Prometheus (node_exporter). Kelpie has no collector of its own."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from kelpie.core.model import Resource


@dataclass(slots=True)
class PrometheusSource:
    """Reads host resources via the Prometheus HTTP API (stub)."""

    url: str = "http://prometheus:9090"

    def read(self) -> Sequence[Resource]:
        raise NotImplementedError
