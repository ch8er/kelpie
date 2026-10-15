"""Persistence for snapshots, actions and spans (stub). SQLite first, DuckDB for analytics."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Storage:
    path: Path = Path("kelpie.db")
